"""
クライアント側スクリプト：ユーザー本人をデバイスコードフローでサインインさせ、
得られたユーザートークンをHosted Agentの Invocations プロトコル
（POST /invocations、action=register_obo）に登録する。

【経路の変更について】
当初は独自ルート `/obo/register`（ResponsesHostServer.add_route）を使っていたが、
実際にazd deployしたエンドポイントに対して認証・api-versionを揃えた上でテストした
結果、Foundryの公開ゲートウェイはazure.yamlで宣言したプロトコル（responses /
invocations / a2a）以外のカスタムルートを中継しないことが実機で確認できた
（無認証→401、Bearerのみ→400 missing api-version、api-version追加→404）。
そのため、OBO登録は正式にazure.yamlで宣言できる Invocations プロトコル
（`InvocationAgentServerHost`、`main.py`にResponsesと同一プロセスでマウント済み）
経由に変更した。`/obo/register` はローカルデバッグ用の互換ルートとして
main.py側には残っているが、本番の公開エンドポイント経由では到達不可。

Hosted Agentのコンテナ内ではサインインできない（obo.pyのモジュールdocstring・
README「Hosted Agent移行時のOBOの扱い」参照）ため、このステップは今まで通り
手元のPC（またはユーザーの認証を行える何らかのフロントエンド）で行う。

使い方:
    1. Hosted Agentをローカルで起動（`azd ai agent run`）、または既にデプロイ済みの
       エンドポイントを用意する。
    2. python scripts/dev/client_register_obo.py <Hosted AgentのURL>
       - ローカル実行時: http://localhost:8088 のように認証不要でそのまま叩ける。
         このスクリプトは {base_url}/invocations へPOSTする
         （InvocationAgentServerHostのルートは絶対パス /invocations のため）。
       - 実際にazdでデプロイした後の公開エンドポイントを渡す場合は、`azd deploy`が
         出力する「Agent endpoint (responses)」のURLから
         `/protocols/openai/responses?api-version=v1` の部分を除いたベースURL
         （例: https://<account>.services.ai.azure.com/api/projects/<project>/
         agents/<agent>/endpoint）を渡すこと。このスクリプトは
         `{base_url}/protocols/invocations?api-version=v1` へPOSTする。
         ★このURL形状はResponsesの実績あるURLからの類推であり、実機未確認。
         実際に叩いて404等が返る場合はURL形状そのものを見直す必要がある
         （README「動作確認が必要な項目」参照）。
         また、公開エンドポイントへはFoundry自体への認証（Entra ID Bearer
         トークン、scope=https://ai.azure.com/.default）がゲートウェイ側で
         必須になるため、渡されたURLが`http://localhost`系以外の場合は自動的に
         DefaultAzureCredentialでこのBearerトークンを取得して付与する
         （呼び出し元アカウントに、対象のHosted Agentリソースへの「Foundry User」
         相当のロール、具体的には`.../applications/invoke/action`権限が必要。
         権限が無い場合は403になる）。
    3. 表示されるURLとコードでブラウザからサインインする（これはFoundry呼び出し用の
       Bearerトークンとは別に、OBOのuser_assertion取得のためのサインイン）。
    4. 登録に成功すると、解決されたユーザーのグループ（Object ID）・session_id・
       invocation_id・user_idが表示される（session_id/user_idがInvocations経由でも
       Responses側と同じ値になるかは実機未確認。README参照）。
    5. 以降、同じHosted Agentへチャットリクエスト（Responses API）を送ると、
       同じユーザーとして識別され（get_request_context().user_id）、
       search_documents_tool / query_fabric_tool がこのユーザーの権限で実行される
       ……はずだが、これはHosted Agent側の未検証事項（README参照）。
       実際にazdでデプロイ・実行して確認すること。

注意：このスクリプトは対話的なdevice code flowを含むため、Hosted Agentの
デプロイ・起動確認（`azd ai agent run`）と合わせて、実際にはユーザー自身の
PowerShell環境で実行することを想定している（このサンドボックスでは実行できない）。
"""
import sys
from pathlib import Path

import requests
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# agent/ のモジュール（common など）を共有して使う（エージェント本体と設定を二重に持たないため）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from obo import sign_in_device_code

# 公開デプロイ後のFoundryエンドポイントを呼ぶ際に必要なBearerトークンのscope。
# 公式ドキュメント（Chat with your Agent Application using the Responses API
# protocol | Microsoft Foundry）に基づく。ローカル（http://localhost）には使わない。
_FOUNDRY_INVOKE_SCOPE = "https://ai.azure.com/.default"


def main() -> None:
    if len(sys.argv) < 2:
        print("使い方: python scripts/dev/client_register_obo.py <Hosted AgentのURL>")
        print("例:     python scripts/dev/client_register_obo.py http://localhost:8088")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")

    print("=== デバイスコードサインイン ===")
    user_token = sign_in_device_code()
    print("サインイン成功。\n")

    headers = {}
    params = {}
    is_local = base_url.startswith("http://localhost") or base_url.startswith("http://127.0.0.1")
    if is_local:
        # InvocationAgentServerHostのルートは絶対パス /invocations で登録されている
        # （main.pyでResponsesHostServerへ server.mount("/", invocations_app) している
        # ため、プレフィックスは付かない）。
        invocations_url = f"{base_url}/invocations"
    else:
        print("=== Foundry呼び出し用のBearerトークンを取得（DefaultAzureCredential）===")
        token_provider = get_bearer_token_provider(DefaultAzureCredential(), _FOUNDRY_INVOKE_SCOPE)
        headers["Authorization"] = f"Bearer {token_provider()}"
        print("取得OK。\n")
        # デプロイ後のFoundryエンドポイントは、標準のprotocolsエンドポイント
        # （.../endpoint/protocols/openai/responses?api-version=v1）と同様、
        # ?api-version=v1 クエリパラメータが無いと400 BadRequestになる
        # （実機で確認済み）。Invocationsプロトコルの公開URLも同じゲートウェイを
        # 経由するため同様に必要と推測される（★実機未確認。URL形状自体も
        # Responsesの実績あるURLからの類推であり、外れていた場合は404が返る）。
        params["api-version"] = "v1"
        # base_urlは「.../agents/<agent>/endpoint」までを渡してもらう想定
        # （Responsesの実績あるURL「.../endpoint/protocols/openai/responses」から
        # 類推。Invocationsは openai/ 配下ではない素のプロトコルなので
        # "/protocols/invocations" とした）。
        invocations_url = f"{base_url}/protocols/invocations"

    print(f"=== {invocations_url} へ登録（action=register_obo）===")
    resp = requests.post(
        invocations_url,
        json={"action": "register_obo", "user_assertion": user_token},
        headers=headers,
        params=params,
        timeout=120,
    )
    if resp.status_code >= 400:
        print(f"登録に失敗しました (status={resp.status_code}): {resp.text}")
        sys.exit(1)

    body = resp.json()
    print(f"登録成功。解決されたグループ（Object ID）: {body.get('user_groups')}")
    print(f"session_id={body.get('session_id')!r}  invocation_id={body.get('invocation_id')!r}  user_id={body.get('user_id')!r}")
    print(
        "\nこのHosted AgentへChat（Responses API）リクエストを送ると、"
        "このユーザーとして識別され、Tool実行時にこの権限が使われる想定です"
        "（同一プロセス/サンドボックスに載ることが前提。上のsession_id/user_idが"
        "Responses側のget_request_context()と一致するかは実機未検証。README「動作"
        "確認が必要な項目」を参照し、実際にチャットを送って確認してください）。"
    )


if __name__ == "__main__":
    main()