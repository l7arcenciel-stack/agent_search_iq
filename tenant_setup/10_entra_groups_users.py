"""
Entra ID のセキュリティグループとデモ用テストユーザーを作り、ライセンスを割り当てる。

    python tenant_setup/10_entra_groups_users.py --dry-run
    python tenant_setup/10_entra_groups_users.py

【必要な権限】ユーザー管理者＋グループ管理者（新テナントでは Global Administrator）。
  セキュリティ既定値が有効なテナントでは先に1回:
    az login --tenant <TENANT_ID> --scope "https://graph.microsoft.com//.default"

【元テナントで使う場合】品質チームのグループは既存を流用し、全社員グループだけ作る:
    python tenant_setup/10_entra_groups_users.py --groups grp-all-employees --no-users
  テストユーザーを作れない（作らない）環境では --no-users を付ける。

【冪等】同名のグループ・ユーザーが既にあれば作らずにスキップする。何度流してもよい。

【実機でわかったこと（新テナント、2026-09-21）】
  - 既定で存在する「All Company」は Microsoft 365 グループ（securityEnabled=false）なので
    ACL やレイクハウスの権限付与に使わず、専用のセキュリティグループを作る。
  - 利用場所（usageLocation）を設定しないとライセンス割り当てが
    "invalid usage location" で失敗する。設定直後も反映待ちで失敗するので再試行する。
  - POWER_BI_STANDARD は Power BI の**無料版**（Pro ではない）。これで Fabric への
    サインインとオントロジーの閲覧はできた。
  - パスワードはメールで届かない。作成時の値は**リポジトリ外**の
    ~/.agent_search_iq/secrets/secrets.txt に保存し、画面には出さない。
"""
import argparse
import os
import secrets
import string
import time

import _http as h

GROUPS = {
    "grp-all-employees": "全社員（全社公開データの閲覧・エージェント呼び出し）",
    "grp-quality-team": "品質チーム（品質チーム限定データの閲覧）",
}

# (表示名, メールニックネーム, 所属グループ)
USERS = [
    ("品質 太郎", "demo.quality", ["grp-all-employees", "grp-quality-team"]),
    ("営業 次郎", "demo.sales", ["grp-all-employees"]),
]

LICENSE_SKU = os.environ.get("DEMO_LICENSE_SKU", "POWER_BI_STANDARD")
USAGE_LOCATION = os.environ.get("DEMO_USAGE_LOCATION", "JP")


