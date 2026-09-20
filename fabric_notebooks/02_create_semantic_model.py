"""
Direct Lake のセマンティックモデルを Fabric REST API で作成する。

【これは Fabric ノートブックではなく、手元の PC で実行するスクリプト】
  fabric_notebooks/ に置いてあるのは「Fabric のリソースを作るコード」をまとめるため。
  01_create_delta_tables.py が Fabric 上で動くのに対し、これは手元で az のトークンを使って
  REST API を叩く。実行前に対象テナントへ `az login` しておくこと。

【なぜポータルではなく API か】
  ポータルの「新しいセマンティックモデル」でも Direct Lake のモデルは作れる。
  こちらを用意したのは、モデルの定義（テーブル・リレーション・メジャー）が
  TMDL としてコードに残り、**元テナントへ反映するときに同じものを再現できる**ため。
  ポータルで作ると手作業の再現になり、メジャー定義の写し間違いが起きる。

【Direct Lake であることの担保】
  各テーブルのパーティションを `mode: directLake` とし、SQL エンドポイント経由の
  `expressionSource: DatabaseQuery` を参照させている。インポートモードにはならない。
  作成後に必ず、ポータルの設定でストレージモードが「Direct Lake」と出ることを確認すること。

【使い方】
    # 環境変数で対象を指定（実値をコードに埋め込まない）
    $env:FABRIC_WORKSPACE_ID   = "<ワークスペースID>"
    $env:FABRIC_LAKEHOUSE_ID   = "<レイクハウスID>"
    $env:FABRIC_SQL_ENDPOINT   = "<...datawarehouse.fabric.microsoft.com>"
    $env:FABRIC_SQL_DATABASE_ID= "<SQLエンドポイントのID>"
    python fabric_notebooks/02_create_semantic_model.py

    --dry-run を付けると、POST せずに生成した TMDL を標準出力に出すだけ。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

FABRIC_API = "https://api.fabric.microsoft.com/v1"

MODEL_NAME = os.environ.get("FABRIC_SEMANTIC_MODEL_NAME", "sm_agent_search_iq")

# 列名 -> TMDL の dataType。01_create_delta_tables.py の Spark スキーマと一致させること。
#   StringType    -> string
#   DoubleType    -> double
#   TimestampType -> dateTime
TABLES: dict[str, list[tuple[str, str]]] = {
    "product": [
        ("product_id", "string"), ("name", "string"), ("status", "string"),
        ("product_group_code", "string"), ("unit_price", "double"),
    ],
    "product_group": [
        ("product_group_code", "string"), ("name", "string"), ("status", "string"),
    ],
    "site": [
        ("site_code", "string"), ("name", "string"), ("status", "string"),
        ("region_name", "string"),
    ],
    # 品質文書のテーブルは**意図的にモデルへ含めない**。
    # 文書は AI Search（全文検索）と Fabric IQ（オントロジー）が扱う担当で、
    # DAX 経路から本文を引けるようにすると、同じ情報に3つの経路ができて
    # 「どの権限で取得した結果か」が曖昧になる。
    # さらに、このモデルは lh_public だけを向いているため、限定文書は
    # そもそもここからは参照できない（権限分離の設計上そうしている）。
    "edge_product_product_group": [
        ("edge_id", "string"), ("product_id", "string"), ("product_group_code", "string"),
    ],
    "edge_product_site": [
        ("edge_id", "string"), ("product_id", "string"), ("site_code", "string"),
    ],
    "edge_document_public_product": [
        ("edge_id", "string"), ("document_id", "string"), ("product_id", "string"),
    ],
    "product_group_sales_monthly": [
        ("product_group_code", "string"), ("period_start", "dateTime"),
        ("sales_amount", "double"), ("gross_profit", "double"),
    ],
    "site_sales_monthly": [
        ("site_code", "string"), ("period_start", "dateTime"),
        ("sales_amount", "double"), ("gross_profit", "double"),
    ],
}

# このモデルは **lh_public** のテーブルだけを対象にする。
# lh_restricted（品質チーム限定）は Fabric IQ のオントロジー経由でのみ参照し、
# セマンティックモデル（DAX 経路）からは到達できない構成にしている。

# メジャー。売上テーブルが2つあるため「売上金額」を1つにすると集計軸が曖昧になる。
# 商品群側と拠点側で名前を分け、どちらを見ているかが質問文からも分かるようにする。
# ここで決めた名前は fabric_schema.py の MEASURES と一致させること。
MEASURES: dict[str, list[tuple[str, str, str]]] = {
    "product_group_sales_monthly": [
        ("商品群売上金額", "SUM(product_group_sales_monthly[sales_amount])", "#,0"),
        ("商品群粗利", "SUM(product_group_sales_monthly[gross_profit])", "#,0"),
    ],
    "site_sales_monthly": [
        ("拠点売上金額", "SUM(site_sales_monthly[sales_amount])", "#,0"),
        ("拠点粗利", "SUM(site_sales_monthly[gross_profit])", "#,0"),
    ],
}

# (fromTable, fromColumn, toTable, toColumn) — 多 → 1 の向き
RELATIONSHIPS = [
    ("product_group_sales_monthly", "product_group_code", "product_group", "product_group_code"),
    ("site_sales_monthly", "site_code", "site", "site_code"),
    ("edge_product_product_group", "product_id", "product", "product_id"),
    ("edge_product_product_group", "product_group_code", "product_group", "product_group_code"),
    ("edge_product_site", "product_id", "product", "product_id"),
    ("edge_product_site", "site_code", "site", "site_code"),
]


def build_table(table: str, columns: list[tuple[str, str]]) -> dict:
    """TMSL のテーブル定義。partition の mode=directLake がインポート化を防ぐ肝。"""
    cols = []
    for name, dtype in columns:
        col = {
            "name": name,
            "dataType": dtype,
            "sourceColumn": name,
            "summarizeBy": "none",
        }
        if dtype == "dateTime":
            col["formatString"] = "yyyy-mm-dd"
        cols.append(col)

    t: dict = {
        "name": table,
        "columns": cols,
        "partitions": [{
            "name": table,
            "mode": "directLake",
            "source": {
                "type": "entity",
                "entityName": table,
                "expressionSource": "DatabaseQuery",
            },
        }],
    }
    measures = [
        {"name": mname, "expression": expr, "formatString": fmt}
        for mname, expr, fmt in MEASURES.get(table, [])
    ]
    if measures:
        t["measures"] = measures
    return t


def build_model_bim(sql_endpoint: str, sql_database_id: str) -> dict:
    return {
        "name": MODEL_NAME,
        "compatibilityLevel": 1604,
        "model": {
            "culture": "ja-JP",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "ja-JP",
            "discourageImplicitMeasures": True,
            "expressions": [{
                "name": "DatabaseQuery",
                "kind": "m",
                "expression": [
                    "let",
                    f'    database = Sql.Database("{sql_endpoint}", "{sql_database_id}")',
                    "in",
                    "    database",
                ],
                "annotations": [
                    {"name": "PBI_IncludeFutureArtifacts", "value": "False"}
                ],
            }],
            "tables": [build_table(t, c) for t, c in TABLES.items()],
            "relationships": [
                {
                    "name": str(uuid.uuid4()),
                    "fromTable": ft, "fromColumn": fc,
                    "toTable": tt, "toColumn": tc,
                }
                for ft, fc, tt, tc in RELATIONSHIPS
            ],
            "annotations": [
                {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"}
            ],
        },
    }


def build_definition(sql_endpoint: str, sql_database_id: str) -> list[dict]:
    """semanticModels API に渡す parts。TMDL ではなく TMSL（model.bim）を使う。

    TMDL 形式（definition/*.tmdl）で投げると
      Workload_FailedToParseFile: Cannot read 'model.bim'. Missing required artifact 'model.bim'.
    になる（実機で確認）。この API は model.bim を要求する。
    """
    def part(path: str, text: str) -> dict:
        return {
            "path": path,
            "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "payloadType": "InlineBase64",
        }

    bim = build_model_bim(sql_endpoint, sql_database_id)
    return [
        part("model.bim", json.dumps(bim, ensure_ascii=False, indent=2)),
        part("definition.pbism", json.dumps({"version": "1.0", "settings": {}}, indent=2)),
    ]


def _token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://api.fabric.microsoft.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=(os.name == "nt"),
    )
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"トークンを取得できませんでした。az login を確認してください: {out.stderr[:300]}")
    return out.stdout.strip()


def _request(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, dict(e.headers), json.loads(raw)
        except ValueError:
            return e.code, dict(e.headers), {"raw": raw[:2000]}


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"環境変数 {name} が未設定です。冒頭の docstring を参照してください。")
    return v


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    workspace_id = _required("FABRIC_WORKSPACE_ID")
    sql_endpoint = _required("FABRIC_SQL_ENDPOINT")
    sql_database_id = _required("FABRIC_SQL_DATABASE_ID")

    parts = build_definition(sql_endpoint, sql_database_id)

    if dry_run:
        for p in parts:
            print("=" * 70)
            print(p["path"])
            print("=" * 70)
            print(base64.b64decode(p["payload"]).decode("utf-8"))
        return

    token = _token()
    body = {
        "displayName": MODEL_NAME,
        "description": "Direct Lake model over lh_agent_search_iq (agent_search_iq demo)",
        "definition": {"parts": parts},
    }

    print(f"セマンティックモデルを作成します: {MODEL_NAME}  (parts={len(parts)})")
    status, headers, resp = _request(
        "POST", f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels", token, body
    )
    print(f"  HTTP {status}")

    if status == 202:
        op = headers.get("Location") or headers.get("location")
        for _ in range(40):
            time.sleep(5)
            s, _h, r = _request("GET", op, token)
            state = (r or {}).get("status")
            print(f"  ... {state}")
            if state in ("Succeeded", "Failed", "Undefined"):
                if state != "Succeeded":
                    print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
                    sys.exit(1)
                break
    elif status not in (200, 201):
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:2000])
        sys.exit(1)

    s, _h, models = _request(
        "GET", f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels", token
    )
    for m in (models or {}).get("value", []):
        if m.get("displayName") == MODEL_NAME:
            print("\n作成しました。")
            print(f"  FABRIC_DATASET_ID   = {m.get('id')}")
            print(f"  FABRIC_WORKSPACE_ID = {workspace_id}")
            print("\n次の手順:")
            print("  1. ポータルでストレージモードが『Direct Lake』であることを確認する")
            print("  2. メジャー名を fabric_schema.py の MEASURES に反映する")
            print("  3. RLS を付ける場合は『モデルの管理 > セキュリティのロール』で設定する")
            return
    print("作成は完了しましたが、一覧から該当モデルを見つけられませんでした。ポータルで確認してください。")


if __name__ == "__main__":
    main()
