"""
構築状況を点検して、次にやるべきことを表示する。**作業再開時に最初に実行する。**

手元の PC で実行する（対象テナントに az login 済みであること）。
読み取りしか行わないので、状態が分からなくなったらいつでも流してよい。

    python fabric_notebooks/00_check_status.py

【なぜこれがあるか】
  必要な ID（ワークスペース・レイクハウス・オントロジー・グループ）を
  **ファイルに書かず、毎回ライブで引く**ため。
  実値をリポジトリに残さない方針なので、再開のたびにポータルを手で辿るのは非効率。
  このスクリプトが現状を読み取り、次の環境変数と手順をそのまま出力する。

【容量が停止していると 404 になる】
  Fabric 容量が Paused だと、ワークスペースやアイテムの一覧は取れるのに、
  アイテム個別のサブリソース（/lakehouses/<id>/tables、/ontologies/<id>/getDefinition）
  だけが **404** を返す。「データが消えた」「権限が無い」と誤診しやすい。
  このスクリプトは最初に容量の状態を確認し、停止中なら警告を出す。
  エラーを「0件」と誤読しないよう、取得できなかった項目は [??] で区別する。

【前提】
  - Fabric 向け: az login 済み
  - Entra 向け : セキュリティ既定値が有効なテナントでは、Graph へのアクセスに
      az login --tenant <TENANT_ID> --scope "https://graph.microsoft.com//.default"
    が別途必要（この部分は失敗してもスキップして続行する）
"""
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

WORKSPACE_NAME = "ws-agent-search-iq"
# 限定データ専用のワークスペース。閲覧者は grp-quality-team だけにする。
# 元のワークスペースに置くと、ワークスペースの閲覧者（デモユーザー全員）が
# SQL エンドポイント経由で限定データを読めてしまう（operations_log §20）。
RESTRICTED_WORKSPACE_NAME = "ws-agent-search-iq-restricted"
ONTOLOGY_NAME = "ont_agent_search_iq"
SEMANTIC_MODEL_NAME = "sm_agent_search_iq"
LAKEHOUSES = ("lh_public", "lh_restricted")
GROUPS = ("grp-all-employees", "grp-quality-team")
TEST_USER_PREFIXES = ("demo.quality", "demo.sales")

EXPECTED_TABLES = {
    "lh_public": {
        "product", "product_group", "site", "quality_document_public",
        "edge_product_product_group", "edge_product_site",
        "edge_document_public_product",
        "product_group_sales_monthly", "site_sales_monthly",
    },
    "lh_restricted": {"quality_document_restricted", "edge_document_restricted_product"},
}


def _az(args: list[str]) -> tuple[str | None, str]:
    r = subprocess.run(args, capture_output=True, text=True, shell=(sys.platform == "win32"))
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200]
    return r.stdout.strip(), ""


def _token(resource: str) -> str | None:
    out, _err = _az(["az", "account", "get-access-token", "--resource", resource,
                     "--query", "accessToken", "-o", "tsv"])
    return out


def _get(url: str, token: str):
    # $filter などにスペースが入るとそのままでは InvalidURL になるためエンコードする
    url = urllib.parse.quote(url, safe=":/?&=$,'()@+;~*!")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode(errors="replace")[:200]}


def mark(ok: bool) -> str:
    return "[OK]  " if ok else "[--]  "


