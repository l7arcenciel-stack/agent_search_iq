"""
デモUI・エージェントが使うアプリ登録（OBO 用）を作り、委任権限と管理者同意を付ける。

    python tenant_setup/20_entra_app_registration.py --dry-run
    python tenant_setup/20_entra_app_registration.py

【作るもの】
  - アプリ登録（既定名 app-agent-search-iq-obo。OBO_APP_NAME で変更可）
      サポートするアカウント: この組織のみ／パブリッククライアントフロー: 有効
      API の公開: api://<appId> と委任スコープ access_as_user
  - 委任権限と**管理者同意**（テナント全体への oauth2PermissionGrant）
      Microsoft Graph                  GroupMember.Read.All, User.Read
      Power BI Service                 Dataset.Read.All
      Azure Machine Learning Services  user_impersonation   ← https://ai.azure.com の実体
      自分自身                          access_as_user
  - クライアントシークレット（~/.agent_search_iq/secrets/secrets.txt に保存。画面には出さない）

【必要な権限】アプリケーション管理者（作成）＋ 特権ロール管理者 または Global Administrator（同意）。
  同意の付与に失敗した場合は、管理者に渡す「管理者の同意 URL」を表示する。

【元テナントで使う場合】既存デモのアプリ登録には触らず、**別名で新規作成**する:
    $env:OBO_APP_NAME = "app-agent-search-iq-obo"   # 既存と別名であること
  管理者に依頼するなら、このスクリプトをそのまま渡して実行してもらうのが早い。

【Fabric IQ 接続用のアプリは作らない】
  Foundry の Fabric IQ ツールの認証は「OAuth ID パススルー」（UserEntraToken：利用者本人の
  トークンをそのまま渡す）で、独自の OAuth アプリもリダイレクト URI も不要だった（実機で確認）。

【冪等】同名のアプリがあれば作り直さず、権限と同意だけ揃える。シークレットは新規作成時のみ発行する。
"""
import argparse
import os
import time
import uuid

import _http as h

APP_NAME = os.environ.get("OBO_APP_NAME", "app-agent-search-iq-obo")
SECRET_END = os.environ.get("OBO_SECRET_END", "2027-03-31T00:00:00Z")

# (リソースの appId, 表示用ラベル, 委任スコープ)
# ai.azure.com（Foundry）の実体は Azure Machine Learning Services。appId は Microsoft 共通。
RESOURCES = [
    ("00000003-0000-0000-c000-000000000000", "Microsoft Graph", ["GroupMember.Read.All", "User.Read"]),
    ("00000009-0000-0000-c000-000000000000", "Power BI Service", ["Dataset.Read.All"]),
    ("18a66f5f-dbdf-4c17-9dd7-1634712a9cbe", "Azure Machine Learning Services", ["user_impersonation"]),
]


def _sp(tok: str, app_id: str):
    _s, _h, r = h.call("GET", h.url(h.GRAPH, "/servicePrincipals",
                                    **{"$filter": f"appId eq '{app_id}'"}), tok)
    return (r.get("value") or [None])[0]


