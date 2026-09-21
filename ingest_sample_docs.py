"""
AI Search のインデックスを作り、corpus_data.py の架空製品の文書を投入する。

    python ingest_sample_docs.py --dry-run     # 投入予定の文書と ACL を表示するだけ
    python ingest_sample_docs.py               # インデックスが無ければ作り、文書を upsert
    python ingest_sample_docs.py --recreate    # インデックスを作り直してから投入

必要な環境変数（common.py が読む）:
    AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_API_KEY / AZURE_SEARCH_INDEX_NAME
    AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    AI_FOUNDRY_PROJECT_ENDPOINT（import 時に必須。投入では使わない）
    QUALITY_TEAM_GROUP_ID / ALL_EMPLOYEES_GROUP_ID   ← Entra の実グループの Object ID

【最重要：グループIDは投入時点の値が文書に焼き付く】
  各文書の acl_groups には、**投入した瞬間の** QUALITY_TEAM_GROUP_ID / ALL_EMPLOYEES_GROUP_ID が
  値として入る。グループを作り直した（別テナントへ移った）ら、**--recreate で作り直す**こと。
  古い値のまま残すと、エラーにならずに「誰が聞いても閲覧できません」になる。

  また、グループIDが文字列スラッグ（"quality-team" / "all-employees"）のまま投入すると、
  OBO で解決したユーザーのグループ（GUID）と一致せず、同じく誰にも見えなくなる。
  このスクリプトは **GUID でなければ投入を拒否する**（--allow-slugs で解除。ローカル検証用）。

【インデックスの形】search_tool.py が前提にしているフィールドに合わせてある。
  chunk_id(キー) / document_id / document_title / content / source_file / document_type /
  page_number / acl_groups(Collection, フィルタ可) / product_group / product_id /
  content_vector(1536次元) ／ セマンティック構成 default-semantic

【元テナントでの注意】既存エージェントとインデックスを共有しないこと。
  AZURE_SEARCH_INDEX_NAME を**既存と別名**にして実行する（既存インデックスを上書きすると凍結デモが壊れる）。
"""
import argparse
import re
import sys

from common import (
    ALL_EMPLOYEES_GROUP_ID,
    EMBEDDING_DIMENSIONS,
    QUALITY_TEAM_GROUP_ID,
    SEARCH_INDEX_NAME,
    SEMANTIC_CONFIG_NAME,
    VECTOR_FIELD,
    embed_text,
    get_search_client,
    get_search_index_client,
)
from corpus_data import (
    PRODUCTS,
    acl_groups_for_document_id,
    product_group_for_document_id,
    product_id_for_document_id,
)

_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def build_documents() -> list[dict]:
    docs = []
    for p in PRODUCTS:
        pid, name = p["id"], p["name"]

        doc_id = f"quality_report_{pid}-0"
        checks = "\n".join(f"・{k}: {v}" for k, v in p["checks"])
        docs.append({
            "document_id": doc_id,
            "document_title": f"{name}（{pid}）品質報告書",
            "document_type": "品質報告書",
            "source_file": f"quality_report_{pid}.{p['quality_format']}",
            "content": (f"製品 {pid} {name}（{p['domain']}）の品質基準と検査結果。\n"
                        f"品質基準:\n{checks}\n直近の状況: {p['issue']}"),
        })

        doc_id = f"spec_{pid}"
        spec = "\n".join(f"・{k}: {v}{'' if u == '-' else ' ' + u}" for k, v, u in p["spec"])
        docs.append({
            "document_id": doc_id,
            "document_title": f"{name}（{pid}）仕様書",
            "document_type": "仕様書",
            "source_file": f"spec_{pid}.txt",
            "content": f"製品 {pid} {name}（{p['domain']}）の主要仕様。\n{spec}",
        })

    for d in docs:
        d["chunk_id"] = d["document_id"]
        d["page_number"] = None
        d["acl_groups"] = acl_groups_for_document_id(d["document_id"])
        d["product_id"] = product_id_for_document_id(d["document_id"])
        d["product_group"] = product_group_for_document_id(d["document_id"])
    return docs