def main() -> None:
    todo: list[str] = []
    env_lines: list[str] = []

    print("=" * 72)
    print("agent_search_iq  構築状況")
    print("=" * 72)

    # --- テナント -----------------------------------------------------------
    acct, err = _az(["az", "account", "show",
                     "--query", "{t:tenantId,s:name,u:user.name}", "-o", "json"])
    if not acct:
        sys.exit(f"az にログインしていません: {err}\n  az login --tenant <TENANT_ID>")
    a = json.loads(acct)
    print(f"\n■ テナント")
    print(f"   {a['u']}  /  {a['s']}")
    print(f"   tenantId = {a['t']}")
    print("   ※ 複数テナントを扱うため、作業前に必ずここを確認すること")
    env_lines.append(f"OBO_TENANT_ID = {a['t']}")

    # --- Fabric -------------------------------------------------------------
    tok = _token("https://api.fabric.microsoft.com")
    if not tok:
        sys.exit("Fabric のトークンを取得できません。az login を確認してください。")

    ws = _get("https://api.fabric.microsoft.com/v1/workspaces", tok)
    target = next((w for w in ws.get("value", [])
                   if w.get("displayName") == WORKSPACE_NAME), None)
    print(f"\n■ Fabric ワークスペース")
    if not target:
        print(f"   {mark(False)}{WORKSPACE_NAME} が見つかりません")
        todo.append(f"ワークスペース {WORKSPACE_NAME} を作成し、容量に割り当てる")
        _print_todo(todo, env_lines)
        return
    wsid = target["id"]
    print(f"   {mark(True)}{WORKSPACE_NAME}")
    print(f"   workspaceId = {wsid}")

    # --- 容量（ワークスペースが実際に割り当てられている容量を見る）-----------
    # F2 を止めて試用容量（FTL64 など）に付け替える運用があるため、ARM 上の
    # 特定の容量ではなく「いまワークスペースが載っている容量」の状態で判定する。
    wd = _get(f"https://api.fabric.microsoft.com/v1/workspaces/{wsid}", tok)
    caps = {c["id"]: c for c in
            _get("https://api.fabric.microsoft.com/v1/capacities", tok).get("value", [])}
    cap = caps.get(wd.get("capacityId"))
    print("\n■ Fabric 容量（このワークスペースの割り当て先）")
    if not cap:
        print(f"   {mark(False)}容量に割り当てられていない（capacityId={wd.get('capacityId')}）")
        todo.append("ワークスペースを容量に割り当てる")
    else:
        active = cap.get("state") == "Active"
        trial = cap.get("sku", "").upper().startswith("FT")
        print(f"   {mark(active)}{cap['displayName']}  sku={cap['sku']}  state={cap['state']}"
              + ("  （試用容量）" if trial else ""))
        if not active:
            print("          停止中はアイテム個別の API が 404 を返す。作業前に再開すること")
            todo.append(f"容量 {cap['displayName']} を再開する（停止中だと点検結果が不正確）")
        if trial:
            print("          ※ 試用容量には期限がある。切れる前に有料容量へ戻すこと")
    for c in caps.values():
        if c["id"] != (cap or {}).get("id") and c["sku"].startswith("F") \
                and not c["sku"].startswith("FT") and c.get("state") == "Active":
            print(f"   [!!]  {c['displayName']} ({c['sku']}) は未使用なのに Active（課金中）")
    env_lines.append(f"FABRIC_WORKSPACE_ID = {wsid}")

    items = _get(f"https://api.fabric.microsoft.com/v1/workspaces/{wsid}/items", tok)
    by_name = {i["displayName"]: i for i in items.get("value", [])}

    # --- 限定用ワークスペース -------------------------------------------------
    print(f"\n■ 限定用ワークスペース")
    rws = next((w for w in ws.get("value", [])
                if w.get("displayName") == RESTRICTED_WORKSPACE_NAME), None)
    r_by_name: dict = {}
    if not rws:
        print(f"   {mark(False)}{RESTRICTED_WORKSPACE_NAME} が見つかりません")
        todo.append(f"ワークスペース {RESTRICTED_WORKSPACE_NAME} を作成し、"
                    "grp-quality-team だけを閲覧者にする")
    else:
        rwsid = rws["id"]
        print(f"   {mark(True)}{RESTRICTED_WORKSPACE_NAME}")
        print(f"   workspaceId = {rwsid}")
        env_lines.append(f"FABRIC_LAKEHOUSE_RESTRICTED_WORKSPACE_ID = {rwsid}")
        ra = _get(f"https://api.fabric.microsoft.com/v1/workspaces/{rwsid}/roleAssignments", tok)
        for r in ra.get("value", []):
            pr = r["principal"]
            print(f"          {r['role']:12s} {pr.get('displayName')} ({pr['type']})")
        r_items = _get(f"https://api.fabric.microsoft.com/v1/workspaces/{rwsid}/items", tok)
        r_by_name = {i["displayName"]: i for i in r_items.get("value", [])}
    if "lh_restricted" in by_name:
        print(f"   [!!]  {WORKSPACE_NAME} にも lh_restricted がある。閲覧者に SQL 経由で漏れる")
        todo.append(f"{WORKSPACE_NAME} の lh_restricted を削除する（限定用ワークスペースに移したもの）")

    # --- レイクハウスとテーブル ---------------------------------------------
    print(f"\n■ レイクハウス")
    lh_ids: dict[str, str] = {}
    for lh in LAKEHOUSES:
        lh_wsid = rws["id"] if lh == "lh_restricted" and rws else wsid
        it = (r_by_name if lh == "lh_restricted" else by_name).get(lh)
        if not it:
            print(f"   {mark(False)}{lh} が無い")
            todo.append(f"レイクハウス {lh} を作成する")
            continue
        lh_ids[lh] = it["id"]
        t = _get(f"https://api.fabric.microsoft.com/v1/workspaces/{lh_wsid}"
                 f"/lakehouses/{it['id']}/tables", tok)
        if "_error" in t:
            # API エラーを「0件」と読むと誤った TODO を出してしまうので区別する。
            # この /tables エンドポイントは一時的に 404 を返すことがある（実機で確認）。
            print(f"   [??]  {lh}  テーブル一覧を取得できません"
                  f"（HTTP {t['_error']}）  id={it['id']}")
            print(f"          ポータルで中身を目視確認すること")
            todo.append(f"{lh} の状態を確認できなかった（容量停止中でないか確認）")
            continue
        names = {x["name"] for x in t.get("data", t.get("value", []))}
        want = EXPECTED_TABLES[lh]
        missing, extra = want - names, names - want
        ok = not missing and not extra
        print(f"   {mark(ok)}{lh}  ({len(names)} tables)  id={it['id']}")
        if missing:
            print(f"          不足: {sorted(missing)}")
            todo.append(f"{lh}: 01_create_delta_tables.py を TARGET="
                        f"'{'public' if lh == 'lh_public' else 'restricted'}' で実行"
                        f"（{lh} をアタッチすること）")
        if extra:
            print(f"          余分: {sorted(extra)}")
            todo.append(f"{lh}: 余分なテーブルを DROP TABLE で削除する")
    for k, v in lh_ids.items():
        env_lines.append(f"FABRIC_LAKEHOUSE_{'PUBLIC' if k == 'lh_public' else 'RESTRICTED'}_ID = {v}")

    # --- セマンティックモデル -------------------------------------------------
    print(f"\n■ セマンティックモデル")
    sm = by_name.get(SEMANTIC_MODEL_NAME)
    if sm:
        print(f"   {mark(True)}{SEMANTIC_MODEL_NAME}  id={sm['id']}")
        env_lines.append(f"FABRIC_DATASET_ID = {sm['id']}")
        print("          ※ lh_public を向いているか、ストレージモードが Direct Lake かは")
        print("            ポータルまたは getDefinition で要確認")
    else:
        print(f"   {mark(False)}{SEMANTIC_MODEL_NAME} が無い")
        todo.append("02_create_semantic_model.py を lh_public の SQL エンドポイントで実行")

    # --- オントロジー ---------------------------------------------------------
    print(f"\n■ オントロジー")
    ont = by_name.get(ONTOLOGY_NAME)
    if not ont:
        print(f"   {mark(False)}{ONTOLOGY_NAME} が無い")
        todo.append(f"オントロジー {ONTOLOGY_NAME} を作成する"
                    "（POST /v1/workspaces/<ws>/ontologies）")
    else:
        print(f"   {mark(True)}{ONTOLOGY_NAME}  id={ont['id']}")
        env_lines.append(f"FABRIC_IQ_ONTOLOGY_ITEM_ID = {ont['id']}")
        env_lines.append(f"FABRIC_IQ_ONTOLOGY_WORKSPACE_ID = {wsid}")
        _report_ontology(wsid, ont["id"], tok, lh_ids, todo)

    # --- Entra ----------------------------------------------------------------
    print(f"\n■ Entra ID")
    gtok = _token("https://graph.microsoft.com")
    if not gtok:
        print("   [??]  Graph のトークンを取得できません（セキュリティ既定値）")
        print("         az login --tenant <TENANT_ID> --scope \"https://graph.microsoft.com//.default\"")
    else:
        for g in GROUPS:
            r = _get("https://graph.microsoft.com/v1.0/groups"
                     f"?$filter=displayName eq '{g}'&$select=id,displayName", gtok)
            v = r.get("value") or []
            print(f"   {mark(bool(v))}{g}" + (f"  id={v[0]['id']}" if v else "  が無い"))
            if v:
                key = ("ALL_EMPLOYEES_GROUP_ID" if "all-employees" in g
                       else "QUALITY_TEAM_GROUP_ID")
                env_lines.append(f"{key} = {v[0]['id']}")
            else:
                todo.append(f"セキュリティグループ {g} を作成する")
        r = _get("https://graph.microsoft.com/v1.0/users?$select=userPrincipalName", gtok)
        upns = [u["userPrincipalName"] for u in r.get("value", [])]
        for p in TEST_USER_PREFIXES:
            hit = [u for u in upns if u.startswith(p + "@")]
            print(f"   {mark(bool(hit))}{p}" + (f"  {hit[0]}" if hit else "  が無い"))
            if not hit:
                todo.append(f"テストユーザー {p} を作成し、グループに追加・ライセンス割り当て")

    _print_todo(todo, env_lines)