def _grant(tok, dry, client_sp, resource_sp, scopes) -> bool:
    label = resource_sp["displayName"]
    r = {}
    if client_sp:
        f = f"clientId eq '{client_sp['id']}' and resourceId eq '{resource_sp['id']}'"
        _s, _h, r = h.call("GET", h.url(h.GRAPH, "/oauth2PermissionGrants", **{"$filter": f}), tok)
        have = set(((r.get("value") or [{}])[0].get("scope") or "").split())
        if set(scopes) <= have:
            h.step(False, f"既存の同意: {label:32s} {' '.join(scopes)}")
            return True
    h.step(dry, f"管理者同意: {label:32s} {' '.join(scopes)}")
    if dry:
        return True
    if r.get("value"):
        # 既存の同意にある他のスコープを消さないよう、足し合わせてから更新する
        merged = sorted(set((r["value"][0].get("scope") or "").split()) | set(scopes))
        s, _h, r = h.call("PATCH", h.GRAPH + f"/oauth2PermissionGrants/{r['value'][0]['id']}",
                          tok, {"scope": " ".join(merged)})
    else:
        s, _h, r = h.call("POST", h.GRAPH + "/oauth2PermissionGrants", tok, {
            "clientId": client_sp["id"], "consentType": "AllPrincipals",
            "resourceId": resource_sp["id"], "scope": " ".join(scopes)})
    if s >= 300:
        print(f"      同意の付与に失敗: {str(r)[:160]}")
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tok = h.token("https://graph.microsoft.com")
    tenant = h.az(["account", "show", "--query", "tenantId", "-o", "tsv"])

    # リソース側の委任スコープID はテナントに登録されたサービスプリンシパルから引く
    resolved = []
    for app_id, label, scopes in RESOURCES:
        sp = _sp(tok, app_id)
        if not sp:
            raise SystemExit(f"{label} のサービスプリンシパルがテナントにありません")
        ids = {x["value"]: x["id"] for x in sp.get("oauth2PermissionScopes", [])}
        missing = [x for x in scopes if x not in ids]
        if missing:
            raise SystemExit(f"{label} に委任スコープ {missing} がありません")
        resolved.append((sp, scopes, [ids[x] for x in scopes]))

    print(f"■ アプリ登録 {APP_NAME}")
    _s, _h, r = h.call("GET", h.url(h.GRAPH, "/applications",
                                    **{"$filter": f"displayName eq '{APP_NAME}'"}), tok)
    new = not r.get("value")
    if not new:
        app = r["value"][0]
        h.step(False, f"既存: appId={app['appId']}")
    else:
        h.step(a.dry_run, "作成（パブリッククライアント有効・access_as_user を公開）")
        if a.dry_run:
            for sp, scopes, _ids in resolved:
                h.step(True, f"委任権限: {sp['displayName']:32s} {' '.join(scopes)}")
            print("\n（dry-run のため以降の同意・シークレット発行は省略）")
            return
        s, _h, app = h.call("POST", h.GRAPH + "/applications", tok, {
            "displayName": APP_NAME,
            "signInAudience": "AzureADMyOrg",
            "isFallbackPublicClient": True,          # デバイスコードフローに必須
            "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [{
                "id": str(uuid.uuid4()), "value": "access_as_user", "type": "User",
                "isEnabled": True,
                "adminConsentDisplayName": "Access agent_search_iq as the signed-in user",
                "adminConsentDescription": "Allows the demo UI to call the agent on behalf of the signed-in user.",
                "userConsentDisplayName": "Access agent_search_iq on your behalf",
                "userConsentDescription": "Allows the demo UI to call the agent on your behalf."}]},
            "requiredResourceAccess": [
                {"resourceAppId": sp["appId"],
                 "resourceAccess": [{"id": i, "type": "Scope"} for i in ids]}
                for sp, _scopes, ids in resolved],
        })
        if s >= 300:
            raise SystemExit(f"アプリ作成に失敗: {app}")

    # デモUIのブラウザ方式サインイン（認可コード＋PKCE）のリダイレクト先。
    # デバイスコード方式はセキュリティ既定値で AADSTS530035 になるため、ブラウザ方式を使う。
    redirect = "http://localhost"
    if redirect not in (app.get("publicClient") or {}).get("redirectUris", []):
        h.step(a.dry_run, f"リダイレクト URI（パブリッククライアント）: {redirect}")
        if not a.dry_run:
            uris = sorted(set((app.get("publicClient") or {}).get("redirectUris", [])) | {redirect})
            h.call("PATCH", h.GRAPH + f"/applications/{app['id']}", tok,
                   {"publicClient": {"redirectUris": uris}})
    else:
        h.step(False, f"既存: リダイレクト URI {redirect}")

    if not app.get("identifierUris"):
        h.step(a.dry_run, f"identifierUri: api://{app['appId']}")
        if not a.dry_run:
            h.call("PATCH", h.GRAPH + f"/applications/{app['id']}", tok,
                   {"identifierUris": [f"api://{app['appId']}"]})

    client_sp = _sp(tok, app["appId"])
    if not client_sp and not a.dry_run:
        for _ in range(6):
            s, _h, client_sp = h.call("POST", h.GRAPH + "/servicePrincipals", tok,
                                      {"appId": app["appId"]})
            if s < 300:
                break
            time.sleep(5)
        time.sleep(5)

    ok = True
    for sp, scopes, _ids in resolved:
        ok &= _grant(tok, a.dry_run, client_sp, sp, scopes)
    # デモUIは自アプリの API（api://<appId>/.default）のトークンを取るので、自分自身にも同意する
    ok &= _grant(tok, a.dry_run, client_sp, client_sp, ["access_as_user"])

    if new and not a.dry_run:
        s, _h, pw = h.call("POST", h.GRAPH + f"/applications/{app['id']}/addPassword", tok,
                           {"passwordCredential": {"displayName": "agent_search_iq",
                                                   "endDateTime": SECRET_END}})
        path = h.save_secret(f"OBO_CLIENT_SECRET ({APP_NAME})", pw.get("secretText", ""))
        print(f"\n  シークレットを保存: {path}（期限 {SECRET_END}）")

    if not ok:
        print("\n  管理者同意を付与できませんでした。管理者に次の URL を開いてもらう:")
        print(f"  https://login.microsoftonline.com/{tenant}/adminconsent?client_id={app['appId']}")

    print("\n=== .env / azd env に設定する値（ファイルには保存していない）===")
    print(f"  OBO_TENANT_ID = {tenant}")
    print(f"  OBO_CLIENT_ID = {app['appId']}")
    print(f"  OBO_APP_SCOPE = api://{app['appId']}/.default")
    print("  OBO_CLIENT_SECRET = （secrets.txt を参照）")


if __name__ == "__main__":
    main()
