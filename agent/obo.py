"""
OBO（On-Behalf-Of）フロー — 設計書 第10章「Data Authorizationの本番実装」対応。

agent_search_starter/obo.pyと同じMSALベースの自前OBO実装（トークン交換ロジック自体に
変更は無い）。位置づけが変わったのは「どこで実行されるか」の役割分担：

  - sign_in_device_code()：引き続き「クライアント側」で実行する関数。
    Hosted Agentのコンテナの中では実行しない（後述の理由）。
  - exchange_for_graph_token() / exchange_for_powerbi_token() / resolve_user_groups()：
    Hosted Agent移行後は、main.pyの `/obo/register` ルート（サーバー側）がこれらを呼ぶ。

なぜHosted Agent移行後も自前OBOのままなのか（重要、README参照）:
  FoundryにはAgent Identity Blueprintという管理型OBOの仕組みがあるが、公式ドキュメント
  （Agent identity concepts）によれば対象は「Agent Applications」公開モデルであり、
  かつMicrosoft Q&A（User Identity Passthrough for Hosted Agents Calling a Custom MCP
  Server）で実際にHosted Agent×カスタムMCPサーバーの組み合わせで試した開発者が
  「Hosted Agentのコード側からは、サインイン済みユーザー本人のトークンにランタイムで
  アクセスする公式APIが無い」「Agent Serviceが渡すトークンのaudienceは
  https://ai.azure.com 固定で、カスタムAPI宛に転送できない」「Foundry管理のAgent
  Identity（agentIdentityBlueprint）はクライアントシークレットを持てず、confidential
  clientとしてのOBO交換を行えない」と報告している（2026年9月時点）。
  実際にazure-ai-agentserver-core（このHosted Agent SDKの一部）のソースコードを
  確認したところ、`get_request_context()` が返す `FoundryAgentRequestContext` には
  `call_id` / `user_id` / `session_id` の3フィールドしか無く、ユーザーのアクセス
  トークンそのものは含まれていない（ground truthとしてSDKソースで確認済み）。
  そのため、Fabric/AI SearchのようなMicrosoft外部（Foundry非管轄）のAPIに対して
  ユーザー本人の委任トークンを使うには、agent_search_starterと同じ「自前のEntra ID
  アプリ登録＋MSALのConfidentialClientApplication.acquire_token_on_behalf_of」方式を、
  実行場所だけHosted Agentのコンテナ内（main.pyの/obo/registerルート）に移して
  使い続けるのが、現時点で唯一の実装可能な方法。

全体の流れ（main.pyのアーキテクチャ）:
  1. クライアント（当面は手元のPythonスクリプト。将来的にはフロントエンド）が
     sign_in_device_code() でユーザー本人をデバイスコードフローでサインインさせ、
     「このOrchestratorアプリ自身」を対象とするユーザートークンを取得する
     （agent_search_starterのobo_smoke_test.pyと全く同じ手順）。
  2. クライアントが、そのユーザートークンを `POST /obo/register` のリクエストボディ
     （{"user_assertion": "..."}）としてHosted Agentへ送る。
  3. main.pyの `/obo/register` ハンドラ（サーバー側、Hosted Agentのコンテナ内）が、
     本モジュールの exchange_for_graph_token() / exchange_for_powerbi_token() /
     resolve_user_groups() を呼び、結果（Graph/Power BIトークン、解決済みグループ）を
     Foundryが払い出す `get_request_context().user_id` をキーに、サーバー内の
     一時キャッシュへ保存する（main.py参照）。
  4. 以降、同じユーザーからのチャットリクエスト（Responsesプロトコル）では、
     `get_request_context().user_id` が同じ値で返ってくる前提のもと、
     Tool実行時（search_documents_tool / query_fabric_tool）にそのキャッシュから
     トークン・グループを取り出して使う。

グループをObject ID（GUID）で扱う理由、@odata.typeの$select非対応の話、
ページング対応などは agent_search_starter/obo.py と同じ（詳細はそちらのコメント参照）。

事前に必要なEntra ID側の設定は agent_search_starter で作成済みのものをそのまま
流用できる（README.md参照）。
"""
import requests
import msal

from common import OBO_TENANT_ID, OBO_CLIENT_ID, OBO_CLIENT_SECRET, OBO_APP_SCOPE

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_GRAPH_MEMBEROF_URL = "https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.group?$select=id,displayName"


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("OBO_TENANT_ID", OBO_TENANT_ID),
            ("OBO_CLIENT_ID", OBO_CLIENT_ID),
            ("OBO_CLIENT_SECRET", OBO_CLIENT_SECRET),
            ("OBO_APP_SCOPE", OBO_APP_SCOPE),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "OBOに必要な設定が.envにありません: " + ", ".join(missing) + "。"
            "README.md「OBO実装：事前に必要なEntra ID App Registration設定」を"
            "参照し、アプリ登録を行ったうえで.envに設定してください"
            "（agent_search_starterで作成済みのものを流用できます）。"
        )


def _authority() -> str:
    return f"https://login.microsoftonline.com/{OBO_TENANT_ID}"


def _confidential_client() -> msal.ConfidentialClientApplication:
    _require_config()
    return msal.ConfidentialClientApplication(
        client_id=OBO_CLIENT_ID,
        client_credential=OBO_CLIENT_SECRET,
        authority=_authority(),
    )


