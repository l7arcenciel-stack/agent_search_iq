"""
オントロジーの定義（エンティティ型・バインド・リレーション型）を REST API で流し込む。

【これは Fabric ノートブックではなく、手元の PC で実行するスクリプト】
  実行前に対象テナントへ `az login` しておくこと。

【前提】
  - fabric_notebooks/01_create_delta_tables.py で Delta テーブルが作成済み
  - オントロジーのアイテムが作成済み（POST /v1/workspaces/<ws>/ontologies）

【定義ファイルの構造（実機で確認）】
    definition.json                                        ← ルートは {} のまま
    EntityTypes/<entityTypeId>/definition.json             ← エンティティ型
    EntityTypes/<entityTypeId>/DataBindings/<guid>.json    ← エンティティのバインド
    RelationshipTypes/<relTypeId>/definition.json          ← リレーション型（source/target のみ）
    RelationshipTypes/<relTypeId>/Contextualizations/<guid>.json  ← リレーションのバインド
    .platform

  リレーションのバインドだけ **DataBindings ではなく Contextualizations** という名前で、
  中の形も違う（sourceKeyRefBindings / targetKeyRefBindings）。ここは間違えやすい。

  各ファイルの $schema に公開スキーマのURLが入っている：
    .../fabric/item/ontology/entityType/1.0.0/schema.json
    .../fabric/item/ontology/dataBinding/1.0.0/schema.json
    .../fabric/item/ontology/relationshipType/1.0.0/schema.json
    .../fabric/item/ontology/contextualization/1.0.0/schema.json

【型について】
  valueType に指定できるのは String / Boolean / DateTime / Object / BigInt / Double のみ。
  **Decimal は存在しない。** 金額列を DecimalType で作るとバインド先の型が無く null になる。
  01_create_delta_tables.py が DoubleType を使っているのはこのため。

【updateDefinition は全体置き換え】
  差分更新ではないため、**既存の定義を読み取って保持したうえで**新しいパーツを足して投げる。
  このスクリプトはそれを行う（既に同名のエンティティ型／リレーション型があればスキップする）。

【使い方】
    $env:FABRIC_WORKSPACE_ID            = "<ワークスペースID>"
    $env:FABRIC_LAKEHOUSE_PUBLIC_ID     = "<lh_public のID>"
    $env:FABRIC_LAKEHOUSE_RESTRICTED_ID = "<lh_restricted のID>"
    $env:FABRIC_ONTOLOGY_ID             = "<オントロジーのアイテムID>"
    python fabric_notebooks/03_create_ontology_definition.py --dry-run
    python fabric_notebooks/03_create_ontology_definition.py
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

FABRIC_API = "https://api.fabric.microsoft.com/v1"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/ontology"

# ---------------------------------------------------------------------------
# 作りたいオントロジーの仕様。01_create_delta_tables.py のテーブルと一致させること。
# ---------------------------------------------------------------------------

# どのレイクハウスにバインドするか。**ここが権限分離の実体**。
# エンティティのデータ問い合わせはバインド元のレイクハウス単位で個別に認可されるため、
# レイクハウスを分けることでエンティティ単位の分離が成立する（実機で確認済み）。
#   public     → 全社員グループに読み取りを与えたレイクハウス
#   restricted → 品質チームグループだけに読み取りを与えたレイクハウス
LAKEHOUSE_ENV = {
    "public": "FABRIC_LAKEHOUSE_PUBLIC_ID",
    "restricted": "FABRIC_LAKEHOUSE_RESTRICTED_ID",
}

_DOC_PROPS = [
    ("document_id", "String"), ("product_id", "String"), ("name", "String"),
    ("status", "String"), ("summary", "String"), ("visibility", "String"),
]

# name -> (キー列, [(プロパティ名, valueType), ...], ソーステーブル名, 閲覧範囲)
ENTITIES: dict[str, tuple[str, list[tuple[str, str]], str, str]] = {
    "product": ("product_id", [
        ("product_id", "String"), ("name", "String"), ("status", "String"),
        ("product_group_code", "String"), ("unit_price", "Double"),
    ], "product", "public"),
    "product_group": ("product_group_code", [
        ("product_group_code", "String"), ("name", "String"), ("status", "String"),
    ], "product_group", "public"),
    "site": ("site_code", [
        ("site_code", "String"), ("name", "String"), ("status", "String"),
        ("region_name", "String"),
    ], "site", "public"),
    "quality_document_public": ("document_id", _DOC_PROPS,
                                "quality_document_public", "public"),
    "quality_document_restricted": ("document_id", _DOC_PROPS,
                                    "quality_document_restricted", "restricted"),
}

# エンティティ名 -> (ソーステーブル, タイムスタンプ列, [(時系列プロパティ名, valueType), ...])
# 時系列バインドは1エンティティ型に複数付けられる（静的バインドは1つだけ）。
TIMESERIES: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "product_group": ("product_group_sales_monthly", "period_start", [
        ("sales_amount", "Double"), ("gross_profit", "Double"),
    ]),
    "site": ("site_sales_monthly", "period_start", [
        ("sales_amount", "Double"), ("gross_profit", "Double"),
    ]),
}

# リレーション名 -> (source エンティティ, target エンティティ, エッジテーブル,
#                    source側のキー列, target側のキー列, エッジテーブルの閲覧範囲)
# エッジテーブルも閲覧範囲ごとに分ける。1つにまとめると、中身は見えなくても
# 「限定文書が存在すること」とそのIDが公開側から見えてしまう。
RELATIONSHIPS: dict[str, tuple[str, str, str, str, str, str]] = {
    "belongs_to_product_group": (
        "product", "product_group", "edge_product_product_group",
        "product_id", "product_group_code", "public"),
    "produced_at_site": (
        "product", "site", "edge_product_site",
        "product_id", "site_code", "public"),
    "public_document_describes_product": (
        "quality_document_public", "product", "edge_document_public_product",
        "document_id", "product_id", "public"),
    "restricted_document_describes_product": (
        "quality_document_restricted", "product", "edge_document_restricted_product",
        "document_id", "product_id", "restricted"),
}


# 既にオントロジー上にある要素のid。ポータルで作ったものが入る。
# 同じ名前で別のidを送ると
#   ALMOperationImportFailed: Duplicate Name-Namespace combinations found in entity types
# で拒否される（名前は namespace 内で一意でなければならない）ため、
# **既存のidを必ず引き継ぐ**。
_ID_OVERRIDES: dict[tuple[str, ...], str] = {}


def _id(*parts: str) -> str:
    """定義内で一意な数値文字列のidを決定的に作る。

    既存要素は _ID_OVERRIDES（ポータルが採番したid）を優先する。
    新規要素は名前からハッシュで導出し、再実行しても同じidになるようにする
    （そうしないと再実行のたびに別のエンティティ型として増えてしまう）。
    """
    if parts in _ID_OVERRIDES:
        return _ID_OVERRIDES[parts]
    h = hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()
    return str(int(h[:15], 16))


def load_existing_ids(token: str, workspace_id: str, ontology_id: str) -> int:
    """現在の定義を読み、既存のエンティティ型・プロパティ・リレーション型のidを
    _ID_OVERRIDES へ取り込む。戻り値は取り込んだ件数。"""
    s, h, b = _request(
        "POST",
        f"{FABRIC_API}/workspaces/{workspace_id}/ontologies/{ontology_id}/getDefinition",
        token,
    )
    s, b = _await(token, s, h, b)
    if s >= 400 or not b:
        print("  既存定義を取得できませんでした（新規作成として扱います）")
        return 0

    for part in b.get("definition", {}).get("parts", []):
        path = part["path"]
        if not path.endswith("definition.json") or path == "definition.json":
            continue
        obj = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
        name = obj.get("name")
        if not name:
            continue
        if path.startswith("EntityTypes/"):
            _ID_OVERRIDES[("entity", name)] = obj["id"]
            for p in obj.get("properties", []):
                _ID_OVERRIDES[("prop", name, p["name"])] = p["id"]
            for p in obj.get("timeseriesProperties", []):
                _ID_OVERRIDES[("tsprop", name, p["name"])] = p["id"]
        elif path.startswith("RelationshipTypes/"):
            _ID_OVERRIDES[("rel", name)] = obj["id"]
    return len(_ID_OVERRIDES)


def _b64(obj: dict) -> str:
    return base64.b64encode(
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")


def build_entity(name: str) -> dict:
    key_col, props, _table, _scope = ENTITIES[name]
    eid = _id("entity", name)
    properties = [
        {
            "id": _id("prop", name, pname),
            "name": pname,
            "redefines": None,
            "baseTypeNamespaceType": None,
            "valueType": vtype,
        }
        for pname, vtype in props
    ]
    ts_props = []
    if name in TIMESERIES:
        _t, _ts, tsp = TIMESERIES[name]
        ts_props = [
            {
                "id": _id("tsprop", name, pname),
                "name": pname,
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": vtype,
            }
            for pname, vtype in tsp
        ]
    return {
        "$schema": f"{SCHEMA}/entityType/1.0.0/schema.json",
        "id": eid,
        "namespace": "usertypes",
        "baseEntityTypeId": None,
        "name": name,
        "entityIdParts": [_id("prop", name, key_col)],
        "displayNamePropertyId": _id("prop", name, "name"),
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": properties,
        "timeseriesProperties": ts_props,
        "untypedProperties": [],
    }


def build_static_binding(name: str, workspace_id: str, lakehouses: dict[str, str]) -> dict:
    _key, props, table, scope = ENTITIES[name]
    lakehouse_id = lakehouses[scope]
    return {
        "$schema": f"{SCHEMA}/dataBinding/1.0.0/schema.json",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"static::{name}")),
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": [
                {"sourceColumnName": p, "targetPropertyId": _id("prop", name, p)}
                for p, _v in props
            ],
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": table,
                "sourceSchema": None,
            },
        },
    }


def build_timeseries_binding(name: str, workspace_id: str, lakehouses: dict[str, str]) -> dict:
    table, ts_col, tsp = TIMESERIES[name]
    key_col, _props, _t, scope = ENTITIES[name]
    lakehouse_id = lakehouses[scope]
    # エンティティのキー列も一緒に渡す。これが無いと、どの行がどのエンティティの
    # 時系列なのかを結び付けられない。
    bindings = [{"sourceColumnName": key_col, "targetPropertyId": _id("prop", name, key_col)}]
    bindings += [
        {"sourceColumnName": p, "targetPropertyId": _id("tsprop", name, p)}
        for p, _v in tsp
    ]
    return {
        "$schema": f"{SCHEMA}/dataBinding/1.0.0/schema.json",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ts::{name}::{table}")),
        "dataBindingConfiguration": {
            "dataBindingType": "TimeSeries",
            "timestampColumnName": ts_col,
            "propertyBindings": bindings,
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": table,
                "sourceSchema": None,
            },
        },
    }


def build_relationship(name: str) -> dict:
    src, tgt, _edge, _sk, _tk, _scope = RELATIONSHIPS[name]
    return {
        "$schema": f"{SCHEMA}/relationshipType/1.0.0/schema.json",
        "namespace": "usertypes",
        "id": _id("rel", name),
        "name": name,
        "namespaceType": "Custom",
        "source": {"entityTypeId": _id("entity", src)},
        "target": {"entityTypeId": _id("entity", tgt)},
    }


def build_contextualization(name: str, workspace_id: str, lakehouses: dict[str, str]) -> dict:
    src, tgt, edge, src_col, tgt_col, scope = RELATIONSHIPS[name]
    src_key, _p, _t, _s1 = ENTITIES[src]
    tgt_key, _p2, _t2, _s2 = ENTITIES[tgt]
    lakehouse_id = lakehouses[scope]
    return {
        "$schema": f"{SCHEMA}/contextualization/1.0.0/schema.json",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctx::{name}")),
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_id,
            "sourceTableName": edge,
            "sourceSchema": None,
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [
            {"sourceColumnName": src_col, "targetPropertyId": _id("prop", src, src_key)}
        ],
        "targetKeyRefBindings": [
            {"sourceColumnName": tgt_col, "targetPropertyId": _id("prop", tgt, tgt_key)}
        ],
    }


def build_parts(workspace_id: str, lakehouses: dict[str, str]) -> list[dict]:
    parts = [
        {"path": "definition.json", "payload": _b64({}), "payloadType": "InlineBase64"}
    ]

    def add(path: str, obj: dict) -> None:
        parts.append({"path": path, "payload": _b64(obj), "payloadType": "InlineBase64"})

    for name in ENTITIES:
        eid = _id("entity", name)
        add(f"EntityTypes/{eid}/definition.json", build_entity(name))
        b = build_static_binding(name, workspace_id, lakehouses)
        add(f"EntityTypes/{eid}/DataBindings/{b['id']}.json", b)
        if name in TIMESERIES:
            tb = build_timeseries_binding(name, workspace_id, lakehouses)
            add(f"EntityTypes/{eid}/DataBindings/{tb['id']}.json", tb)

    for name in RELATIONSHIPS:
        rid = _id("rel", name)
        add(f"RelationshipTypes/{rid}/definition.json", build_relationship(name))
        c = build_contextualization(name, workspace_id, lakehouses)
        add(f"RelationshipTypes/{rid}/Contextualizations/{c['id']}.json", c)

    return parts


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

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
            return resp.status, dict(resp.headers), (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, dict(e.headers), json.loads(raw)
        except ValueError:
            return e.code, dict(e.headers), {"raw": raw[:1500]}


def _await(token: str, status: int, headers: dict, body):
    """202 の非同期オペレーションを待つ。完了なら result を返す。"""
    if status not in (202,):
        return status, body
    loc = headers.get("Location") or headers.get("location")
    for _ in range(40):
        time.sleep(4)
        _s, _h, r = _request("GET", loc, token)
        st = (r or {}).get("status")
        if st == "Succeeded":
            _s2, _h2, res = _request("GET", loc.rstrip("/") + "/result", token)
            return 200, res
        if st in ("Failed", "Undefined"):
            return 500, r
    return 504, {"error": "timeout"}


def _required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"環境変数 {name} が未設定です。冒頭の docstring を参照してください。")
    return v


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    workspace_id = _required("FABRIC_WORKSPACE_ID")
    lakehouses = {scope: _required(env) for scope, env in LAKEHOUSE_ENV.items()}

    if not dry_run:
        token = _token()
        ontology_id = _required("FABRIC_ONTOLOGY_ID")
        n = load_existing_ids(token, workspace_id, ontology_id)
        print(f"既存のidを {n} 件引き継ぎました")

    parts = build_parts(workspace_id, lakehouses)

    if dry_run:
        for p in parts:
            print("=" * 72)
            print(p["path"])
            print("=" * 72)
            print(base64.b64decode(p["payload"]).decode("utf-8"))
        print(f"\nparts: {len(parts)}")
        return

    print(f"オントロジーの定義を置き換えます（parts={len(parts)}）")
    print("  ※ updateDefinition は全体置き換え。ポータルで加えた変更は失われる。")
    s, h, b = _request(
        "POST",
        f"{FABRIC_API}/workspaces/{workspace_id}/ontologies/{ontology_id}/updateDefinition",
        token,
        {"definition": {"parts": parts}},
    )
    s, b = _await(token, s, h, b)
    if s >= 400:
        print(json.dumps(b, ensure_ascii=False, indent=2)[:2000])
        sys.exit(1)

    print("  OK")
    print("\n次の手順:")
    print("  1. ポータルを Ctrl+Shift+R で強制リロードし（エンティティ一覧がキャッシュされるため）、")
    print("     各エンティティの『インスタンス』タブでデータを確認する")
    print("     （バインド保存時に取り込みが走るので、初回は手動リフレッシュ不要だった）")
    print("  2. 権限はレイクハウス単位で付与する（エンティティ単位の権限は無い）:")
    print("       lh_public     → 全社員グループ")
    print("       lh_restricted → 品質チームグループのみ")
    print("     いずれも『すべての SQL エンドポイント データを読み取る』と")
    print("     『すべての Apache Spark を読み取り…』にチェックが必要")
    print("  3. オントロジー自体への読み取りも両グループに付与する")


if __name__ == "__main__":
    main()
