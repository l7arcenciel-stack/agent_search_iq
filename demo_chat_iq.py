"""
demo_chat_iq.py — agent-search-iq（Fabric IQ 搭載版）用のデモUI（Streamlit）。

【目的】
「部署をまたいで聞けるようにしても、見せてはいけないものは見せない」ことを、
**同じ質問を権限の違う2人で実行して**見比べる形で実演する。
ブラウザを2つ（通常ウィンドウ＋シークレット、または別プロファイル）開き、
それぞれ別のアカウントでサインインして使う。

【既存デモUI（demo_chat.py / demo_chat_simple.py）との違い】
  1. 接続先が agent-search-iq（別エージェント）。既存エージェントには一切触れない。
  2. 参照元に「🕸 Fabric IQ」を追加。
  3. **「実行ユーザー」と「閲覧できなかった情報」を毎回表示する。**
     第5回で出た「中間レイヤーが見えず、出てきた数字が正しいか分からない」への打ち手。
  4. Fabric IQ の OAuth 同意要求（oauth_consent_request）を受け取って、同意リンクを表示する。
     未実装だと、未同意ユーザーには「何も返ってこない」ようにしか見えない。
  5. **Foundry をエンドユーザー本人のトークンで呼ぶ（FOUNDRY_CALL_AS=user、既定）。**

【5. が必要な理由（重要）】
  既存のデモUIは、Foundry 呼び出しの Bearer をこのPCの DefaultAzureCredential
  （＝オペレーターの az login）で取っていた。この場合：
    - AI Search / Power BI：OBO登録した「サインインした人」の権限で動く（_OBO_CACHE 経由）
    - Fabric IQ（Toolbox）：Foundry MCP プロキシが「Foundry の呼び出し元」＝**オペレーター**
      としてユーザーを解決するため、オペレーターの権限で動く
  となり、**経路によって「誰の権限か」がずれる**。権限の違う2人で見比べるデモでは、
  Fabric IQ の部分だけ2人とも同じ結果になってしまい、デモの主張が成立しない。
  そこでサインインした本人のトークン（scope=https://ai.azure.com/.default）で Foundry を呼び、
  3経路すべてを同一人物にそろえる。

  FOUNDRY_CALL_AS=operator にすると従来どおりオペレーターのトークンで呼ぶ
  （Azure側の準備が済む前の疎通確認用）。この場合は画面に警告を出す。

【user モードの前提（Azure側）】
  - サインインに使う Entra アプリ（OBO_CLIENT_ID）が、https://ai.azure.com 向けの
    委任権限を持ち、管理者同意済みであること（無いと AADSTS65001 / AADSTS650057 等）。
  - デモに使う各アカウントに、Foundry プロジェクト上でエージェントを呼び出せるロール
    （Foundry User 相当）が割り当てられていること（無いと 403）。
  - 各アカウントが Fabric IQ 接続の OAuth 同意を済ませていること（初回は画面に同意リンクが出る）。

【起動方法】
    pip install -r requirements-demo.txt
    streamlit run demo_chat_iq.py

【セキュリティ上の注意】
  アクセストークン・user_assertion は画面にも標準出力にも出さない。
  st.session_state に保持するのは MSAL のアプリオブジェクトとアカウント情報のみで、
  トークンは必要になるたびに MSAL のキャッシュから取り出す。
"""
import json
import os

import msal
import requests
import streamlit as st

from common import (
    FOUNDRY_PROJECT_ENDPOINT,
    IQ_AGENT_NAME,
    OBO_APP_SCOPE,
    OBO_CLIENT_ID,
    OBO_TENANT_ID,
    QUALITY_TEAM_GROUP_ID,
)

# ============================================================================
# 設定
# ============================================================================

_FOUNDRY_SCOPE = "https://ai.azure.com/.default"

_ENDPOINT_BASE = (
    os.environ.get("IQ_AGENT_ENDPOINT_BASE")
    or f"{FOUNDRY_PROJECT_ENDPOINT.rstrip('/')}/agents/{IQ_AGENT_NAME}/endpoint"
).rstrip("/")
_INVOCATIONS_URL = f"{_ENDPOINT_BASE}/protocols/invocations"
_RESPONSES_URL = f"{_ENDPOINT_BASE}/protocols/openai/responses"

_CALL_AS = (os.environ.get("FOUNDRY_CALL_AS") or "user").strip().lower()
if _CALL_AS not in ("user", "operator"):
    _CALL_AS = "user"