def _password() -> str:
    al = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(al) for _ in range(20))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--groups", help="作るグループをカンマ区切りで限定する")
    ap.add_argument("--no-users", action="store_true", help="テストユーザーを作らない")
    a = ap.parse_args()

    tok = h.token("https://graph.microsoft.com")
    groups = {k: v for k, v in GROUPS.items()
              if not a.groups or k in a.groups.split(",")}

    _s, _h, d = h.call("GET", h.url(h.GRAPH, "/domains"), tok)
    domain = next(x["id"] for x in d["value"] if x.get("isDefault"))
    print(f"テナントの既定ドメイン: {domain}")

    print("\n■ セキュリティグループ")
    gids: dict[str, str] = {}
    for name, desc in groups.items():
        _s, _h, r = h.call("GET", h.url(h.GRAPH, "/groups",
                                        **{"$filter": f"displayName eq '{name}'"}), tok)
        if r.get("value"):
            gids[name] = r["value"][0]["id"]
            h.step(False, f"既存: {name}  id={gids[name]}")
            continue
        h.step(a.dry_run, f"作成: {name}（セキュリティ・割り当て済み）")
        if a.dry_run:
            continue
        s, _h, r = h.call("POST", h.GRAPH + "/groups", tok, {
            "displayName": name, "description": desc, "securityEnabled": True,
            "mailEnabled": False, "mailNickname": name.replace("-", ""), "groupTypes": []})
        if s >= 300:
            raise SystemExit(f"グループ作成に失敗: {r}")
        gids[name] = r["id"]

    if a.no_users:
        _print_ids(gids)
        return

    print("\n■ テストユーザー")
    _s, _h, skus = h.call("GET", h.GRAPH + "/subscribedSkus", tok)
    sku = next((x for x in skus.get("value", []) if x["skuPartNumber"] == LICENSE_SKU), None)
    if not sku:
        print(f"  ※ ライセンス {LICENSE_SKU} がテナントに無いため割り当ては省略する")

    for disp, nick, member_of in USERS:
        upn = f"{nick}@{domain}"
        _s, _h, r = h.call("GET", h.url(h.GRAPH, "/users",
                                        **{"$filter": f"userPrincipalName eq '{upn}'"}), tok)
        if r.get("value"):
            uid = r["value"][0]["id"]
            h.step(False, f"既存: {upn}")
        else:
            h.step(a.dry_run, f"作成: {upn}（{disp}）")
            if a.dry_run:
                continue
            pw = _password()
            s, _h, r = h.call("POST", h.GRAPH + "/users", tok, {
                "accountEnabled": True, "displayName": disp, "mailNickname": nick,
                "userPrincipalName": upn,
                "passwordProfile": {"password": pw, "forceChangePasswordNextSignIn": False}})
            if s >= 300:
                raise SystemExit(f"ユーザー作成に失敗: {r}")
            uid = r["id"]
            path = h.save_secret(f"{disp} {upn} のパスワード", pw)
            print(f"      パスワードを保存: {path}")

        _s, _h, mo = h.call("GET", h.GRAPH + f"/users/{uid}/memberOf?$select=id", tok)
        current = {x["id"] for x in mo.get("value", [])}
        for g in member_of:
            if g not in gids:
                continue
            if gids[g] in current:
                h.step(False, f"    既存: {g} に所属")
                continue
            h.step(a.dry_run, f"    {g} に追加")
            if not a.dry_run:
                s, _h, r = h.call("POST", h.GRAPH + f"/groups/{gids[g]}/members/$ref", tok,
                                  {"@odata.id": f"{h.GRAPH}/directoryObjects/{uid}"})
                if s >= 300 and "already exist" not in str(r):
                    print(f"      追加に失敗: {str(r)[:160]}")

        if sku and not a.dry_run:
            h.call("PATCH", h.GRAPH + f"/users/{uid}", tok, {"usageLocation": USAGE_LOCATION})
            for attempt in range(6):
                _s, _h, u = h.call("GET", h.GRAPH + f"/users/{uid}?$select=assignedLicenses", tok)
                if any(l["skuId"] == sku["skuId"] for l in u.get("assignedLicenses", [])):
                    h.step(False, f"    ライセンス {LICENSE_SKU}: 割り当て済み")
                    break
                s, _h, r = h.call("POST", h.GRAPH + f"/users/{uid}/assignLicense", tok,
                                  {"addLicenses": [{"skuId": sku["skuId"], "disabledPlans": []}],
                                   "removeLicenses": []})
                if s < 300:
                    h.step(False, f"    ライセンス {LICENSE_SKU}: 割り当て")
                    break
                time.sleep(15)  # 利用場所の反映待ち
            else:
                print("      ライセンス割り当てに失敗（利用場所の反映待ちの可能性。再実行する）")

    _print_ids(gids)
    print("\n次: 2人とも事前に初回サインインと MFA 登録を済ませる（デモ当日にやらせない）")


def _print_ids(gids: dict) -> None:
    print("\n=== .env / azd env に設定する値（ファイルには保存していない）===")
    if "grp-all-employees" in gids:
        print(f"  ALL_EMPLOYEES_GROUP_ID = {gids['grp-all-employees']}")
    if "grp-quality-team" in gids:
        print(f"  QUALITY_TEAM_GROUP_ID  = {gids['grp-quality-team']}")


if __name__ == "__main__":
    main()
