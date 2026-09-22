"""
Fabric のワークスペース2つを作って容量に割り当て、レイクハウス2つとオントロジーのアイテムを作る。

    $env:FABRIC_CAPACITY_NAME = "<容量の表示名>"      # 例: F2 の容量名、または試用容量名
    python tenant_setup/30_fabric_items.py --dry-run
    python tenant_setup/30_fabric_items.py

このあと:
  1. fabric_notebooks/01_create_delta_tables.py を Fabric ノートブックで2回実行（lh_public / lh_restricted）
  2. fabric_notebooks/02_create_semantic_model.py
  3. fabric_notebooks/03_create_ontology_definition.py

【作るもの】（名前は環境変数で変更可）
  FABRIC_WORKSPACE_NAME             ws-agent-search-iq
    lh_public                       ← 全社公開。全社員グループにアイテム共有
    FABRIC_ONTOLOGY_NAME            ont_agent_search_iq  ← 付随の Lakehouse / GraphModel が自動で作られる
  FABRIC_RESTRICTED_WORKSPACE_NAME  ws-agent-search-iq-restricted
    lh_restricted                   ← 品質チーム限定。grp-quality-team をワークスペースの閲覧者にする

  限定データを別ワークスペースに置く理由：デモユーザーは Fabric IQ を使うために
  ws-agent-search-iq の閲覧者にする必要があり、閲覧者には同じワークスペースの
  全レイクハウスの SQL エンドポイントを読む権限が付く（operations_log §20）。

【元テナントで使う場合】既存ワークスペースには作らず、**新しいワークスペース名**で作る。
  既存容量への割り当てには容量の管理者（または共同作成者）権限が要る。

【実機でわかったこと（新テナント、2026-09-21）】
  - 日本語を含む JSON を curl にシェル経由で渡すと文字化けして InvalidInput になった。
    このスクリプトは UTF-8 のバイト列を直接送るので問題ない。
  - grp-quality-team の ID は Graph で引く。セキュリティ既定値で Graph が使えないときは
    QUALITY_TEAM_GROUP_ID を設定する（無ければ閲覧者の付与だけスキップ）。
  - アイテムの作成は /items に "type" をボディで渡す（クエリ文字列の itemType では 400）。
  - GET /ontologies は 200 を返すが中身が空で、作成済みでも一覧に出てこない。
    存在確認は GET /items で行う（このスクリプトもそうしている）。
  - 容量が停止中だと、一覧は取れるのにアイテム個別の API だけ 404 になる。
    割り当て先の容量は Active にしておく。
  - レイクハウスの OneLake セキュリティは有効にしない（オントロジーのバインド対象から外れる）。
    API で作ると既定で無効。

【冪等】同名のものがあれば作らない。容量の割り当ては、違う容量に載っていれば付け替える。
"""
import argparse
import os
import sys

import _http as h

WS_NAME = os.environ.get("FABRIC_WORKSPACE_NAME", "ws-agent-search-iq")
RESTRICTED_WS_NAME = os.environ.get("FABRIC_RESTRICTED_WORKSPACE_NAME",
                                    "ws-agent-search-iq-restricted")
CAPACITY_NAME = os.environ.get("FABRIC_CAPACITY_NAME")
QUALITY_GROUP_NAME = "grp-quality-team"
# レイクハウス名 → (置き場所, 説明)。"main" = WS_NAME、"restricted" = RESTRICTED_WS_NAME
LAKEHOUSES = {
    "lh_public": ("main", "全社公開のテーブル。全社員グループに読み取りを付与する"),
    "lh_restricted": ("restricted", "品質チーム限定のテーブル。閲覧者は品質チームグループだけ"),
}
ONTOLOGY_NAME = os.environ.get("FABRIC_ONTOLOGY_NAME", "ont_agent_search_iq")