def build_index():
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )
    S = SearchFieldDataType
    fields = [
        SimpleField(name="chunk_id", type=S.String, key=True),
        SimpleField(name="document_id", type=S.String, filterable=True),
        SearchableField(name="document_title", type=S.String, analyzer_name="ja.microsoft"),
        SearchableField(name="content", type=S.String, analyzer_name="ja.microsoft"),
        SimpleField(name="source_file", type=S.String),
        SimpleField(name="document_type", type=S.String, filterable=True, facetable=True),
        SimpleField(name="page_number", type=S.Int32),
        SimpleField(name="acl_groups", type=S.Collection(S.String), filterable=True),
        SimpleField(name="product_group", type=S.String, filterable=True, facetable=True),
        SimpleField(name="product_id", type=S.String, filterable=True, facetable=True),
        SearchField(name=VECTOR_FIELD, type=S.Collection(S.Single), searchable=True,
                    vector_search_dimensions=EMBEDDING_DIMENSIONS,
                    vector_search_profile_name="default-vector-profile"),
    ]
    return SearchIndex(
        name=SEARCH_INDEX_NAME,
        fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="default-hnsw")],
            profiles=[VectorSearchProfile(name="default-vector-profile",
                                          algorithm_configuration_name="default-hnsw")]),
        semantic_search=SemanticSearch(configurations=[SemanticConfiguration(
            name=SEMANTIC_CONFIG_NAME,
            prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="document_title"),
                content_fields=[SemanticField(field_name="content")]))]),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recreate", action="store_true", help="インデックスを削除して作り直す")
    ap.add_argument("--allow-slugs", action="store_true",
                    help="グループIDが GUID でなくても投入する（ローカル検証用）")
    a = ap.parse_args()

    bad = [n for n, v in (("QUALITY_TEAM_GROUP_ID", QUALITY_TEAM_GROUP_ID),
                          ("ALL_EMPLOYEES_GROUP_ID", ALL_EMPLOYEES_GROUP_ID)) if not _GUID.match(v)]
    if bad and not a.allow_slugs:
        sys.exit(f"{bad} が GUID ではありません（{QUALITY_TEAM_GROUP_ID!r}, {ALL_EMPLOYEES_GROUP_ID!r}）。\n"
                 "スラッグのまま投入すると、OBO で解決したユーザーと一致せず誰にも見えなくなる。\n"
                 "Entra の実グループの Object ID を設定すること。")

    docs = build_documents()
    print(f"インデックス: {SEARCH_INDEX_NAME}   文書: {len(docs)} 件")
    by_acl: dict[str, list[str]] = {}
    for d in docs:
        key = ("品質チーム限定" if d["acl_groups"] == [QUALITY_TEAM_GROUP_ID]
               else "全社公開" if d["acl_groups"] == [ALL_EMPLOYEES_GROUP_ID]
               else str(d["acl_groups"]))
        by_acl.setdefault(key, []).append(d["document_id"])
    for k, v in by_acl.items():
        print(f"  {k:10s} {len(v):2d} 件: {', '.join(v)}")
    if a.dry_run:
        return

    idx = get_search_index_client()
    names = set(idx.list_index_names())
    if a.recreate and SEARCH_INDEX_NAME in names:
        idx.delete_index(SEARCH_INDEX_NAME)
        print("  既存のインデックスを削除")
        names.discard(SEARCH_INDEX_NAME)
    if SEARCH_INDEX_NAME not in names:
        idx.create_index(build_index())
        print("  インデックスを作成")
    else:
        print("  既存のインデックスに upsert（グループIDを変えた場合は --recreate を使う）")

    for d in docs:
        d[VECTOR_FIELD] = embed_text(f"{d['document_title']}\n{d['content']}")
    res = get_search_client().merge_or_upload_documents(documents=docs)
    failed = [r.key for r in res if not r.succeeded]
    print(f"  投入: {len(docs) - len(failed)} 件成功" + (f" / 失敗 {failed}" if failed else ""))


if __name__ == "__main__":
    main()
