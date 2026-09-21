"""共通設定・クライアント生成。全スクリプトから読み込む。

agent_search_starter/common.pyとほぼ同じ内容。Hosted Agent移行に伴う変更点は、
CURRENT_USER_GROUPSの位置づけだけ：agent_search_starterでは「OBO実装までの暫定値」
だったが、ここではOBOそのもの（main.pyの/obo/registerルート＋obo.py）は実装済みなので、
CURRENT_USER_GROUPSは「OBO登録が無い場合（ローカル開発時、azd ai agent run等）の
フォールバック値」という位置づけに変わる（main.py参照）。
"""
import os
import re
from dotenv import load_dotenv

load_dotenv()

# --- Azure AI Search ---
SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY = os.environ["AZURE_SEARCH_API_KEY"]
SEARCH_INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX_NAME", "poc-documents")

# --- Azure OpenAI (embedding用) ---
AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
AOAI_EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AOAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

# text-embedding-3-small = 1536次元。別モデルを使う場合はここを合わせて変更すること。
EMBEDDING_DIMENSIONS = int(os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))

# --- Microsoft Foundry（Hosted Agent） ---
# agent_framework.foundry.FoundryChatClientにも同名の環境変数(FOUNDRY_PROJECT_ENDPOINT/
# FOUNDRY_MODEL)を自動で読む機能があるが、agent_search_starterとの.env互換のため、
# ここではAI_FOUNDRY_*という名前のまま保持し、main.pyから明示的に渡す。
FOUNDRY_PROJECT_ENDPOINT = os.environ["AI_FOUNDRY_PROJECT_ENDPOINT"]
FOUNDRY_MODEL = os.environ.get("AI_FOUNDRY_MODEL", "gpt-4.1-mini")

# Foundry 上のエージェント名（azure.yaml の services.<name> / name と一致させること）。
# デモUI・検証スクリプトがエンドポイントURLを組み立てるのに使う。
# 元テナントへ反映する際は既存エージェントと**必ず別の名前**にする。
IQ_AGENT_NAME = os.environ.get("IQ_AGENT_NAME", "agent-search-iq")

# Agent() に渡す表示名。組織名を含めないこと（テナントをまたいで使い回すため）。
AGENT_DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "poc-agent-search-iq")

# --- Microsoft Fabric / Power BI（Scenario A: Semantic Model経由の構造化データ照会） ---
FABRIC_WORKSPACE_ID = os.environ.get("FABRIC_WORKSPACE_ID")
FABRIC_DATASET_ID = os.environ.get("FABRIC_DATASET_ID")

VECTOR_FIELD = "content_vector"
SEMANTIC_CONFIG_NAME = "default-semantic"

# --- ユーザーのグループ（Data Authorization、OBO未登録時のフォールバック） ---
# 本番の経路はmain.pyの/obo/registerルート＋obo.pyによるOBOトークン交換・グループ解決。
# ローカル開発時（azd ai agent run等でget_request_context().user_idが無い、または
# 該当ユーザーがまだ/obo/registerを呼んでいない）は、このフォールバック値を使う。
# フィルタ文字列への埋め込み時にインジェクションを防ぐため、グループ名は英数字・
# ハイフン・アンダースコアのみを許可する。
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def validate_group_names(groups: list[str]) -> list[str]:
    """フィルタ文字列に埋め込む前に必ず通すこと（$filterインジェクション対策）。"""
    for g in groups:
        if not _GROUP_NAME_RE.match(g):
            raise ValueError(
                f"不正なグループ名です（英数字・-・_ の1〜64文字のみ許可）: {g!r}"
            )
    return groups


def parse_group_names(raw: str) -> list[str]:
    groups = [g.strip() for g in raw.split(",") if g.strip()]
    return validate_group_names(groups)


# --- ACLグループ（corpus_data.pyから参照） ---
# コードへの直書きを避け、すべて環境変数化する（テナントを差し替えたときに
# 値の変更だけで済むようにするため。docs/guides/new_tenant_setup.html 参照）。
# 未設定時はデモ用の文字列スラッグにフォールバックするが、**実運用では必ず
# Entra ID の実グループの Object ID(GUID) を設定すること**。
#
# ここがスラッグのままだと、OBO 登録済みユーザー（実グループの GUID しか持たない）と
# 文書側の acl_groups（スラッグ）が一致せず、**エラーにならずに検索結果が0件になる**。
QUALITY_TEAM_GROUP_ID = os.environ.get("QUALITY_TEAM_GROUP_ID", "quality-team")
ALL_EMPLOYEES_GROUP_ID = os.environ.get("ALL_EMPLOYEES_GROUP_ID", "all-employees")

CURRENT_USER_GROUPS = parse_group_names(
    os.environ.get("CURRENT_USER_GROUPS", ALL_EMPLOYEES_GROUP_ID)
)

# --- OBO（On-Behalf-Of）設定 ---
# main.pyの/obo/registerルートと、obo.pyのトークン交換で使う。事前にEntra IDへの
# アプリ登録が必要（README参照。agent_search_starterで作成済みのものをそのまま流用できる）。
OBO_TENANT_ID = os.environ.get("OBO_TENANT_ID")
OBO_CLIENT_ID = os.environ.get("OBO_CLIENT_ID")
OBO_CLIENT_SECRET = os.environ.get("OBO_CLIENT_SECRET")
OBO_APP_SCOPE = os.environ.get("OBO_APP_SCOPE") or (
    f"api://{OBO_CLIENT_ID}/.default" if OBO_CLIENT_ID else None
)


def get_search_index_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient
    return SearchIndexClient(SEARCH_ENDPOINT, AzureKeyCredential(SEARCH_API_KEY))


def get_search_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    return SearchClient(SEARCH_ENDPOINT, SEARCH_INDEX_NAME, AzureKeyCredential(SEARCH_API_KEY))


def get_aoai_client():
    from openai import AzureOpenAI
    return AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT,
        api_key=AOAI_API_KEY,
        api_version=AOAI_API_VERSION,
    )


def embed_text(text: str) -> list[float]:
    client = get_aoai_client()
    resp = client.embeddings.create(model=AOAI_EMBEDDING_DEPLOYMENT, input=text)
    return resp.data[0].embedding