def ensure_workspace(tok: str, name: str, desc: str, cap: dict, dry_run: bool) -> str | None:
    """ワークスペースを作って容量に割り当てる。dry-run で未作成なら None。"""
    print(f"\n■ ワークスペース {name}")
    _s, _h, wss = h.call("GET", h.FABRIC + "/workspaces", tok)
    ws = next((w for w in wss.get("value", []) if w["displayName"] == name), None)
    if ws:
        h.step(False, f"既存: id={ws['id']}")
    else:
        h.step(dry_run, "作成")
        if dry_run:
            return None
        s, _h, ws = h.call("POST", h.FABRIC + "/workspaces", tok,
                           {"displayName": name, "description": desc})
        if s >= 300:
            sys.exit(f"ワークスペース作成に失敗: {ws}")
    wsid = ws["id"]

    _s, _h, wd = h.call("GET", h.FABRIC + f"/workspaces/{wsid}", tok)
    if wd.get("capacityId") == cap["id"]:
        h.step(False, "容量: 割り当て済み")
    else:
        h.step(dry_run, f"容量に割り当て（現在: {wd.get('capacityId')}）")
        if not dry_run:
            s, _h, r = h.call("POST", h.FABRIC + f"/workspaces/{wsid}/assignToCapacity", tok,
                              {"capacityId": cap["id"]})
            if s >= 300:
                sys.exit(f"容量の割り当てに失敗: {r}")
    return wsid


def quality_group_id() -> str | None:
    gid = os.environ.get("QUALITY_TEAM_GROUP_ID")
    if gid:
        return gid
    try:
        gtok = h.az(["account", "get-access-token", "--resource", "https://graph.microsoft.com",
                     "--query", "accessToken", "-o", "tsv"])
    except RuntimeError:
        return None
    _s, _h, r = h.call("GET", h.url(h.GRAPH, "/groups",
                                    **{"$filter": f"displayName eq '{QUALITY_GROUP_NAME}'",
                                       "$select": "id"}), gtok)
    v = (r or {}).get("value") or []
    return v[0]["id"] if v else None