_SESSION_ERROR_STATUS_CODES = (401, 403, 424)

# ツール名 → 参照元の分類。この2つ以外のツール呼び出しは Fabric IQ（Toolbox 経由の MCP ツール）
# とみなす（Fabric IQ 側が公開するツール名は実機で確認するまで確定しないため、除外法で判定する）。
_TOOL_SOURCE = {
    "search_documents_tool": "search",
    "query_fabric_tool": "fabric",
}
_SOURCE_LABEL = {
    "iq": "🕸 Fabric IQ",
    "fabric": "📊 Microsoft Fabric",
    "search": "🔎 Azure AI Search",
}
_SOURCE_NOUN = {
    "iq": "業務データの関係（Fabric IQ）",
    "fabric": "売上などの数値データ",
    "search": "品質文書などの社内文書",
}
_SOURCE_ORDER = ("iq", "fabric", "search")

_EXAMPLE_QUESTIONS = [
    "製品A008の品質基準について教えて",
    "地域別の売上金額を教えて",
    "A008の品質情報と、関連する商品群の売上をまとめて",
]

st.set_page_config(page_title="社内ナレッジアシスタント（Fabric IQ）", page_icon="🕸")


# ============================================================================
# セッション状態（ブラウザのタブごとに独立。2人を見比べるときは別ウィンドウで開く）
# ============================================================================

def _init_state() -> None:
    defaults = {
        "signed_in": False,
        "agent_session_id": None,
        "messages": [],
        "device_flow": None,
        "error": None,
        "user_name": None,
        "quality_team": None,
        "group_count": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _reset_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    _init_state()


# ============================================================================
# トークン取得（トークン本体はどこにも保存・表示しない）
# ============================================================================

def _msal_app() -> msal.PublicClientApplication:
    app = st.session_state.get("_msal_app")
    if app is None:
        app = msal.PublicClientApplication(
            client_id=OBO_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{OBO_TENANT_ID}",
        )
        st.session_state["_msal_app"] = app
    return app


def _user_foundry_token() -> str:
    """サインインした本人の Foundry 呼び出し用トークンを、MSAL のキャッシュからサイレントに取得する。
    デバイスコードでのサインイン時に得たリフレッシュトークンを使うので、再サインインは不要。"""
    app = _msal_app()
    account = st.session_state.get("_msal_account")
    if not account:
        raise RuntimeError("サインイン情報がありません。サインインし直してください。")
    result = app.acquire_token_silent([_FOUNDRY_SCOPE], account=account)
    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (result or {}).get("error") or "不明"
        raise RuntimeError(
            "サインインしたアカウントで Foundry 呼び出し用のトークンを取得できませんでした。"
            "サインインに使う Entra アプリに https://ai.azure.com 向けの委任権限と管理者同意が"
            f"必要です（README 参照）。詳細: {detail}"
        )
    return result["access_token"]


def _operator_foundry_token() -> str:
    """このPCで az login しているアカウントのトークン（従来の demo_chat.py と同じ）。"""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), _FOUNDRY_SCOPE)()


def _foundry_bearer() -> str:
    return _user_foundry_token() if _CALL_AS == "user" else _operator_foundry_token()


# ============================================================================
# Foundry 呼び出し（エンドポイント・payload 形式は既存デモUIと同じ）
# ============================================================================

