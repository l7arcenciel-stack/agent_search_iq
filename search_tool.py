"""
Hybrid Search + Semantic Ranker を実行する検索関数。
main.py（Hosted Agentのagent_framework @tool定義）からTool関数として呼ばれるほか、
単体でも動作確認できる。agent_search_starter/search_tool.pyと同じロジック
（Hosted Agent移行に伴う変更なし。実行場所がクライアント側スクリプトから
Hosted Agentのコンテナ内に変わるだけ）。

ACLフィルタ（Security Filters, GA）を追加済み：acl_groupsフィールドと
現在のユーザーのグループ（common.CURRENT_USER_GROUPS、暫定値）の積集合が
空でない文書だけを返す。フィルタ条件は必ずこの関数（コード側）が組み立て、
LLM（Agent側）には一切生成させない（設計書 第11章 Guardrail：フィルタ生成の分離）。
"""
import sys

from common import (
    get_search_client,
    embed_text,
    VECTOR_FIELD,
    SEMANTIC_CONFIG_NAME,
    CURRENT_USER_GROUPS,
    validate_group_names,
)
from corpus_data import PRODUCT_GROUPS, PRODUCT_IDS

# Semantic Rankerの@search.reranker_scoreは0.00〜4.00の範囲で返り、公式ドキュメントでは
# おおむね 4=完全に回答, 3=関連するが情報が不足, 2=部分的に関連, 1=わずかに関連,
# 0=無関係、という目安が示されている
# （https://learn.microsoft.com/azure/search/semantic-search-overview）。
# 同ドキュメントは「クエリごとのスコア分布はインフラ側の条件やモデル更新で多少変動しうるため、
# 閾値を細かくしすぎないこと」とも注意しているため、ここでは「2未満＝部分的な関連にも
# 満たない」を足切りラインとする、粗めの閾値にとどめる。低関連度の文書を根拠に
# 無関係な代替回答を生成してしまう問題（README参照）への対策。
MIN_RERANKER_SCORE = 2.0


def build_product_id_filter(product_id: str) -> str:
    """質問文から製品が明確に特定できる場合、semantic searchだけに委ねず、
    コード側で構築したmetadata filterを併用するためのフィルタを組み立てる
    （設計書 第9・11章 Guardrail：Entity抽出結果からのフィルタ生成はコード側が行い、
    LLMには生のフィルタ文字列を書かせない）。値は既知の製品ID一覧
    （corpus_data.PRODUCT_IDS）との完全一致のみ許可し、それ以外は拒否する
    （フィルタインジェクション対策の多重防御。search_agent_tool.SEARCH_TOOL側の
    enumで一次的に制限しているが、ここでも独立に検証する）。"""
    if product_id not in PRODUCT_IDS:
        raise ValueError(f"未知の製品IDです: {product_id!r}。既知の製品ID: {PRODUCT_IDS}")
    escaped = product_id.replace("'", "''")
    return f"product_id eq '{escaped}'"


def build_product_group_filter(product_group: str) -> str:
    """Cross Source連携（設計書 第9章）用：Fabric側から抽出したEntity（商品群名称）を、
    AI Search用の$filterへ機械的に変換する。呼び出し元（cross_source_test.py）が
    Fabric結果からコード側で抽出した値のみを受け取り、LLMが直接filter文字列を
    書くことは無い（設計書 第11章 Guardrail：フィルタ生成の分離）。
    値は既知の商品群一覧（corpus_data.PRODUCT_GROUPS）との完全一致のみ許可し、
    それ以外は拒否する（フィルタインジェクション対策の多重防御）。"""
    if product_group not in PRODUCT_GROUPS:
        raise ValueError(
            f"未知の商品群です: {product_group!r}。既知の商品群: {PRODUCT_GROUPS}"
        )
    escaped = product_group.replace("'", "''")
    return f"product_group eq '{escaped}'"


def combine_filters(*filters: str | None) -> str | None:
    """複数の$filter式をANDで結合する（設計書 第16章 擬似コードのcombine_filters対応）。
    None・空文字は無視する。結合対象が無ければNoneを返す。"""
    parts = [f for f in filters if f]
    if not parts:
        return None
    return " and ".join(f"({p})" for p in parts)


def build_acl_filter(user_groups: list[str]) -> str:
    """acl_groups（Collection(Edm.String)）と user_groups の積集合が空でない文書のみ許可する
    ODataフィルタ式を組み立てる。呼び出し側から渡された値であっても必ず再検証する
    （defense in depth：common.CURRENT_USER_GROUPS は起動時に検証済みだが、
    将来ここが外部入力に差し替わった場合の事故を防ぐため）。"""
    validated = validate_group_names(list(user_groups))
    if not validated:
        # グループが1つも無いユーザーには何も見せない（安全側のデフォルト）
        return "acl_groups/any(g: false)"
    quoted = ",".join(validated)
    return f"acl_groups/any(g: search.in(g, '{quoted}'))"