def ensure_viewer(tok: str, wsid: str | None, group_id: str | None, dry_run: bool) -> None:
    print(f"\n■ {RESTRICTED_WS_NAME} の閲覧者")
    if not group_id:
        print(f"  [!!] {QUALITY_GROUP_NAME} の ID が分からないので付与をスキップ"
              "（QUALITY_TEAM_GROUP_ID を設定して再実行するか、ポータルで閲覧者に追加する）")
        return
    if wsid:
        _s, _h, ra = h.call("GET", h.FABRIC + f"/workspaces/{wsid}/roleAssignments", tok)
        if any(r["principal"]["id"] == group_id for r in ra.get("value", [])):
            h.step(False, f"既存: {QUALITY_GROUP_NAME}")
            return
    h.step(dry_run, f"{QUALITY_GROUP_NAME} を閲覧者に追加")
    if not dry_run:
        s, _h, r = h.call("POST", h.FABRIC + f"/workspaces/{wsid}/roleAssignments", tok,
                          {"principal": {"id": group_id, "type": "Group"}, "role": "Viewer"})
        if s >= 300:
            sys.exit(f"閲覧者の追加に失敗: {r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not CAPACITY_NAME:
        sys.exit("FABRIC_CAPACITY_NAME（割り当てる容量の表示名）を設定してください")

    tok = h.token("https://api.fabric.microsoft.com")

    _s, _h, caps = h.call("GET", h.FABRIC + "/capacities", tok)
    cap = next((c for c in caps.get("value", []) if c["displayName"] == CAPACITY_NAME), None)
    if not cap:
        names = [c["displayName"] for c in caps.get("value", [])]
        sys.exit(f"容量 {CAPACITY_NAME} が見つかりません。見えている容量: {names}")
    print(f"■ 容量: {cap['displayName']}  sku={cap['sku']}  state={cap['state']}")
    if cap["state"] != "Active":
        print("  ※ 容量が Active ではない。割り当てやアイテム作成が失敗するので先に再開する")

    wsids = {
        "main": ensure_workspace(
            tok, WS_NAME,
            "Fabric IQ (Ontology) permission demo: public lakehouse / Direct Lake model / ontology",
            cap, a.dry_run),
        "restricted": ensure_workspace(
            tok, RESTRICTED_WS_NAME,
            "Fabric IQ permission demo: quality-team-only data. Viewer = grp-quality-team only",
            cap, a.dry_run),
    }
    ensure_viewer(tok, wsids["restricted"], quality_group_id(), a.dry_run)

    existing: dict[str, dict] = {}
    for key, wsid in wsids.items():
        existing[key] = {}
        if wsid:
            _s, _h, items = h.call("GET", h.FABRIC + f"/workspaces/{wsid}/items", tok)
            existing[key] = {(i["type"], i["displayName"]): i["id"] for i in items.get("value", [])}

    print("\n■ レイクハウス")
    ids: dict[str, str] = {}
    for name, (where, desc) in LAKEHOUSES.items():
        if ("Lakehouse", name) in existing[where]:
            ids[name] = existing[where][("Lakehouse", name)]
            h.step(False, f"既存: {name}  id={ids[name]}")
            continue
        h.step(a.dry_run, f"作成: {name}（{WS_NAME if where == 'main' else RESTRICTED_WS_NAME}）")
        if not a.dry_run:
            s, hd, r = h.call("POST", h.FABRIC + f"/workspaces/{wsids[where]}/items", tok,
                              {"displayName": name, "type": "Lakehouse", "description": desc})
            s, r = h.wait_lro(tok, s, hd, r)
            if s >= 300:
                sys.exit(f"{name} の作成に失敗: {r}")
            ids[name] = r["id"]
    if ("Lakehouse", "lh_restricted") in existing["main"]:
        print(f"  [!!] {WS_NAME} にも lh_restricted がある。閲覧者に SQL 経由で漏れるので削除すること")

    print("\n■ オントロジー")
    wsid = wsids["main"]
    if ("Ontology", ONTOLOGY_NAME) in existing["main"]:
        ids[ONTOLOGY_NAME] = existing["main"][("Ontology", ONTOLOGY_NAME)]
        h.step(False, f"既存: {ONTOLOGY_NAME}  id={ids[ONTOLOGY_NAME]}")
    else:
        h.step(a.dry_run, f"作成: {ONTOLOGY_NAME}（付随の Lakehouse / GraphModel も自動で作られる）")
        if not a.dry_run:
            s, hd, r = h.call("POST", h.FABRIC + f"/workspaces/{wsid}/ontologies", tok,
                              {"displayName": ONTOLOGY_NAME,
                               "description": "Fabric IQ ontology for the permission demo"})
            s, r = h.wait_lro(tok, s, hd, r)
            if s >= 300:
                sys.exit(f"オントロジーの作成に失敗: {r}")
            # 作成レスポンスに id が無いことがあるので /items から引き直す
            _s, _h, items = h.call("GET", h.FABRIC + f"/workspaces/{wsid}/items", tok)
            ids[ONTOLOGY_NAME] = next(i["id"] for i in items["value"]
                                      if i["type"] == "Ontology" and i["displayName"] == ONTOLOGY_NAME)

    print("\n=== 次のスクリプトに渡す値（ファイルには保存していない）===")
    if wsids["main"]:
        print(f"  {'FABRIC_WORKSPACE_ID':40s} = {wsids['main']}")
    if wsids["restricted"]:
        print(f"  {'FABRIC_LAKEHOUSE_RESTRICTED_WORKSPACE_ID':40s} = {wsids['restricted']}")
    for k, env in (("lh_public", "FABRIC_LAKEHOUSE_PUBLIC_ID"),
                   ("lh_restricted", "FABRIC_LAKEHOUSE_RESTRICTED_ID"),
                   (ONTOLOGY_NAME, "FABRIC_ONTOLOGY_ID")):
        if k in ids:
            print(f"  {env:40s} = {ids[k]}")
    print("\n権限付与はポータルで行う（アイテム単位の権限は API 非対応）:")
    print(f"  {ONTOLOGY_NAME} → 全社員グループ（読み取り）")
    print("  lh_public         → 全社員グループ")
    print("    「すべての SQL エンドポイント データを読み取る」と")
    print("    「すべての Apache Spark を読み取り…」にチェックする（既定の共有だけでは 401）")
    print(f"  lh_restricted     → 品質チームグループ（{RESTRICTED_WS_NAME} の閲覧者に加えて必須）")
    print("    同じく「すべての Apache Spark を読み取り…」にチェック。閲覧者だけだと Fabric IQ で拒否される")
    print("    グループへの共有で足りるが、反映に最大2時間かかる")


if __name__ == "__main__":
    main()
