"""
検証用スクリプト：Invocations経由で登録したOBOセッション（agent_session_id）を明示指定して、
Responsesプロトコルへ直接チャットリクエストを送る。

【目的】
Foundry Toolkit/ポータルのプレイグラウンドは、メッセージを送るたびに独自のsession_idで
新しいセッション（＝新しいサンドボックス）を割り当てるため、client_register_obo.pyで
Invocations経由のOBO登録に使ったセッションと、プレイグラウンドでのチャットのセッションが
別物になってしまい、`_OBO_CACHE`（プロセス内メモリ）が共有されない可能性がある
（`agent_search_hosted/README.md`「3.2 まだ実機確認できていない項目」参照）。

このスクリプトは、`client_register_obo.py`実行時に払い出された`agent_session_id`を
Responsesリクエストのbodyに明示的に含めることで、**同一セッション（＝同一サンドボックス
であることが期待される）**上でチャットを実行させ、A008のような制限文書に対して
正しい（OBO登録済みの）権限で回答が返るかを確認する。

- 同一session_idでA008の実際の内容が返れば：「別session/別サンドボックスによる
  _OBO_CACHE非共有」が原因であると確定できる（Distributed Identity Store設計へ進む
  判断材料になる）。
- 同一session_idでも拒否されるなら：session_idの一致だけでは解決しない別要因
  （例：get_request_context().user_idがInvocations側と一致していない等）が疑われ、
  次のステップ（Responses側でuser_id・cache hit/missをログ出力して確認）に進む。

使い方:
    python scripts/dev/client_test_responses_session.py <agent_session_id> [追加の質問文]

    例:
        python scripts/dev/client_test_responses_session.py 06eb5167c268fb230008akIccD3SyEeBQ65VUo1FOkimGneTfs

    質問文を省略した場合は「製品A008の品質基準について教えて」を送る。
    Responsesエンドポイントは環境変数から組み立てる（テナント・プロジェクトを
    差し替えたときにコード変更が要らないようにするため）:
      IQ_AGENT_ENDPOINT_BASE          … 指定すればこれをそのまま使う
      AI_FOUNDRY_PROJECT_ENDPOINT + IQ_AGENT_NAME … 未指定時はこの2つから組み立てる

このスクリプトはclient_register_obo.pyと同じ認証パターン（Entra ID Bearerトークン、
scope=https://ai.azure.com/.default、DefaultAzureCredential）を使う。トークン値は
標準出力に一切出力しない。
"""
import json
import os
import sys
from pathlib import Path

import requests
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# agent/ のモジュール（common など）を共有して使う（エージェント本体と設定を二重に持たないため）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from common import FOUNDRY_PROJECT_ENDPOINT, IQ_AGENT_NAME

_FOUNDRY_INVOKE_SCOPE = "https://ai.azure.com/.default"

_ENDPOINT_BASE = (
    os.environ.get("IQ_AGENT_ENDPOINT_BASE")
    or f"{FOUNDRY_PROJECT_ENDPOINT.rstrip('/')}/agents/{IQ_AGENT_NAME}/endpoint"
).rstrip("/")

_RESPONSES_URL = f"{_ENDPOINT_BASE}/protocols/openai/responses"

_DEFAULT_QUESTION = "製品A008の品質基準について教えて"


def main() -> None:
    if len(sys.argv) < 2:
        print("使い方: python scripts/dev/client_test_responses_session.py <agent_session_id> [質問文]")
        print(
            "例:     python scripts/dev/client_test_responses_session.py "
            "06eb5167c268fb230008akIccD3SyEeBQ65VUo1FOkimGneTfs"
        )
        sys.exit(1)

    agent_session_id = sys.argv[1]
    question = sys.argv[2] if len(sys.argv) >= 3 else _DEFAULT_QUESTION

    print("=== Foundry呼び出し用のBearerトークンを取得（DefaultAzureCredential）===")
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _FOUNDRY_INVOKE_SCOPE)
    bearer_token = token_provider()  # トークン値自体はこの変数以外には出力しない
    print("取得OK。\n")

    body = {
        "input": question,
        "stream": False,
        "agent_session_id": agent_session_id,
    }

    print(f"=== {_RESPONSES_URL} へPOST（agent_session_id={agent_session_id!r}）===")
    print(f"送信body: {json.dumps(body, ensure_ascii=False)}\n")

    resp = requests.post(
        _RESPONSES_URL,
        json=body,
        headers={"Authorization": f"Bearer {bearer_token}"},
        params={"api-version": "v1"},
        # Invocationsと同様、セッションのコールドスタートを考慮して長めに取る。
        timeout=120,
    )

    print(f"=== ステータス: {resp.status_code} ===")
    try:
        parsed = resp.json()
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except ValueError:
        print(resp.text)

    if resp.status_code >= 400:
        sys.exit(1)


if __name__ == "__main__":
    main()