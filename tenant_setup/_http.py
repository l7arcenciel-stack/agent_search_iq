"""tenant_setup/ の各スクリプトが共通で使う、トークン取得と REST 呼び出し。

シェルを介さずに HTTP を直接叩く。Windows では `az rest` を shell 経由で呼ぶと
URL 中の `&` を cmd がコマンド区切りとして解釈して壊れるため（実機で踏んだ）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.microsoft.com/v1.0"
FABRIC = "https://api.fabric.microsoft.com/v1"
ARM = "https://management.azure.com"

SECRETS_DIR = os.environ.get(
    "AGENT_SEARCH_IQ_SECRETS_DIR",
    os.path.join(os.path.expanduser("~"), ".agent_search_iq", "secrets"),
)


def az(args: list[str]) -> str:
    r = subprocess.run(["az", *args], capture_output=True, text=True,
                       shell=(sys.platform == "win32"))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:400])
    return r.stdout.strip()


def token(resource: str) -> str:
    try:
        return az(["account", "get-access-token", "--resource", resource,
                   "--query", "accessToken", "-o", "tsv"])
    except RuntimeError as e:
        hint = ""
        if "graph.microsoft.com" in resource:
            hint = ("\n  セキュリティ既定値が有効なテナントでは、先に次を1回実行する:\n"
                    '  az login --tenant <TENANT_ID> --scope "https://graph.microsoft.com//.default"')
        sys.exit(f"{resource} のトークンを取得できません: {e}{hint}")


def url(base: str, path: str, **params) -> str:
    return base + path + ("?" + urllib.parse.urlencode(params) if params else "")


def call(method: str, u: str, tok: str, body=None):
    """(status, headers, json) を返す。HTTP エラーでも例外にしない。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(u, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8")
            return r.status, dict(r.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, dict(e.headers), json.loads(raw)
        except ValueError:
            return e.code, dict(e.headers), {"raw": raw[:800]}


def wait_lro(tok: str, status: int, headers: dict, body):
    """Fabric の 202（長時間実行）を完了まで待つ。完了後の result を返す。"""
    if status != 202:
        return status, body
    loc = headers.get("Location") or headers.get("location")
    for _ in range(60):
        time.sleep(int(headers.get("Retry-After", 4) or 4))
        s, _h, r = call("GET", loc, tok)
        st = (r or {}).get("status")
        if st == "Succeeded":
            s2, _h2, res = call("GET", loc.rstrip("/") + "/result", tok)
            return (200, res) if s2 < 400 else (200, r)
        if st in ("Failed", "Undefined"):
            return 500, r
    return 504, {"error": "timeout"}


def save_secret(label: str, value: str) -> str:
    """シークレットを**リポジトリ外**のファイルに追記する。標準出力には出さない。"""
    os.makedirs(SECRETS_DIR, exist_ok=True)
    path = os.path.join(SECRETS_DIR, "secrets.txt")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{label}\n  {value}\n\n")
    return path


def step(dry_run: bool, msg: str) -> None:
    print(("  [dry-run] " if dry_run else "  ") + msg)
