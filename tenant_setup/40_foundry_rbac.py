"""
Foundry プロジェクトにロールを割り当てる。

    $env:FOUNDRY_RESOURCE_GROUP = "rg-agent-search-iq"
    $env:FOUNDRY_ACCOUNT        = "<Foundry アカウント名>"
    $env:FOUNDRY_PROJECT        = "<プロジェクト名>"
    $env:AGENT_USERS_GROUP_ID   = "<全社員グループのID>"
    python tenant_setup/40_foundry_rbac.py --dry-run
    python tenant_setup/40_foundry_rbac.py

【割り当てるもの】スコープはすべてプロジェクト
  AGENT_USERS_GROUP_ID  … Foundry Agent Consumer（エージェントのエンドポイント呼び出しだけの最小権限）
  実行している自分       … Foundry Project Manager（ツール・Toolbox 作成、エージェントのデプロイ用）
                           --skip-operator で省略できる

【必要な権限】プロジェクト（またはその上位）の 所有者 か ユーザー アクセス管理者。

【実機でわかったこと（新テナント、2026-09-21）】
  - ロール名が変わっている。旧「Azure AI User / Azure AI Project Manager」は見つからず、
    「Foundry User」「Foundry Project Manager」「Foundry Agent Consumer」などになっていた。
    このスクリプトは名前でロール定義を引くので、見つからなければ候補を表示して止まる。
  - サブスクリプションの Owner だけでは Toolbox を作れない（Owner はデータ操作 dataActions を
    含まない）。自分にも Foundry 系のロールが要る。
  - Git Bash で az role assignment create --scope /subscriptions/... を打つと、パスが
    Windows 形式に変換されて MissingSubscription になる。このスクリプトは ARM を直接叩くので影響しない。
  - Foundry Agent Consumer で Invocations（OBO 登録）まで通るかは【要確認】。
    403 になったら AGENT_ROLE=\"Foundry User\" にして再実行する。

【冪等】同じ割り当てがあればスキップする。
"""
import argparse
import os
import sys
import uuid

import _http as h

API = "2022-04-01"
AGENT_ROLE = os.environ.get("AGENT_ROLE", "Foundry Agent Consumer")
OPERATOR_ROLE = os.environ.get("OPERATOR_ROLE", "Foundry Project Manager")


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"環境変数 {name} を設定してください（冒頭の docstring 参照）")
    return v


def _role_def_id(tok: str, scope: str, name: str) -> str:
    _s, _h, r = h.call("GET", h.url(h.ARM, f"{scope}/providers/Microsoft.Authorization/roleDefinitions",
                                    **{"$filter": f"roleName eq '{name}'", "api-version": API}), tok)
    if r.get("value"):
        return r["value"][0]["id"]
    _s, _h, all_ = h.call("GET", h.url(h.ARM, f"{scope}/providers/Microsoft.Authorization/roleDefinitions",
                                       **{"api-version": API}), tok)
    cands = sorted(d["properties"]["roleName"] for d in all_.get("value", [])
                   if any(k in d["properties"]["roleName"] for k in ("Foundry", "Azure AI")))
    sys.exit(f"ロール「{name}」が見つかりません。候補: {cands}")


def _assign(tok, dry, scope, role_name, principal_id, principal_type, label) -> None:
    rid = _role_def_id(tok, scope, role_name)
    _s, _h, r = h.call("GET", h.url(h.ARM, f"{scope}/providers/Microsoft.Authorization/roleAssignments",
                                    **{"$filter": f"principalId eq '{principal_id}'", "api-version": API}), tok)
    for x in r.get("value", []):
        p = x["properties"]
        if p["roleDefinitionId"].lower() == rid.lower() and p["scope"].lower() == scope.lower():
            h.step(False, f"既存: {label} → {role_name}")
            return
    h.step(dry, f"割り当て: {label} → {role_name}")
    if dry:
        return
    s, _h, r = h.call("PUT", h.url(h.ARM, f"{scope}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}",
                                   **{"api-version": API}), tok,
                      {"properties": {"roleDefinitionId": rid, "principalId": principal_id,
                                      "principalType": principal_type}})
    if s >= 300:
        print(f"      失敗: {str(r)[:200]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-operator", action="store_true")
    a = ap.parse_args()

    sub = h.az(["account", "show", "--query", "id", "-o", "tsv"])
    scope = (f"/subscriptions/{sub}/resourceGroups/{_req('FOUNDRY_RESOURCE_GROUP')}"
             f"/providers/Microsoft.CognitiveServices/accounts/{_req('FOUNDRY_ACCOUNT')}"
             f"/projects/{_req('FOUNDRY_PROJECT')}")
    tok = h.token("https://management.azure.com")
    print(f"■ スコープ: {scope}")

    _assign(tok, a.dry_run, scope, AGENT_ROLE, _req("AGENT_USERS_GROUP_ID"), "Group",
            "エージェント利用者グループ")
    if not a.skip_operator:
        me = h.az(["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
        _assign(tok, a.dry_run, scope, OPERATOR_ROLE, me, "User", "自分")
    print("\n反映まで数分かかることがある。")


if __name__ == "__main__":
    main()
