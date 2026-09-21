"""
Power BI / Fabric の ExecuteQueries REST API（GA）を呼び出す薄いクライアント。

認証はFoundry側と同じ DefaultAzureCredential を使う（az login 済みならそのまま動く。
サービスプリンシパルに切り替える場合も同じ関数で対応可能）。

公式ドキュメント:
- Datasets - Execute Queries In Group:
  https://learn.microsoft.com/rest/api/power-bi/datasets/execute-queries-in-group
- 認証スコープ: https://analysis.windows.net/powerbi/api/.default
- 制限事項（要確認・重要）:
  MDXクエリ・INFO系DAX関数（INFO.VIEW.MEASURES()等）・DMVクエリは非対応。
  1回のAPI呼び出しにつき1クエリ、1クエリにつき1テーブルのみ。
  最大100,000行 または 1,000,000値（先に到達した方）、最大15MB/クエリ、
  120リクエスト/分/ユーザー。RLS有効またはSSO有効なデータセットに対して
  サービスプリンシパルは使用不可。
  呼び出し元（Foundry Agentを実行するユーザー/SP）に、対象データセットへの
  Read and Build権限があることが前提。

INFO.VIEW系メタデータ関数が使えないため、実際のテーブル名・メジャー名・列名の
確認は本モジュールでは行えない。fabric_schema.py 冒頭のコメントに、
Power BI Desktop/ServiceのDAX Query Viewで確認する手順を記載している。
"""
import requests
from azure.identity import DefaultAzureCredential

from common import FABRIC_WORKSPACE_ID, FABRIC_DATASET_ID

_POWERBI_API_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_credential = DefaultAzureCredential()


def _get_token() -> str:
    token = _credential.get_token(_POWERBI_API_SCOPE)
    return token.token


def execute_dax(dax_query: str, access_token: str | None = None) -> dict:
    """ExecuteQueries (In Group) を呼び出し、レスポンスJSONをそのまま返す。

    dax_query は呼び出し元（query_fabric.py）がAllowlist検証済みのQuery Planから
    機械的に組み立てたDAX文字列であることを前提とする。本モジュール自身は
    DAXの中身を検証しない（責務はAPI呼び出しのみに限定し、Guardrail＝
    「何を照会してよいか」の判断はquery_fabric.py側に集約する設計）。

    access_tokenを省略した場合は従来通りDefaultAzureCredential（=このスクリプトを
    実行しているユーザー/SP自身の権限）で呼び出す。OBO経由で取得した「実際のユーザー
    本人のPower BIトークン」を渡すと、そのトークンでAPIを呼び出す（設計書 第10-2章
    「OBOで得たユーザーコンテキストのトークンをそのままFabric ExecuteQueriesの
    Authorizationヘッダーに使用」に対応。obo.py参照）。
    """
    if not FABRIC_WORKSPACE_ID or not FABRIC_DATASET_ID:
        raise RuntimeError(
            "FABRIC_WORKSPACE_ID / FABRIC_DATASET_ID が.envに設定されていません。"
            "Fabricのワークスペース設定（ワークスペースの「詳細」やURL）と、"
            "対象セマンティックモデルの設定画面から取得して設定してください。"
        )

    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{FABRIC_WORKSPACE_ID}"
        f"/datasets/{FABRIC_DATASET_ID}/executeQueries"
    )
    headers = {
        "Authorization": f"Bearer {access_token or _get_token()}",
        "Content-Type": "application/json",
    }
    body = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Fabric ExecuteQueries呼び出しに失敗しました "
            f"(status={resp.status_code}): {resp.text[:2000]}"
        )
    return resp.json()