def _report_ontology(wsid: str, oid: str, tok: str, lh_ids: dict, todo: list) -> None:
    """オントロジーの定義を読み、エンティティとバインド先を表示する。"""
    import base64
    import time

    req = urllib.request.Request(
        f"https://api.fabric.microsoft.com/v1/workspaces/{wsid}/ontologies/{oid}/getDefinition",
        data=None, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            status, headers = resp.status, dict(resp.headers)
            raw = resp.read().decode()
            body = json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        print(f"   [??]      定義を取得できません: {e.code}")
        todo.append("オントロジー定義を確認できなかった（容量停止中でないか確認）")
        return

    if status == 202:
        loc = headers.get("Location") or headers.get("location")
        body = None
        for _ in range(20):
            time.sleep(3)
            r = _get(loc, tok)
            if r.get("status") == "Succeeded":
                body = _get(loc.rstrip("/") + "/result", tok)
                break
            if r.get("status") == "Failed":
                break
    if not body or "definition" not in body:
        print("          定義が空（エンティティ未作成）")
        todo.append("03_create_ontology_definition.py で定義を流し込む")
        return

    import re
    parts = {p["path"]: json.loads(base64.b64decode(p["payload"]).decode())
             for p in body["definition"]["parts"] if p["path"] != ".platform"}
    ENT = re.compile(r"^EntityTypes/[^/]+/definition\.json$")
    rev = {v: k for k, v in lh_ids.items()}
    ents = {o["id"]: o for p, o in parts.items() if ENT.match(p)}
    print(f"          エンティティ型 {len(ents)} 件（? = lh_public / lh_restricted 以外を指している）")
    stale = False
    for o in ents.values():
        binds = [x for p, x in parts.items()
                 if p.startswith(f"EntityTypes/{o['id']}/DataBindings/")]
        where = {rev.get(b["dataBindingConfiguration"]["sourceTableProperties"]["itemId"],
                         "?") for b in binds}
        kinds = [b["dataBindingConfiguration"]["dataBindingType"][:4] for b in binds]
        if not binds or "?" in where:
            stale = True
        print(f"            {o['name']:30s} -> {','.join(sorted(where)) or '(未バインド)'}  {kinds}")
    rels = [o for p, o in parts.items()
            if re.match(r"^RelationshipTypes/[^/]+/definition\.json$", p)]
    print(f"          リレーション型 {len(rels)} 件: {[r['name'] for r in rels]}")
    if stale or len(ents) < 5:
        todo.append("03_create_ontology_definition.py を実行し、バインド先を"
                    " lh_public / lh_restricted に向け直す")


def _print_todo(todo: list, env_lines: list) -> None:
    print("\n" + "=" * 72)
    print("次にやること")
    print("=" * 72)
    if todo:
        for i, t in enumerate(todo, 1):
            print(f"  {i}. {t}")
    else:
        print("  構築済みの項目に不足はありません。")
        print("  残りの工程は docs/guides/new_tenant_setup.html の §5 以降（Foundry 接続 →")
        print("  Toolbox → azd deploy → 2人で比較）を参照。")

    print("\n  ※ 権限:")
    print("       lh_public     → grp-all-employees（アイテム共有。ポータル作業）")
    print("                       「すべての SQL エンドポイント データを読み取る」と")
    print("                       「すべての Apache Spark を読み取り…」にチェックが必要")
    print(f"       lh_restricted → {RESTRICTED_WORKSPACE_NAME} に置き、")
    print("                       grp-quality-team だけをワークスペースの閲覧者にし、")
    print("                       lh_restricted も grp-quality-team に ReadAll 付きで共有する（Fabric IQ に必須）")

    if env_lines:
        print("\n" + "=" * 72)
        print("現在の値（.env / azd env 用。ファイルには保存していない）")
        print("=" * 72)
        for line in env_lines:
            print(f"  {line}")


if __name__ == "__main__":
    main()