def sign_in_device_code() -> str:
    """デバイスコードフローでユーザー本人にサインインしてもらい、
    このOrchestratorアプリ自身（OBO_APP_SCOPE）を対象とするアクセストークンを返す。

    **この関数はクライアント側（手元のPC）で呼ぶものであり、Hosted Agentの
    コンテナ内（main.py）からは呼ばない。** サーバー側のTool実行はリクエストごとに
    短時間で完了する必要があり、ブラウザでのサインイン完了を待ってブロックする
    このフローとは相性が悪い。また、そもそもHosted Agentのコンテナはサインイン画面を
    表示できるフロントエンドを持たない。クライアント側でこの関数を実行して得た
    ユーザートークンを、`POST /obo/register` でHosted Agentに渡す想定
    （client_register_obo.py参照）。
    """
    _require_config()
    app = msal.PublicClientApplication(client_id=OBO_CLIENT_ID, authority=_authority())

    flow = app.initiate_device_flow(scopes=[OBO_APP_SCOPE])
    if "user_code" not in flow:
        raise RuntimeError(f"デバイスコードフローの開始に失敗しました: {flow}")

    print(flow["message"])  # 「https://microsoft.com/devicelogin で XXXXXXX を入力してください」等

    result = app.acquire_token_by_device_flow(flow)  # ユーザーがブラウザでサインインするまでブロックする
    if "access_token" not in result:
        raise RuntimeError(
            "サインインに失敗しました: "
            f"{result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"]


def _acquire_obo_token(user_token: str, scope: str) -> str:
    cca = _confidential_client()
    result = cca.acquire_token_on_behalf_of(user_assertion=user_token, scopes=[scope])
    if "access_token" not in result:
        raise RuntimeError(
            f"OBOトークン交換に失敗しました（scope={scope}）: "
            f"{result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"]


def exchange_for_graph_token(user_token: str) -> str:
    """ユーザートークンをMicrosoft Graph用のOBOトークンに交換する
    （ユーザーの所属グループ解決に使う。/me/memberOfを呼べる権限が必要）。
    main.pyの/obo/registerルート（サーバー側）から呼ばれる想定。"""
    return _acquire_obo_token(user_token, _GRAPH_SCOPE)


def exchange_for_powerbi_token(user_token: str) -> str:
    """ユーザートークンをPower BI用のOBOトークンに交換する
    （fabric_client.execute_dax()のaccess_tokenにそのまま渡すことで、
    ユーザー本人の権限・RLSでFabric ExecuteQueriesを呼び出せる）。
    main.pyの/obo/registerルート（サーバー側）から呼ばれる想定。"""
    return _acquire_obo_token(user_token, _POWERBI_SCOPE)


def resolve_user_groups(graph_token: str) -> list[str]:
    """Microsoft Graphの/me/memberOf/microsoft.graph.group（OData型キャスト）を呼び、
    ユーザーが所属するグループのObject ID（GUID）一覧を返す。

    /me/memberOf単体は directoryObject（group・directoryRole・administrativeUnit等が
    混在するポリモーフィックな型）を返すため、型を絞り込む "/microsoft.graph.group"
    キャストを付けてグループのみをサーバー側でフィルタしている（公式ドキュメントに
    記載されているOData型キャストの標準的な使い方）。
    なお @odata.type は $select に含めるとエラーになる
    （"Term '@odata.type' is not valid in a $select or $expand expression"）ため
    $select には含めていない。ただし @odata.type 自体は $select の指定内容に関わらず
    レスポンスの各オブジェクトに常に付与される（公式ドキュメントに明記）ため、
    多重防御としてここでも念のため確認している。

    search_tool.search_documents()のuser_groups引数にそのまま渡せる想定。
    """
    headers = {"Authorization": f"Bearer {graph_token}"}
    groups: list[str] = []
    url = _GRAPH_MEMBEROF_URL
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Microsoft Graph /me/memberOf 呼び出しに失敗しました "
                f"(status={resp.status_code}): {resp.text[:2000]}"
            )
        body = resp.json()
        for item in body.get("value", []):
            if item.get("@odata.type", "#microsoft.graph.group") == "#microsoft.graph.group" and item.get("id"):
                groups.append(item["id"])
        url = body.get("@odata.nextLink")
    return groups


if __name__ == "__main__":
    # 使い方: python agent/obo.py
    # デバイスコードでサインイン → Graph/Power BIトークン交換 → グループ解決、までを
    # このモジュール単体で実演する（Hosted Agentを介さない、ロジック単体の疎通確認用）。
    # 実際にHosted Agentへユーザートークンを登録する場合は client_register_obo.py を使う。
    user_token = sign_in_device_code()
    print("\nサインイン成功。OBOトークン交換を行います...")

    graph_token = exchange_for_graph_token(user_token)
    powerbi_token = exchange_for_powerbi_token(user_token)
    print("Graphトークン取得OK / Power BIトークン取得OK")

    groups = resolve_user_groups(graph_token)
    print(f"\n解決されたグループ（Object ID）: {groups}")
    print(
        "\n(参考) Power BIトークンの先頭: "
        f"{powerbi_token[:20]}...（この値をquery_fabric(plan, access_token=...)に渡せます）"
    )
