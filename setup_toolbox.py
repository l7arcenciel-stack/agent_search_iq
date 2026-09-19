"""
Fabric IQ（オントロジー）の Toolbox を Foundry プロジェクトに作成する、一回実行用スクリプト。

【前提（Azure側で先に済ませておくこと）】
  1. Fabric 管理ポータルで「Ontology item (preview)」テナント設定を有効化
  2. オントロジーを作成し、実データをバインドしてリフレッシュ済み
     ※ インポートモードのセマンティックモデルから生成した場合、エンティティ等の「定義」は
       できるがデータバインドはできない（Direct Lake のみ対応）。README 参照。
  3. Fabric IQ 接続用の Entra アプリ登録（Power BI 委任権限 Item.Execute.All /
     Item.Read.All ＋ 管理者同意、クライアントシークレット）
  4. Foundry ポータルで接続を作成
       Settings > Connections > New connection > Fabric IQ
     → 作成された接続の ID を FABRIC_IQ_PROJECT_CONNECTION_ID に設定する

【使い方】
    # 手元PCで（az login / azd auth login 済みの状態で）
    set FABRIC_IQ_PROJECT_CONNECTION_ID=<接続ID>
    set FABRIC_IQ_ONTOLOGY_WORKSPACE_ID=<オントロジーのあるワークスペースID>
    set FABRIC_IQ_ONTOLOGY_ITEM_ID=<オントロジーのアイテムID>
    python setup_toolbox.py

  ワークスペースID・アイテムIDは、オントロジーを開いたときのURL
      https://app.fabric.microsoft.com/groups/<workspace-ID>/ontologies/<ontology-item-ID>
  から取れる。

  成功すると Toolbox の MCP エンドポイントが表示されるので、それを
      azd env set FABRIC_IQ_TOOLBOX_ENDPOINT "<表示されたURL>"
  で **agent_search_iq 用の azd 環境**に登録してから azd deploy する。

【注意】
  - 同じ名前で再実行すると新しいバージョンが作られる（エンドポイントのバージョン番号も変わる）。
  - このスクリプトは Toolbox を作るだけで、エージェントのデプロイは行わない。
"""
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FabricIQPreviewToolboxTool
from azure.identity import DefaultAzureCredential

from common import FOUNDRY_PROJECT_ENDPOINT

TOOLBOX_NAME = os.environ.get("FABRIC_IQ_TOOLBOX_NAME", "fabric-iq-toolbox")
SERVER_LABEL = "fabric_iq_ontology"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"環境変数 {name} が未設定です。スクリプト冒頭の説明を参照してください。")
        sys.exit(1)
    return value


def main() -> None:
    connection_id = _required("FABRIC_IQ_PROJECT_CONNECTION_ID")
    workspace_id = _required("FABRIC_IQ_ONTOLOGY_WORKSPACE_ID")
    ontology_id = _required("FABRIC_IQ_ONTOLOGY_ITEM_ID")

    # Fabric IQ の Ontology エンドポイント（Microsoft Learn「Connect agents to Microsoft Fabric
    # with Fabric IQ (preview)」記載の形）。ワークスペース単位のプライベートリンクを使う場合は
    # ホスト部分をワークスペース専用ホストに差し替えること。
    server_url = (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/"
        f"workspaces/{workspace_id}/items/{ontology_id}/ontologyEndpoint"
    )

    project = AIProjectClient(endpoint=FOUNDRY_PROJECT_ENDPOINT, credential=DefaultAzureCredential())

    tool = FabricIQPreviewToolboxTool(
        project_connection_id=connection_id,
        server_label=SERVER_LABEL,
        server_url=server_url,
        # Hosted Agent からの自動実行を前提とするため承認プロンプトは出さない
        # （Prompt Agent の公式サンプルと同じ設定）。
        require_approval="never",
    )

    print(f"Toolbox を作成します: name={TOOLBOX_NAME}")
    print(f"  接続先（Ontology）: {server_url}")
    version = project.toolboxes.create_version(
        name=TOOLBOX_NAME,
        description="agent_search_iq 用の Fabric IQ（Ontology）ツール",
        tools=[tool],
    )

    v_name = getattr(version, "name", TOOLBOX_NAME)
    v_ver = getattr(version, "version", None)
    endpoint = f"{FOUNDRY_PROJECT_ENDPOINT.rstrip('/')}/toolboxes/{v_name}"
    if v_ver:
        endpoint += f"/versions/{v_ver}"
    endpoint += "/mcp?api-version=v1"

    print("\n作成しました。")
    print(f"  Toolbox: {v_name}  version: {v_ver}")
    print(f"  MCPエンドポイント: {endpoint}")
    print("\n次の手順（agent_search_iq 用の azd 環境が選択されていることを確認してから）:")
    print(f'  azd env set FABRIC_IQ_TOOLBOX_ENDPOINT "{endpoint}"')
    print("  azd deploy")


if __name__ == "__main__":
    main()