def _register_obo(user_assertion: str) -> dict:
    resp = requests.post(
        _INVOCATIONS_URL,
        json={"action": "register_obo", "user_assertion": user_assertion},
        headers={"Authorization": f"Bearer {_foundry_bearer()}"},
        params={"api-version": "v1"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _send_chat(question: str, agent_session_id: str) -> dict:
    resp = requests.post(
        _RESPONSES_URL,
        json={"input": question, "stream": False, "agent_session_id": agent_session_id},
        headers={"Authorization": f"Bearer {_foundry_bearer()}"},
        params={"api-version": "v1"},
        timeout=180,  # Fabric IQ は同期実行でツール呼び出しが増えるため既存より長めに取る
    )
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# 応答の解析：回答・参照元・閲覧できなかった情報・同意要求
# ============================================================================

def _parse_tool_output(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _looks_unavailable(source: str, output: dict | None) -> bool:
    """ツール結果が「何も得られなかった」ことを示しているか。
    ※ 権限不足なのかデータ自体が無いのかは、ここでは区別できない（区別できないこと自体を
      画面でそのまま伝える）。"""
    if output is None:
        return False
    if output.get("error"):
        return True
    if source == "search":
        return not output.get("hits") or "message" in output
    if source == "fabric":
        return output.get("row_count") == 0
    return False


def _analyze(body: dict) -> dict:
    texts: list[str] = []
    calls: dict[str, str] = {}          # call_id -> tool name
    used: list[str] = []                 # 呼ばれたツール名（順序保持）
    outputs: dict[str, dict | None] = {}  # call_id -> 結果
    consents: list[dict] = []

    for item in body.get("output", []) or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content", []) or []:
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
        elif t == "function_call":
            name = item.get("name") or ""
            used.append(name)
            if item.get("call_id"):
                calls[item["call_id"]] = name
        elif t == "function_call_output":
            if item.get("call_id"):
                outputs[item["call_id"]] = _parse_tool_output(item.get("output"))
        elif t == "mcp_call":
            used.append(item.get("name") or item.get("server_label") or "fabric_iq")
        elif t == "oauth_consent_request":
            link = item.get("consent_link")
            if isinstance(link, str) and link.startswith("https://"):
                consents.append({"label": item.get("server_label") or "Fabric IQ", "url": link})

    sources: list[str] = []
    for name in used:
        src = _TOOL_SOURCE.get(name, "iq")
        if src not in sources:
            sources.append(src)

    unavailable: list[str] = []
    for call_id, out in outputs.items():
        src = _TOOL_SOURCE.get(calls.get(call_id, ""), "iq")
        if _looks_unavailable(src, out) and src not in unavailable:
            unavailable.append(src)
    # 同じ種類のツールを複数回呼び、どれか1回でも結果が得られていれば「見られなかった」扱いにはしない
    got = {
        _TOOL_SOURCE.get(calls.get(cid, ""), "iq")
        for cid, out in outputs.items()
        if not _looks_unavailable(_TOOL_SOURCE.get(calls.get(cid, ""), "iq"), out)
    }
    unavailable = [u for u in unavailable if u not in got]

    if texts:
        reply = "\n".join(texts)
    elif consents:
        reply = "この質問に答えるには、追加の許可が必要です。下のリンクから許可したあと、もう一度同じ質問を送ってください。"
    else:
        reply = "（応答からテキストを抽出できませんでした）"

    return {
        "reply": reply,
        "sources": [s for s in _SOURCE_ORDER if s in sources],
        "unavailable": [s for s in _SOURCE_ORDER if s in unavailable],
        "consents": consents,
        "tools": used,
    }


def _render_assistant(msg: dict) -> None:
    st.write(msg["content"])
    if msg.get("sources") is None:  # エラー時
        return

    sources = msg.get("sources") or []
    st.caption("参照元: " + (" ／ ".join(_SOURCE_LABEL[s] for s in sources) if sources else "🤖 LLMのみ"))

    unavailable = msg.get("unavailable") or []
    if unavailable:
        st.warning(
            "閲覧できなかった情報: "
            + "、".join(_SOURCE_NOUN[s] for s in unavailable)
            + "（閲覧権限が無いか、該当データが存在しないため、回答に含まれていません）"
        )

    for c in msg.get("consents") or []:
        st.info(f"追加の許可が必要です（{c['label']}）")
        st.markdown(f'<a href="{c["url"]}" target="_blank" rel="noopener">許可する（別タブで開きます）</a>', unsafe_allow_html=True)

    who = msg.get("who")
    if who:
        st.caption(f"実行ユーザー: {who}")

    if msg.get("tools"):
        with st.expander("処理詳細を見る"):
            for name in msg["tools"]:
                st.text(name)


# ============================================================================
# サインイン（デバイスコード。デモ開始前に各ウィンドウで済ませておく）
# ============================================================================

def _start_device_flow() -> None:
    missing = [n for n, v in (("OBO_TENANT_ID", OBO_TENANT_ID), ("OBO_CLIENT_ID", OBO_CLIENT_ID), ("OBO_APP_SCOPE", OBO_APP_SCOPE)) if not v]
    if missing:
        raise RuntimeError("必要な設定が.envにありません: " + ", ".join(missing))
    flow = _msal_app().initiate_device_flow(scopes=[OBO_APP_SCOPE])
    if "user_code" not in flow:
        raise RuntimeError(f"サインインの開始に失敗しました: {flow}")
    st.session_state["device_flow"] = flow


def _complete_sign_in() -> None:
    app = _msal_app()
    flow = st.session_state["device_flow"]
    result = app.acquire_token_by_device_flow(flow)  # ブラウザでのサインイン完了までブロック
    st.session_state["device_flow"] = None
    if "access_token" not in result:
        st.session_state["error"] = f"サインインに失敗しました: {result.get('error')}: {result.get('error_description')}"
        return

    claims = result.get("id_token_claims") or {}
    accounts = app.get_accounts(username=claims.get("preferred_username")) or app.get_accounts()
    st.session_state["_msal_account"] = accounts[0] if accounts else None

    user_assertion = result["access_token"]  # ローカル変数のみ
    try:
        reg = _register_obo(user_assertion)
    except RuntimeError as e:  # user モードでの Foundry トークン取得失敗
        st.session_state["error"] = str(e)
        return
    except requests.RequestException as e:
        st.session_state["error"] = f"エージェントへの登録に失敗しました: {e}"
        return

    if reg.get("status") != "registered" or not reg.get("session_id"):
        st.session_state["error"] = f"エージェントへの登録に失敗しました: {reg}"
        return

    groups = reg.get("user_groups") or []
    st.session_state.update(
        {
            "signed_in": True,
            "agent_session_id": reg["session_id"],
            "user_name": claims.get("name") or claims.get("preferred_username") or "(表示名なし)",
            "quality_team": QUALITY_TEAM_GROUP_ID in groups,
            "group_count": len(groups),
            "error": None,
        }
    )


# ============================================================================
# 画面
# ============================================================================

_init_state()

st.title("🕸 社内ナレッジアシスタント")
st.caption("社内の文書・業務データ・その関係から、あなたが見られる範囲で回答します。")

if _CALL_AS == "operator":
    st.warning(
        "確認用モード（FOUNDRY_CALL_AS=operator）で動作しています。"
        "Fabric IQ の部分は、このPCでログインしている担当者の権限で実行されます。"
    )

if st.session_state["error"]:
    st.error(st.session_state["error"])

if not st.session_state["signed_in"]:
    st.subheader("サインイン")
    st.write("デモを始める前に、このウィンドウで使うアカウントでサインインしてください。")
    if st.session_state["device_flow"] is None:
        if st.button("Microsoftアカウントでサインイン", type="primary"):
            try:
                _start_device_flow()
            except RuntimeError as e:
                st.session_state["error"] = str(e)
            st.rerun()
    else:
        flow = st.session_state["device_flow"]
        st.info(f"ブラウザで **{flow['verification_uri']}** を開き、コード **{flow['user_code']}** を入力してください。")
        with st.spinner("サインインの完了を待っています..."):
            _complete_sign_in()
        st.rerun()
    st.stop()

# --- サインイン済み ---
badge = "✅ 品質チーム所属" if st.session_state["quality_team"] else "— 品質チーム未所属"
col1, col2 = st.columns([3, 1])
with col1:
    st.markdown(f"**実行ユーザー：{st.session_state['user_name']}**　{badge}")
with col2:
    if st.button("サインアウト"):
        _reset_session()
        st.rerun()

st.divider()

example_cols = st.columns(len(_EXAMPLE_QUESTIONS))
clicked = None
for col, q in zip(example_cols, _EXAMPLE_QUESTIONS):
    if col.button(q, use_container_width=True):
        clicked = q

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant":
            _render_assistant(msg)
        else:
            st.write(msg["content"])

question = clicked or st.chat_input("質問を入力してください...")

if question:
    st.session_state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    who = f"{st.session_state['user_name']}（{'品質チーム所属' if st.session_state['quality_team'] else '品質チーム未所属'}）"
    with st.chat_message("assistant"):
        with st.spinner("考え中..."):
            try:
                result = _analyze(_send_chat(question, st.session_state["agent_session_id"]))
                msg = {
                    "role": "assistant",
                    "content": result["reply"],
                    "sources": result["sources"],
                    "unavailable": result["unavailable"],
                    "consents": result["consents"],
                    "tools": result["tools"],
                    "who": who,
                }
            except RuntimeError as e:  # user モードのトークン取得失敗
                msg = {"role": "assistant", "content": str(e), "sources": None}
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status in _SESSION_ERROR_STATUS_CODES:
                    text = "接続が切れました。サインアウトして、もう一度サインインしてください。"
                    st.session_state["error"] = text
                else:
                    text = f"回答の取得に失敗しました（status={status}）。"
                msg = {"role": "assistant", "content": text, "sources": None}
            except requests.RequestException:
                msg = {"role": "assistant", "content": "ネットワークに接続できませんでした。", "sources": None}
        _render_assistant(msg)
    st.session_state["messages"].append(msg)
