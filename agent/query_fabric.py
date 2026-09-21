"""
Fabric Semantic Model への構造化データ照会（設計書でいう Scenario A）。

設計書 第6・7・11章の方針:
  - LLMにはDAX文字列そのものを書かせず、「どのメジャーを、何で集計し
    （group_by）、何でフィルタするか（filters）」という構造化されたQuery Plan
    （JSON）だけを出させる。テーブル名はLLMには意識させない
    （fabric_schema.pyのAllowlistがメジャー・列ごとに保持している）。
  - Query Planはfabric_schema.pyのAllowlist（MEASURES/COLUMNS）に対して検証する。
    メジャー名・列名は、LLMが渡した文字列をAllowlist辞書のキーとして検索し、
    一致した場合のみ「Allowlist側に登録されているテーブル名・DAX識別子」を
    採用する（LLMの生文字列をそのままDAXへ埋め込むことは識別子に関しては行わない）。
  - フィルタの「値」（識別子ではなくデータそのもの）はDAX文字列リテラルとして
    エスケープしたうえで埋め込む。
  - 検証を1つでも通らないQuery Planは、DAXを組み立てずに例外を送出する
    （fail closed）。

異なるテーブル（全拠点売上_全期間・日付）の列とメジャーを同じQuery Planに
混在させても、SUMMARIZECOLUMNSがモデルのリレーションを自動的に辿って解決する
（例: group_by=["年月表示"]（日付テーブル）+ measures=["売上金額"]
（全拠点売上_全期間テーブル）は正しく集計される）。

agent_search_starter/query_fabric.pyと同じロジック（Hosted Agent移行に伴う変更なし）。
Tool呼び出し回数の上限は、Hosted Agent移行後はagent_framework.toolデコレータの
max_invocations引数で課す（main.py参照。Prompt Agent版のような手動カウンタは不要になった）。
ここでは1回の照会で返す最大行数のみGuardrailとして持つ。
"""
import json
import sys

from fabric_client import execute_dax
from fabric_schema import MEASURES, COLUMNS

# Guardrail: 1回の照会で返す最大行数（ExecuteQueries自体の上限は10万行/100万値だが、
# Agentに渡すEvidenceとしては小さく絞っておく）
MAX_ROWS = 200


class QueryPlanError(ValueError):
    """Query PlanがAllowlistに違反している場合に送出する。"""


def _escape_dax_string(value) -> str:
    return str(value).replace('"', '""')


def _resolve(name, allowlist: dict, kind: str) -> dict:
    if name not in allowlist:
        raise QueryPlanError(
            f"許可されていない{kind}です: {name!r}。許可済み: {sorted(allowlist.keys())}"
        )
    return allowlist[name]


def measure_result_key(display_name: str) -> str:
    """query_fabric()が返すrows(dict)の中で、指定メジャーの値が入っているキー名を返す。
    ExecuteQueriesの結果は "[メジャー名]" という形式のキーで返る（DAXの
    "Name", Expression ペアで指定した名前がそのままキーになる）。
    Cross Source連携（設計書 第9章）でFabric結果からEntityをコード側で機械的に
    抽出する際、キー名の文字列をハードコードせずAllowlist経由で解決するために使う。"""
    m = _resolve(display_name, MEASURES, "メジャー")
    return f"[{m['name']}]"


def column_result_key(display_name: str) -> str:
    """query_fabric()が返すrows(dict)の中で、指定列の値が入っているキー名を返す。
    ExecuteQueriesの結果は "テーブル名[列名]" という形式のキーで返る。"""
    c = _resolve(display_name, COLUMNS, "列")
    return f"{c['table']}[{c['name']}]"


def validate_query_plan(plan: dict) -> dict:
    """Query Plan(dict)をAllowlistに対して検証し、実際のDAX識別子（テーブル名込み）に
    解決したdictを返す。1つでも未許可の項目があればQueryPlanErrorを送出する。"""
    measures = plan.get("measures") or []
    if not measures:
        raise QueryPlanError("measuresは1つ以上指定してください。")
    resolved_measures = [_resolve(m, MEASURES, "メジャー") for m in measures]

    group_by = plan.get("group_by") or []
    resolved_group_by = [_resolve(c, COLUMNS, "列") for c in group_by]

    filters = plan.get("filters") or []
    resolved_filters = []
    for f in filters:
        col = _resolve(f.get("column"), COLUMNS, "列(filters)")
        op = f.get("operator", "eq")
        if op != "eq":
            raise QueryPlanError(
                f"サポートされていない演算子です: {op!r}（現状 'eq' のみ許可）"
            )
        if "value" not in f:
            raise QueryPlanError(f"filtersにvalueがありません: {f!r}")
        resolved_filters.append({"column": col, "value": f["value"]})

    top_n = plan.get("top_n") or MAX_ROWS
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        raise QueryPlanError(f"top_nは整数で指定してください: {top_n!r}")
    top_n = max(1, min(top_n, MAX_ROWS))

    return {
        "measures": resolved_measures,
        "group_by": resolved_group_by,
        "filters": resolved_filters,
        "top_n": top_n,
    }