def search_documents(
    query: str,
    top: int = 5,
    user_groups: list[str] | None = None,
    cross_source_filter: str | None = None,
    product_id: str | None = None,
) -> list[dict]:
    """自然文クエリでHybrid Search + Semantic Rankerを実行し、上位チャンクを返す。
    user_groupsを省略した場合は common.CURRENT_USER_GROUPS（暫定の「現在のユーザー」）を使う。
    ACLフィルタは常に適用され、バイパスするオプションは用意していない。
    cross_source_filterは、呼び出し元がコード側で組み立て済みの追加$filter式
    （例: build_product_group_filter()の戻り値）を渡すためのもの。ACLフィルタと
    AND結合される。LLMからこの引数に生のフィルタ文字列を渡させてはならない
    （設計書 第9・11章 Guardrail）。

    product_idは、質問文から製品が明確に特定できる場合に、semantic searchだけに
    委ねず「その製品の文書だけ」にmetadata filterで絞り込むためのもの
    （build_product_id_filter()経由でAllowlist検証済みの値のみ使う）。
    これを指定すると、ACLで閲覧不可、またはそもそも該当製品の文書が0件の場合、
    他の製品の文書を代替として返すことはない（フィルタで完全に除外されるため）。

    さらに、@search.reranker_score が MIN_RERANKER_SCORE 未満の文書は
    「質問への関連性が低すぎる」と判断し、結果から除外する（低関連度の文書を
    根拠に無関係な代替回答を生成してしまう問題への対策。product_id未指定の
    通常のsemantic search時にも適用される）。"""
    client = get_search_client()
    vector = embed_text(query)
    acl_filter = build_acl_filter(user_groups if user_groups is not None else CURRENT_USER_GROUPS)
    product_id_filter = build_product_id_filter(product_id) if product_id else None
    filter_expr = combine_filters(acl_filter, product_id_filter, cross_source_filter)

    results = client.search(
        search_text=query,
        vector_queries=[
            {"kind": "vector", "k": max(top * 3, 20), "fields": VECTOR_FIELD, "vector": vector}
        ],
        query_type="semantic",
        semantic_configuration_name=SEMANTIC_CONFIG_NAME,
        filter=filter_expr,
        top=top,
        select=["chunk_id", "document_id", "document_title", "content",
                "source_file", "document_type", "page_number", "acl_groups",
                "product_group", "product_id"],
    )

    hits = []
    for r in results:
        reranker_score = r.get("@search.reranker_score")
        if reranker_score is not None and reranker_score < MIN_RERANKER_SCORE:
            continue
        hits.append(
            {
                "chunk_id": r.get("chunk_id"),
                "document_id": r.get("document_id"),
                "document_title": r.get("document_title"),
                "content": r.get("content"),
                "source_file": r.get("source_file"),
                "document_type": r.get("document_type"),
                "page_number": r.get("page_number"),
                "acl_groups": r.get("acl_groups"),
                "product_group": r.get("product_group"),
                "product_id": r.get("product_id"),
                "score": r.get("@search.score"),
                "reranker_score": reranker_score,
            }
        )
    return hits


if __name__ == "__main__":
    # 使い方: python search_tool.py "質問文" [groupA,groupB,...]
    # 第2引数を省略すると common.CURRENT_USER_GROUPS（.envのCURRENT_USER_GROUPS、
    # 既定は all-employees）が使われる。
    q = sys.argv[1] if len(sys.argv) > 1 else "製品A001の品質基準について教えて"
    groups = sys.argv[2].split(",") if len(sys.argv) > 2 else CURRENT_USER_GROUPS

    print(f"クエリ: {q}")
    print(f"ユーザーグループ: {groups}\n")
    hits = search_documents(q, user_groups=groups)
    if not hits:
        print("(ACLフィルタにより表示できる結果がありませんでした)")
    for i, hit in enumerate(hits, start=1):
        loc = f"p.{hit['page_number']}" if hit["page_number"] is not None else "-"
        print(f"[{i}] {hit['document_title']} ({hit['document_type']}, {loc}, "
              f"acl={hit['acl_groups']}, chunk_id={hit['chunk_id']}, "
              f"score={hit['score']:.3f}, reranker={hit['reranker_score']})")
        print(f"    {hit['content'][:120]}...")