def build_dax_query(resolved: dict) -> str:
    """検証済みQuery Plan（Allowlist解決後の識別子のみを含む）からDAXを組み立てる。
    識別子（テーブル名・列名・メジャー名）はすべてfabric_schema.py由来の信頼済み文字列、
    フィルタの値のみエスケープ済み文字列リテラルとして埋め込む。

    SUMMARIZECOLUMNSの引数順序は
      SUMMARIZECOLUMNS(GroupBy_Column1, ..., FilterTable1, ..., "Name1", Expression1, ...)
    であり、FilterTable（絞り込み条件）はGroup Byの列より後・Name/Expression（メジャー）の
    ペアより前に置く必要がある（公式ドキュメントの引数仕様）。この順序を誤ると
    「引数Nには列名が必要です」といったエラーになるため、必ずこの順で組み立てる。"""
    group_by_terms = [f"'{c['table']}'[{c['name']}]" for c in resolved["group_by"]]

    filter_terms = []
    for f in resolved["filters"]:
        col = f["column"]
        value_literal = f'"{_escape_dax_string(f["value"])}"'
        filter_terms.append(
            f"FILTER(ALL('{col['table']}'[{col['name']}]), "
            f"'{col['table']}'[{col['name']}] = {value_literal})"
        )

    measure_terms = [
        f"\"{m['name']}\", '{m['table']}'[{m['name']}]" for m in resolved["measures"]
    ]

    summarize_args = ",\n    ".join(group_by_terms + filter_terms + measure_terms)
    dax = f"EVALUATE\nTOPN({resolved['top_n']}, SUMMARIZECOLUMNS(\n    {summarize_args}\n))"
    return dax


def query_fabric(plan: dict, access_token: str | None = None) -> dict:
    """Query Plan(dict) → 検証 → DAX組み立て → ExecuteQueries呼び出し →
    行データに整形、までを一括で行う。戻り値には生成したDAXも含める
    （Evidence/Citation設計・監査ログ用に、Agentの回答根拠として何を実行したか
    後から追跡できるようにするため）。

    access_tokenを渡した場合、その値がそのままfabric_client.execute_dax()の
    Authorizationヘッダーに使われる（OBOで取得した実ユーザー本人のPower BIトークンを
    渡すことで、そのユーザーのRLS（行レベルセキュリティ）が適用された結果を取得できる。
    obo_smoke_test.py参照）。省略時は従来通りDefaultAzureCredentialを使う。"""
    resolved = validate_query_plan(plan)
    dax = build_dax_query(resolved)
    raw = execute_dax(dax, access_token=access_token)

    try:
        rows = raw["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError, TypeError):
        rows = []

    return {"dax": dax, "row_count": len(rows), "rows": rows}


if __name__ == "__main__":
    # 使い方: python agent/query_fabric.py '{"measures": ["売上金額"], "group_by": ["地域名称"]}'
    # 引数省略時は「地域別の売上金額」で試す
    if len(sys.argv) > 1:
        plan = json.loads(sys.argv[1])
    else:
        plan = {"measures": ["売上金額"], "group_by": ["地域名称"]}

    print(f"Query Plan: {plan}")
    try:
        resolved = validate_query_plan(plan)
    except QueryPlanError as e:
        print(f"Query Planの検証に失敗しました: {e}")
        sys.exit(1)

    dax = build_dax_query(resolved)
    print(f"\n生成されたDAX:\n{dax}\n")

    try:
        result = query_fabric(plan)
    except RuntimeError as e:
        print(f"ExecuteQueries呼び出しに失敗しました: {e}")
        sys.exit(1)

    print(f"件数: {result['row_count']}")
    for row in result["rows"]:
        print(row)
