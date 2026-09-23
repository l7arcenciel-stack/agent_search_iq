# 実行記録（新テナントで API / CLI から行った操作）

新テナントの構築で、**何を・どうやって・なぜ**実行したかの時系列の記録。
結果とハマった点も併記する。手順としてまとめ直したものは
[new_tenant_setup.html](../guides/new_tenant_setup.html)、再実行できる形にしたものは
`tenant_setup/` と `fabric_notebooks/` にある。

- 期間: 2026-09-20 〜 2026-09-21
- 実行者: 新テナントの Global Administrator（サブスクリプション Owner）
- **実値は書かない。**`<TENANT_ID>` `<WS_ID>` などに置き換えている。
  現在の値は `python fabric_notebooks/00_check_status.py` で表示できる。
- 実行環境: Windows / Git Bash と PowerShell。**Git Bash 特有の問題が2つ出た**（§0）。

---

## 0. 作業環境の注意（先に読む）

| 問題 | 症状 | 対処 |
|---|---|---|
| Git Bash のパス変換 | `az ... --scope /subscriptions/...` が `MissingSubscription` | `export MSYS_NO_PATHCONV=1` を先に実行 |
| Windows の cmd による `&` の解釈 | Python から `subprocess(..., shell=True)` で `az rest --url "...?a=1&b=2"` を呼ぶと URL が途中で切れる | シェルを介さず HTTP を直接叩く（`tenant_setup/_http.py`） |
| シェル経由の日本語 JSON | `curl -d '{"description":"日本語"}'` が文字化けして `InvalidInput` | UTF-8 のファイルを `--data-binary @file` で渡すか、Python から直接送る |
| セキュリティ既定値 | Graph への `az` 操作が `AADSTS530035` でブロック | `az login --tenant <TENANT_ID> --scope "https://graph.microsoft.com//.default"`（MFA あり）を1回 |
| 容量の停止 | アイテム一覧は取れるのに `/tables` や `getDefinition` だけ 404 | 容量を再開する。**トークンの問題と誤診した**ので注意 |

---

## 1. Azure の土台

### 1-1. テナントの確認
```bash
az account show --query "{tenant:tenantId, sub:name, user:user.name}" -o json
az account list --all -o table     # サブスクリプションが1つだけであることを確認
```

### 1-2. Fabric のリソースプロバイダ登録
`Microsoft.Fabric` が `NotRegistered` だったため登録（数十秒で `Registered`）。
```bash
az provider show -n Microsoft.Fabric --query registrationState -o tsv
az provider register -n Microsoft.Fabric
```

### 1-3. Bicep のデプロイ（Fabric F2 を含む）
前回（09-19）はプロジェクト作成が `identity` 不足で失敗していた（修正済み）。
容量管理者の UPN を**リポジトリに書かないため CLI 引数で渡した**。
```bash
az deployment group what-if -g rg-agent-search-iq -f infra/main.bicep \
  -p infra/main.parameters.json -p deployFabricCapacity=true \
  -p fabricAdminMembers='["<ADMIN_UPN>"]'
az deployment group create -g rg-agent-search-iq -n main-fabric -f infra/main.bicep \
  -p infra/main.parameters.json -p deployFabricCapacity=true \
  -p fabricAdminMembers='["<ADMIN_UPN>"]'
```
- 1回目: `RequestConflict`（アカウント更新とプロジェクト作成が同時に走った一時的競合）。
  リソース自体は作成されていた。**同じコマンドの再実行で `Succeeded`**。
- 結果: F2 容量、Foundry プロジェクト（Bicep 管理）、既存の Search / モデルは変更なし。

### 1-4. 前回の回避策プロジェクトの削除
手動作成の残骸。接続が0件で、ローカル設定からも参照されていないことを確認してから削除。
```bash
az rest --method get    --url "https://management.azure.com/<ACCOUNT_ID>/projects/<OLD_PROJECT>/connections?api-version=2025-04-01-preview"
az rest --method delete --url "https://management.azure.com/<ACCOUNT_ID>/projects/<OLD_PROJECT>?api-version=2025-04-01-preview"
```
> `az resource show` はネストしたリソース種別を解釈できず失敗した。ARM を直接叩く。

### 1-5. 容量の停止・再開、試用容量への付け替え
```bash
az resource invoke-action -g rg-agent-search-iq -n <CAPACITY> \
  --resource-type Microsoft.Fabric/capacities --action suspend   # 再開は resume
```
- ワークスペースの容量割り当てには **Active** が必要（停止中だった容量を再開した）。
- 途中でワークスペースを **試用容量（FTL64）** に付け替え、F2 は停止。
  テーブル一覧・オントロジー定義・DAX クエリは試用容量でも動いた。

---

## 2. Fabric のテナント設定の確認

```bash
TOKEN=$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)
curl -s -H "Authorization: Bearer $TOKEN" https://api.fabric.microsoft.com/v1/admin/tenantsettings
```
- `OntologyPreview` は**既定で True**。
- ポータルの警告に出る依存設定（**ユーザーが Graph を作成できる／データエージェント アイテムの作成・共有**）は、
  **この API に返ってこない**（全172件を確認）。ポータルで確認するしかない。
- `PowerBIMCP`（Power BI MCP サーバーエンドポイント）は False のまま。Toolbox 作成は成功した。

---

## 3. Fabric のアイテム → `tenant_setup/30_fabric_items.py`

### 3-1. ワークスペースの作成と容量への割り当て
```
POST https://api.fabric.microsoft.com/v1/workspaces                      {"displayName": "...", "description": "..."}
POST https://api.fabric.microsoft.com/v1/workspaces/<WS_ID>/assignToCapacity   {"capacityId": "<CAPACITY_GUID>"}
```
- `capacityId` は ARM のリソース ID ではなく、`GET /v1/capacities` の **Fabric 側の GUID**。
- 日本語の説明文をシェル経由で渡したら文字化けで `InvalidInput`（§0）。

### 3-2. レイクハウスの作成
```
POST /v1/workspaces/<WS_ID>/items   {"displayName": "lh_public", "type": "Lakehouse"}
```
- `type` は**ボディ**に入れる。`?itemType=Lakehouse` だと 400。
- OneLake セキュリティは既定で無効（オントロジーのバインドに必要な状態）。
- SQL エンドポイントは作成直後 `InProgress`。数分で `Success`。

### 3-3. オントロジーの作成（プローブ → 本番）
```
POST /v1/workspaces/<WS_ID>/ontologies            {"displayName": "..."}      ← 202 → 完了待ち
POST /v1/workspaces/<WS_ID>/ontologies/<ID>/getDefinition
DELETE /v1/workspaces/<WS_ID>/ontologies/<ID>
```
- **付随の Lakehouse・GraphModel・SQLEndpoint が自動生成される。**削除すると一緒に消える。
- `GET /ontologies` は 200 だが**空配列**。作成済みでも出ない。`GET /items` で探す。
- 最初の POST は 202 のボディが空で、実行スクリプトが例外で止まった。
  作成自体は成功しており、再実行したら `ItemDisplayNameAlreadyInUse`（409）で気づいた。

---

## 4. Delta テーブル → `fabric_notebooks/01_create_delta_tables.py`

Fabric のノートブックで実行（pyspark。手元では動かない）。
- 当初は1つのレイクハウスに全テーブル＋権限判定用の混合テーブルを作った。
- 権限検証（§7）の結果、**レイクハウスを `lh_public` / `lh_restricted` に分割**。
  `TARGET` を変えて2回実行する形に変更。
- 2回目で **`TARGET` を変え忘れ**、`lh_restricted` に公開テーブル9個を書き込んだ。
  `DROP TABLE` で掃除し、アタッチ先と `TARGET` の不一致で止まるガードを追加
  （`spark.conf.get("trident.lakehouse.name")`）。

---

## 5. セマンティックモデル → `fabric_notebooks/02_create_semantic_model.py`

```
POST /v1/workspaces/<WS_ID>/semanticModels   {"displayName": "...", "definition": {"parts": [...]}}
POST /v1/workspaces/<WS_ID>/semanticModels/<ID>/getDefinition
DELETE /v1/workspaces/<WS_ID>/semanticModels/<ID>
```
- TMDL（`definition/*.tmdl`）で投げたら `Workload_FailedToParseFile: Missing required artifact 'model.bim'`。
  **TMSL（`model.bim`）で投げ直して成功。**ところが `getDefinition` は **TMDL で返ってくる**。
- 接続先は `Sql.Database("<SQL エンドポイントのホスト>", "<SQL エンドポイントのID>")`。
  値は `GET /v1/workspaces/<WS_ID>/lakehouses/<LH_ID>` の `sqlEndpointProperties`。
- レイクハウス分割後、旧モデルを削除して `lh_public` 向けに作り直した（品質文書テーブルは含めない）。
- 動作確認（試用容量上）:
  ```
  POST https://api.powerbi.com/v1.0/myorg/datasets/<DATASET_ID>/executeQueries
  {"queries":[{"query":"EVALUATE SUMMARIZECOLUMNS('site'[region_name], \"拠点売上金額\", [拠点売上金額])"}]}
  ```
  トークンのリソースは `https://analysis.windows.net/powerbi/api`。3地域の売上が返った。

---

## 6. オントロジーの定義 → `fabric_notebooks/03_create_ontology_definition.py`

### 6-1. スキーマの特定
ポータルでエンティティ `product` を1つ作ってバインドし、`getDefinition` で読んだ。
各ファイルの `$schema` に公開スキーマの URL があり、そこから取得できた。
```bash
curl -sL https://developer.microsoft.com/json-schemas/fabric/item/ontology/entityType/1.0.0/schema.json
curl -sL https://developer.microsoft.com/json-schemas/fabric/item/ontology/dataBinding/1.0.0/schema.json
curl -sL https://developer.microsoft.com/json-schemas/fabric/item/ontology/relationshipType/1.0.0/schema.json
curl -sL https://developer.microsoft.com/json-schemas/fabric/item/ontology/contextualization/1.0.0/schema.json
```
- `dataBinding` は URL を推測して当てた（`binding` などは 404）。
- `valueType` は **String / Boolean / DateTime / Object / BigInt / Double のみ。Decimal が無い。**
- リレーションのバインドは `DataBindings` ではなく **`Contextualizations`**（リレーションを1本ポータルで作って判明）。
- ポータルで概要ページを開くと `EntityTypes/<id>/Overviews/definition.json` が増える。
  `endswith("/definition.json")` で拾うと誤読する（点検スクリプトがこれで壊れた）。

### 6-2. 定義の流し込み
```
POST /v1/workspaces/<WS_ID>/ontologies/<ONT_ID>/updateDefinition   {"definition": {"parts": [...]}}
```
- 1回目: `ALMOperationImportFailed: Duplicate Name-Namespace combinations`。
  ポータルで作ったエンティティと同名・別 ID を送ったため。
  **`getDefinition` で既存の名前→ID を読み、引き継ぐ**ように直して成功。実行前に定義をバックアップした。
- バインド保存時に取り込みが走り、手動リフレッシュ不要だった。
  オントロジー本体にはリフレッシュのメニューもジョブ（`jobType=...` はすべて `InvalidJobType`）も無かったが、
  **後で、付随の GraphModel 側に `jobType: Refresh` のジョブがあり、定義の保存ごとに自動で走っていた**ことが分かった（§16）。
- API で追加したエンティティは、ポータルを **Ctrl+Shift+R** するまで表示されなかった。
- レイクハウス分割後に再実行し、バインド先を `lh_public` / `lh_restricted` に向け直した。

---

## 7. 権限の検証（デモの核心）

営業 次郎（品質チーム未所属）でポータルから確認した。

| 付与した権限 | 結果 |
|---|---|
| オントロジーへの読み取りのみ | スキーマは見えるが、**全エンティティのデータが 401**（`Ontology_Query / query_entity_instance`） |
| ＋ 1つのレイクハウスへの読み取り | **そのレイクハウス上の全エンティティが見えた（限定文書も漏れた）** |
| レイクハウスを分けて、`lh_restricted` は品質チームのみ | **`product` は見える、`quality_document_restricted` は 401**。品質 太郎は両方見える |

- レイクハウスの共有では「すべての SQL エンドポイント データを読み取る」「すべての Apache Spark を読み取り…」にチェックが必要。
- アイテム単位の権限を付ける API は見つからなかった（`/items/<ID>/permissions` などは 404）。
  API で触れるのは `GET /v1/workspaces/<WS_ID>/roleAssignments`（ワークスペース単位）だけで、
  それを使うと全アイテムが見えてしまうので使わない。
- 検証後、漏れた状態の旧レイクハウスを削除した。

---

## 8. Entra ID → `tenant_setup/10_entra_groups_users.py`, `20_entra_app_registration.py`

### 8-1. グループ・ユーザー・ライセンス
```
POST /v1.0/groups          {"displayName":"grp-all-employees","securityEnabled":true,"mailEnabled":false,...}
POST /v1.0/users           {"userPrincipalName":"demo.quality@<DOMAIN>","passwordProfile":{...},...}
POST /v1.0/groups/<GID>/members/$ref
PATCH /v1.0/users/<UID>    {"usageLocation":"JP"}
POST /v1.0/users/<UID>/assignLicense
```
- 既定の「All Company」は M365 グループなので使わなかった。
- `usageLocation` 設定直後のライセンス割り当ては `invalid usage location` で失敗 → 15秒おきの再試行で成功。
- `POWER_BI_STANDARD`（Power BI 無料版）で、Fabric へのサインインとオントロジー閲覧ができた。
- パスワードは画面に出さずファイルに保存した。

### 8-2. 委任スコープの特定
```
GET /v1.0/servicePrincipals?$filter=appId eq '00000003-0000-0000-c000-000000000000'   Microsoft Graph
GET /v1.0/servicePrincipals?$filter=appId eq '00000009-0000-0000-c000-000000000000'   Power BI Service
GET /v1.0/servicePrincipals?$filter=servicePrincipalNames/any(x:x eq 'https://ai.azure.com')
```
- `https://ai.azure.com` の実体は **Azure Machine Learning Services**（appId `18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`）の `user_impersonation`。

### 8-3. アプリ登録と管理者同意
```
POST /v1.0/applications              isFallbackPublicClient=true、access_as_user を公開、requiredResourceAccess
PATCH /v1.0/applications/<ID>        {"identifierUris":["api://<APP_ID>"]}
POST /v1.0/servicePrincipals         {"appId":"<APP_ID>"}
POST /v1.0/oauth2PermissionGrants    {"consentType":"AllPrincipals","clientId":...,"resourceId":...,"scope":"..."}
POST /v1.0/applications/<ID>/addPassword
```
- 管理者同意は `oauth2PermissionGrants`（テナント全体）で付与。自アプリの `access_as_user` にも付与した。
- Fabric IQ 接続用のアプリ（②）も作ったが、**不要だった**（§10）。動作確認後に削除する予定。

---

## 9. Foundry のロール → `tenant_setup/40_foundry_rbac.py`

```bash
export MSYS_NO_PATHCONV=1
az role definition list --query "[?contains(roleName,'Foundry')].roleName" -o tsv
az role assignment create --assignee-object-id <ALL_EMPLOYEES_GROUP_ID> --assignee-principal-type Group \
  --role "Foundry Agent Consumer" --scope <PROJECT_ID>
az role assignment create --assignee-object-id <MY_OBJECT_ID> --assignee-principal-type User \
  --role "Foundry Project Manager" --scope <PROJECT_ID>
```
- ロール名が「Foundry User / Foundry Project Manager / Foundry Agent Consumer」などに変わっていた。
- Foundry Agent Consumer の中身は `Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action` のみ。
- サブスクリプション Owner だけでは Toolbox を作れない（dataActions を含まない）ので、自分にも付与。

---

## 10. Fabric IQ のツールと Toolbox

### 10-1. 接続種別の特定（SDK のソースから）
手元に `azure-ai-projects` が無かったので `.venv` を作って `requirements.txt` を導入し、SDK を読んだ。
```bash
python -m venv .venv && ./.venv/Scripts/python -m pip install -r requirements.txt
```
- `ConnectionType` に `RemoteTool_Preview`、ツール種別に `fabric_iq_preview` と `fabric_dataagent_preview` がある。
- ポータルの接続一覧の「Microsoft Fabric（プレビュー）」は**データエージェント用**で、Fabric IQ には使えない。

### 10-2. ツールの作成（ポータル）と中身の確認
ポータルの **ビルド > ツール** で Fabric IQ を作成（認証は既定の「OAuth ID パススルー」）。
```bash
az rest --method get --url "https://management.azure.com/<PROJECT_ID>/connections/<NAME>?api-version=2025-04-01-preview"
```
- `category: RemoteTool` / `authType: UserEntraToken` / `audience: https://api.fabric.microsoft.com` /
  `metadata.type: fabric_iq_preview`。**本人のトークンをそのまま渡す方式で、独自アプリ不要。**
- `isSharedToAll: false`。作成者以外が使えるかは未確認。

### 10-3. Toolbox の作成
```bash
AI_FOUNDRY_PROJECT_ENDPOINT=https://<ACCOUNT>.services.ai.azure.com/api/projects/<PROJECT> \
FABRIC_IQ_PROJECT_CONNECTION_ID=<PROJECT_ID>/connections/<NAME> \
FABRIC_IQ_ONTOLOGY_WORKSPACE_ID=<WS_ID> FABRIC_IQ_ONTOLOGY_ITEM_ID=<ONT_ID> \
./.venv/Scripts/python setup_toolbox.py
```
- `common.py` が import 時に AI Search / Azure OpenAI の変数も要求するので、ダミー値で通した。
- 接続 ID は ARM のフルパスで渡した。

---

## 11. スクリプトの検証

`tenant_setup/` の4本を作成後、既存リソースに対して `--dry-run` と本実行を行い、**何も変更しない**ことを確認。
`20`（アプリ登録）と `30`（Fabric アイテム）は使い捨ての名前で**新規作成まで実行**し、確認後に削除した。

---

## 12. AI Search のインデックス作成と文書投入 → `ingest_sample_docs.py`

リポジトリに投入スクリプトが無かったため新規に作成（`corpus_data.py` のコメントで参照されていた名前）。
鍵は画面に出さず、`az` の出力を環境変数に直接入れて実行した。
```bash
export AZURE_SEARCH_API_KEY=$(az search admin-key show --service-name <SEARCH> -g <RG> --query primaryKey -o tsv)
export AZURE_OPENAI_API_KEY=$(az cognitiveservices account keys list --name <ACCOUNT> -g <RG> --query key1 -o tsv)
export QUALITY_TEAM_GROUP_ID=<GUID> ALL_EMPLOYEES_GROUP_ID=<GUID> ...
./.venv/Scripts/python ingest_sample_docs.py --dry-run
./.venv/Scripts/python ingest_sample_docs.py
```
- 20文書（10製品×品質報告書・仕様書）。A001〜A006 は全社公開、A007〜A010 は品質チーム限定。
- **グループIDが GUID でなければ投入を拒否する**ガードを入れた（スラッグのまま投入すると誰にも見えなくなるため）。
- 確認（`search_tool.search_documents` を直接呼び出し）:

| 検索 | 結果 |
|---|---|
| 品質チーム所属で A008（限定） | 2件 |
| 未所属で A008（限定） | **0件** |
| 未所属で A001（公開） | 1件 |

---

## 13. azd の導入と環境作成

この PC には `azd` が入っていなかった。
```powershell
winget install --id Microsoft.Azd -e --accept-source-agreements --accept-package-agreements --silent
azd config set auth.useAzCliAuth true          # azd login の代わりに az login の資格情報を使う
azd extension install azure.ai.agents           # host: azure.ai.agent に必要
azd env new <ENV_NAME> --subscription <SUB_ID> --location japaneast --no-prompt
azd env set <KEY> <VALUE>                       # §6 の対応表どおりに全部
```
- winget で入れた直後は、開いているシェルの PATH に反映されない。新しいシェルを開くか PATH を読み直す。
  Git Bash からは見えなかったので PowerShell で操作した。
- シークレット（Search キー、AOAI キー、OBO シークレット）は `az` の出力と保存済みファイルから
  直接 `azd env set` に渡し、画面には出さなかった。azd の環境は `.azure/<ENV_NAME>/.env` に保存される
  （`.gitignore` 済み。ただし OneDrive 配下なので同期はされる点に注意）。
- リポジトリ直下に置かれていた `AzureCLI.msi` がデプロイパッケージに含まれてしまうため、
  `.agentignore` に `*.msi` を追加した（あわせて `docs/`、`infra/`、`ingest_sample_docs.py` も除外）。

---

## 14. デプロイ（`azd deploy`）

```powershell
azd env list
azd deploy --no-prompt
```
- 1回目: `Microsoft Foundry project ID is required: AZURE_AI_PROJECT_ID is not set`
- 2回目: `Foundry dependencies are not ready: foundryproject (azure.ai.project): FOUNDRY_PROJECT_ENDPOINT is not set`
  （`azure.yaml` の `endpoint: ${AI_FOUNDRY_PROJECT_ENDPOINT}` とは別に、この名前で要求される）
- 3回目: 成功（2分41秒）。コードパッケージからエージェントを作成 → 起動待ちのポーリング（12回）→ 環境変数の登録。
  出力に Responses / Invocations のエンドポイントとポータルのプレイグラウンド URL が出る。
```powershell
azd env set AZURE_AI_PROJECT_ID      "/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<ACCOUNT>/projects/<PROJECT>"
azd env set FOUNDRY_PROJECT_ENDPOINT "https://<ACCOUNT>.services.ai.azure.com/api/projects/<PROJECT>"
```

### 動作確認（運用者の資格情報で直接 POST）
```
POST https://<ACCOUNT>.services.ai.azure.com/api/projects/<PROJECT>/agents/agent-search-iq/endpoint/protocols/openai/responses?api-version=v1
Authorization: Bearer <scope https://ai.azure.com/.default のトークン>
{"input": "製品A001の品質基準について教えて", "stream": false}
```
- `search_documents_tool` が呼ばれ、A001 の品質基準で回答（約24秒）。
- **`function_call_output` は Responses の `output` に含まれる**（§8 #9 が解決）。

---

## 15. レート制限（チャットモデルの容量不足）

Fabric IQ を使う質問で `Model deployment rate limit exceeded`。Bicep の既定で `gpt-4.1-mini` の容量が **1（1,000 TPM）**だった。
Fabric IQ のツール定義がプロンプトに乗るぶん、すぐ上限に当たる。
```bash
az cognitiveservices account deployment create --name <ACCOUNT> -g <RG> --deployment-name gpt-4.1-mini \
  --model-name gpt-4.1-mini --model-version 2025-04-14 --model-format OpenAI --sku-name Standard --sku-capacity 50
```
- `az cognitiveservices usage list` には Standard の枠が表示されなかったが、50 はそのまま通った。
- Standard は従量課金なので、容量を上げても固定費は増えない。
- 次の Bicep 実行で 1 に戻らないよう `infra/main.parameters.json` の `chatModelCapacity` も 50 にした。

---

## 16. Fabric IQ の自然文検索が失敗する（調査中）

エージェント経由では `fabric_iq_ontology___search_ontology` が `Error: Function failed.` を返した。
ログ（`azd ai agent monitor --session-id ...`）には `ToolExecutionException` までしか出ないため、
**Toolbox の MCP エンドポイントを直接呼んで**生のエラーを見た。
```
POST https://<ACCOUNT>.services.ai.azure.com/api/projects/<PROJECT>/toolboxes/fabric-iq-toolbox/versions/1/mcp?api-version=v1
Accept: application/json, text/event-stream
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fabric_iq_ontology___search_ontology",
  "arguments":{"naturalLanguageQuery":"製品A005の商品群名称","naturalLanguageResponse":true}}}
```
| 確認 | 結果 |
|---|---|
| 本人トークンで届くか | 届く（`UserEntraToken` のパススルーは機能） |
| ツール名 | `fabric_iq_ontology___list_ontology_entity_types` / `fabric_iq_ontology___search_ontology`。**接頭辞なしの名前では `No tool config matches tool name`** |
| エンティティ一覧 | 成功（ただし `properties` は空で返る。一覧の返し方の都合で、スキーマ自体は正常） |
| 自然文検索 | **`Failed to translate NL query to ontology query.`**（約2秒で失敗） |
| 容量を F2 に戻す | 同じエラー → 容量の種類は原因ではない |
| GraphModel の定義（`POST /items/<ID>/getDefinition`） | ノード型・プロパティ・データソース（`lh_public` / `lh_restricted`）・列の対応すべて正常 |

- 残る候補はテナント設定「**Azure OpenAI に送信されたデータは、容量の地理的リージョン…の外部で処理できます**」（無効だった）。
  設定の説明に「容量の地域が、Fabric のために Azure OpenAI を使える地域の外部にある場合のみ適用」とあり、Japan East の容量が該当する見込み。
- 有効化して反映を確認（admin API で `AllowSendAOAIDataToOtherRegions: True`）したが、**F2 に戻しても同じエラー**。
  英語で聞いても同じ。GraphModel のジョブ履歴（`GET /items/<graphModelId>/jobs/instances`）を見ると
  `jobType: Refresh` が定義保存のたびに自動で走っており、最新は `Completed`。**グラフの取り込みも原因ではない**。
- 「データエージェント アイテムの種類を作成および共有できる」設定を疑ったが、**テナント設定に存在しなかった**（Copilot の設定に統合された模様）。
- Microsoft Learn を確認したところ、オントロジーの必須設定は**データエージェントの必須設定**を参照しており、
  容量が米国・EU 以外なら「外部で**処理**」と「外部に**格納**」の**両方が必須**と明記されていた
  （[data-agent-tenant-settings](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-tenant-settings)）。
  「格納」はエージェントが会話履歴を最長28日保存するため。反映には**最大1時間**。
  → 当初「格納は無効のままで試す」と判断したのは誤りだった。
- **2026-09-21 に「格納」も有効化。**直後は試用容量・F2 とも同じエラーだったが、**20〜30分後に F2 で通るようになった**。
  反映後は、失敗する質問でも所要時間が約2秒 → 約10秒に伸びた（AI まで届いたうえで失敗している）。

### 16-1. 解決：質問の書き方

反映後も「製品A005の商品群名称」は失敗した。質問の書き方を変えて比べた（F2 上、MCP へ直接 `tools/call`）。

| naturalLanguageQuery | 結果 |
|---|---|
| 製品A005の商品群名称 | 失敗（約10秒） |
| Which product_group does the product with product_id A005 belong to? | **成功**：A005 熱交換器β → PG02 金属部品 |
| product_id が A005 の product が belongs_to_product_group でつながる product_group の name | **成功**（日本語でもスキーマ名を混ぜれば通る） |
| List all product_group entities | **成功**：4件 |
| List all product entities | **成功**（約44秒） |

- スキーマが英語の snake_case で、説明や同義語が無いため、日本語の業務用語を対応付けられない。
- エージェントが最初に Fabric IQ へ渡していた質問も「製品ID A005の商品群名称を教えてください」で、まさに失敗パターンだった。
- 対処: `main.py` の `AGENT_INSTRUCTIONS` に「search_ontology には英語でスキーマの名前を使って問い合わせる」
  指示とスキーマの一覧・例文を追加して再デプロイ。

### 16-2. 再デプロイ後の確認と、試用容量の制約

- 再デプロイ後（試用容量上）、エージェントに日本語で「製品A005はどの商品群に属し、どの拠点で生産されていますか」と聞くと、
  エージェントは **「Which product_group does the product with product_id A005 belong to? Return the product_group name.」**
  と英語・スキーマ名の質問を組み立てた（指示の変更は効いた）。
- しかし結果は `Error: Function failed.`。**F2 で成功したのと同じ質問を試用容量で MCP に直接投げると、約2秒で
  `Failed to translate NL query to ontology query.`**。
- 結論: **試用容量では Fabric IQ の自然文検索（AI）は動かない**。構築・閲覧・DAX・エンティティ一覧は動く。
  Fabric IQ を使う確認とデモ本番は F2 に付け替えて行う。

### 16-3. F2 上でのエージェント経由の確認と、指示の追加調整

「製品A005はどの商品群に属し、どの拠点で生産されていますか」をエージェントに投げた。

| 状態 | 起きたこと |
|---|---|
| 指示1回目 | 商品群の取得は**成功**していたのに「閲覧権限が無い可能性」と回答。拠点は「Which site produces …」で失敗 |
| 原因1 | 結果に `naturalLanguageResponseError: "Service request failed. Status: 403 (Forbidden)"` が付いていた。**要約文の生成だけの失敗**（データは `raw` にある。成功する回もあり不安定）。エージェントが「403」を権限不足と読んだ |
| 原因2 | 「produces」では変換に失敗。「via produced_at_site」とリレーション名を書くと成功 |
| 指示2回目 | 拠点は成功したが、今度は商品群が `Function failed`、しかも**製品名「熱交換器β」を商品群名として回答**（推測で埋めた） |
| 原因3 | どちらの回も、エージェントが **search_ontology を2回並列に呼び、片方が失敗**していた。MCP に直接1回ずつ投げると、どの書き方も3回中3回成功した |
| 指示3回目 | 「並列に呼ばず1回にまとめる」「naturalLanguageResponse は false」「結果の読み方」「推測で埋めない」を追加 |
| 結果 | **3回中3回正答**。呼び出しは各1回、失敗0。所要27〜34秒 |
- 「Microsoft サブプロセッサーとしての OpenAI」の2設定は必須一覧に無いため、有効化していない。
- 元テナントでは**国外処理と国外保存**の両方が要るため、データ所在地の社内承認が必要になる見込み（元テナント手順書の依頼 #8）。

---

## 17. デモ UI の起動とサインイン方式の変更

```powershell
.\.venv\Scripts\python -m pip install -r requirements-demo.txt
azd env get-values > .env        # ※ BOM なしで書き直した（下記）
.\.venv\Scripts\streamlit run demo_chat_iq.py --server.address localhost --server.port 8501 --server.headless true
```
- PowerShell 5.1 の `Set-Content -Encoding utf8` は BOM を付け、`.env` の1行目の変数名が壊れる → BOM なしで書き直した。
- Streamlit は既定で全インターフェースで待ち受ける（LAN から開ける）。トークンを扱うので `--server.address localhost` にした。
- サインインでのエラー：
  1. VS Code 内蔵ブラウザでデバイスコードのページを開くと `AADSTS900561: The endpoint only accepts POST requests`
  2. 通常のブラウザでも、パスワード＋MFA の後に **`AADSTS530035`（セキュリティ既定値によるブロック）**。MFA 登録は済んでいた
- 対処：デモ UI のサインインを**ブラウザ方式（MSAL `acquire_token_interactive`、認可コード＋PKCE）**に変更（既定。`DEMO_SIGNIN=device` で旧方式）。
  アプリ登録にパブリッククライアントのリダイレクト URI `http://localhost` を追加（`tenant_setup/20` を更新して適用）。

---

## 18. 一般ユーザーだと Fabric IQ の MCP が 403 になる（原因確定：ワークスペースのロールなし）

デモ UI で品質 太郎・営業 次郎としてサインインすると、どの質問でも回答が空になった。管理者では正答（A005 → 金属部品、ロッテルダム工場）。

- デモ UI を変更し、テキストを抽出できないときは `status` / `error` を表示し、応答全体を一時フォルダの `demo_chat_iq_last_response.json` に保存するようにした
- 応答は `status: failed`、`server_error`：`Failed to enter context manager` … `tools/list failed … fabric_iq_ontology … HTTP_403 Access denied`。
  エージェントは**最初にツール一覧を取得**し、そこで失敗すると **Fabric IQ を使わない質問も含めて応答全体が失敗**する
- 切り分け：営業 次郎の資格情報（`AZURE_CONFIG_DIR` を分けて `az login`）で直接確認した

| 確認 | 管理者 | 営業 次郎 |
|---|---|---|
| ワークスペースのアイテム一覧 | 200 | **401** |
| オントロジー MCP `initialize` / `tools/list`（直接） | 200 | **403** `InsufficientPrivileges` |
| エージェント（Responses） | 正答 | failed（HTTP_403） |

- → **デモ UI のサインインではなく、Fabric が本人を拒否している。**
- ワークスペースのロールは管理者のみ。2人はオントロジーのアイテム共有だけで、付随の GraphModel・Lakehouse（`ont_…_graph_…` / `ont_…_lh_…`）には権限が無い
- 公式ドキュメント上の利用者の条件：オントロジーとデータソースの読み取り、Fabric ライセンス、Foundry プロジェクトの **Foundry User** ロール（2人は Foundry Agent Consumer）

### 18-1. ワークスペースの閲覧者を付与 → Fabric IQ は解消

営業 次郎・品質 太郎をワークスペースの**閲覧者**に追加した。次郎の資格情報で直接確認した結果：

| 確認 | 結果 |
|---|---|
| オントロジー MCP `initialize` / `tools/list` | **200**（403 が解消） |
| `search_ontology`：`quality_document_public` | 取得できる |
| `search_ontology`：`quality_document_restricted` | 拒否（`… you don't have access to any matching node types due to security configuration enforcement`） |
| `lh_public` の OneLake 一覧 | 200 |
| `lh_restricted` の OneLake 一覧 | **403**（閲覧者には ReadAll が付かない） |
| セマンティックモデル `executeQueries` | **404** `PowerBIEntityNotFound` |

- → **原因はワークスペースのロールが無かったこと。**閲覧者でも `lh_restricted` は見えないため、閲覧者のままで運用する
  （当初心配した「閲覧者にすると限定データまで見える」は起きなかった）

### 18-2. デモ UI での2人比較（1回目：閲覧者のみの状態）

| 質問 | 品質 太郎（所属） | 営業 次郎（未所属） | 期待どおりか |
|---|---|---|---|
| 製品A008の品質基準について教えて | 内容が返る | 「閲覧可能な情報からは回答できません」 | ✅ |
| 地域別の売上金額を教えて | 「閲覧権限がない」 | 「閲覧権限がない」 | ❌（2人とも数値が返るはず） |
| A008の品質情報と、関連する商品群の売上をまとめて | 品質は返る・売上なし | 商品群（樹脂部品）は Fabric IQ から返る・品質は欠落を明示・売上なし | 売上以外は ✅ |

- 売上：モデルは `lh_public` 接続で RLS なし＝全員に見せる設計。**閲覧者にはモデルのビルド権限が付かず、`executeQueries` が拒否**されている
- 対処：sm_agent_search_iq の「権限の管理」で2人に**ビルド**を付与（ポータル作業。API なら `POST /groups/{ws}/datasets/{id}/users`、`datasetUserAccessRight: ReadExplore`）
  - ポータルの既定では書き込み・再共有も付いた → 読み取り＋ビルドだけに絞る
- 結果：次郎の資格情報で `query_fabric`（拠点売上金額 × 地域名称）が **3行返った**。**原因はビルド権限なしで確定**。Power BI 無料版のままで通った
- 太郎の3問目で Fabric IQ が呼ばれなかったのは、商品群を品質文書から読み取れたためと思われる【要確認】

### 18-3. 3問目の売上が2人とも失敗する

ビルド付与後、1・2問目は期待どおりになったが、3問目の売上は2人とも失敗した。

- 次郎として Responses を直接呼び、呼び出し内容を確認：**Fabric IQ を呼ばず**、`query_fabric_tool` を `商品群売上金額` × `filters: 製品ID = A008` で呼び、`Error: Function failed.`
- 同じ引数で `query_fabric()` を直接実行すると**成功**。ただし製品IDの絞り込みが効かず、**全4商品群の行**が返った（product → product_group は多対一で、絞り込みが伝わらない）
- 対処1：`fabric_schema.py` の列から製品ID・製品名称を外し、指示とツール説明に「Fabric IQ で商品群コード → そのコードで照会」の手順を追加。想定外の例外も中身を返すようにした
  → 再デプロイ後、太郎は Fabric IQ → 売上の順に呼ぶようになったが、`商品群コード = PG01` で絞った照会も `Function failed.`
- デモ UI の「処理詳細を見る」に引数と結果を表示するようにして確認：**filters を付けた呼び出しだけが毎回失敗**（絞り込みなしの地域別売上は成功）
- ローカルで `query_fabric_tool.invoke()` を呼んで再現：`AttributeError: 'dict' object has no attribute 'model_dump'`。
  **agent_framework は `list[FabricFilter]` の要素を dict のまま渡す。**`model_dump()` は try の外にあったため、中身が返らず `Function failed.` だった
- 対処2：dict でも BaseModel でも受けるように修正して再デプロイ

### 18-4. 最終結果（2026-09-22）

| 質問 | 品質 太郎（所属） | 営業 次郎（未所属） | 判定 |
|---|---|---|---|
| 製品A008の品質基準について教えて | 内容が返る | 「閲覧可能な情報からは回答できません」 | ✅ |
| 地域別の売上金額を教えて | 日本 11.7億／アジア 9.0億／欧州 6.8億 | 同じ | ✅ |
| A008の品質情報と、関連する商品群の売上をまとめて | 品質（仕様・品質基準）＋樹脂部品 502,320,000円 | 品質の欠落を明示＋樹脂部品 502,320,000円 | ✅ |

- 3問目は2人とも Fabric IQ で A008 → PG01（樹脂部品）を取得 → `商品群コード = PG01` で売上を照会（1行）
- 最終的な権限：2人とも**ワークスペース閲覧者＋セマンティックモデルのビルド**、Foundry は **Foundry Agent Consumer** のまま。
  Fabric ライセンスは Power BI 無料版のまま

---

## 19. 容量を試用に戻し、フォルダ構成を整理（2026-09-22）

- デモ確認が終わったので **F2 を停止し、ワークスペースを試用容量に戻した**。試用容量では Fabric IQ の自然文検索が動かないため、
  デモ UI の3問目（Fabric IQ を使う部分）は失敗する。Fabric IQ を確認・デモするときだけ F2 に戻す
- リポジトリ直下に .py が14本並んでいたのを、役割ごとに分けた（`git mv` で履歴は保持）

| 旧 | 新 |
|---|---|
| `main.py` `obo.py` `common.py` `corpus_data.py` `search_tool.py` `query_fabric.py` `fabric_client.py` `fabric_schema.py` `requirements.txt` `.agentignore` | `agent/` |
| `demo_chat_iq.py` / `requirements-demo.txt` | `demo_ui/demo_chat_iq.py` / `demo_ui/requirements.txt` |
| `setup_toolbox.py` `ingest_sample_docs.py` | `scripts/setup/` |
| `client_register_obo.py` `client_test_responses_session.py` | `scripts/dev/` |
| `docs/new_tenant_setup.html` `docs/apply_to_original_tenant.html` | `docs/guides/` |
| `docs/knowledge.md` `docs/operations_log.md` `docs/tenant_config_record.md` | `docs/records/` |
| `docs/azure-resources-report.html` `docs/deployment-status-report.html` | `docs/reports/` |

- `azure.yaml` の `project` を `.` → `agent` に変更。ビルドに送られるのは `agent/` だけになった
- `demo_ui/` と `scripts/` は `sys.path` に `agent/` を足して `common` などを import する。`.env` はリポジトリ直下のまま（`agent/common.py` の `load_dotenv()` が上位のフォルダを探して見つける）
- 確認：全ファイルのコンパイル、`agent/main.py` の import と絞り込み付きツール呼び出し、各スクリプトの import、デモ UI の起動（Streamlit AppTest でサインイン画面まで）
- **`azd deploy`（`project: agent`）後、F2 に載せてデモ UI で3問目を2人で確認 → 期待どおり**（§18-4 と同じ結果。Fabric IQ で A008 → PG01、売上 502,320,000 円、次郎は品質文書の欠落を明示）
- この記録（§1〜§18）に出てくるパスは当時のもの（書き換えていない）

---

## 20. ワークスペース閲覧者だと限定文書が漏れる（2026-09-22）

§18-1 で「閲覧者でも分離は崩れない」としたのは、MCP と OneLake だけで確かめた結果だった。営業 次郎でポータルを確認した。

| 確認（営業 次郎） | 結果 |
|---|---|
| オントロジーの `quality_document_restricted` → インスタンス | **2件表示（QD-A007 / QD-A008）** ❌ |
| `lh_restricted` のレイクハウス画面 | 「このアイテムへのアクセスが制限されています」 |
| `lh_restricted` の SQL 分析エンドポイントで `SELECT * FROM quality_document_restricted` | **2件返る** ❌ |
| 自動生成の `ont_agent_search_iq_lh_…` | テーブル無し（経路ではない） |

- `lh_restricted` の「アクセス許可の管理」：次郎・太郎は**ワークスペース ビューアー**で「読み取り, ViewOutput」、
  `grp-quality-team` は「読み取り, ReadAll」。**ReadAll が無くても、閲覧者の「読み取り」で SQL エンドポイントは読める**
- エージェント（Fabric IQ の MCP）経由では次郎は拒否されたまま（§18-4 の結果は正しい）。漏れているのは**直接アクセス**
- 対策の候補：ワークスペースの閲覧者をやめる／`lh_restricted` を別ワークスペースに移す／SQL エンドポイントで DENY

### 20-1. 限定データを別ワークスペースに移す（案A、実施中）

- 新ワークスペース `ws-agent-search-iq-restricted`（試用容量）を API で作成し、**`grp-quality-team` だけを閲覧者**にした
- そこにレイクハウス `lh_restricted` を作り、`01_create_delta_tables.py`（`TARGET = "restricted"`）をノートブックとして API で作成・実行 → 2テーブル作成、検証セルも通過
- `03_create_ontology_definition.py` に `FABRIC_LAKEHOUSE_RESTRICTED_WORKSPACE_ID` を追加し、限定側だけ新ワークスペースにバインド → **API は受け付けた**。
  GraphModel の `dataSources.json` も新ワークスペースのパスになり、`Refresh` は Completed（約3分）
- 管理者で GraphModel に GQL（`POST /graphModels/<id>/executeQuery?preview=true`）→ `quality_document_restricted` の2件（QD-A007 / QD-A008）が取れる。
  **別ワークスペースのレイクハウスへのバインドは動く**【確認済】
- 別件：限定側のエッジ `restricted_document_describes_product` は**管理者でも** `does not match any edge type … security configuration enforcement` で拒否される。
  バインドを元の `lh_restricted` に戻しても同じだったので、移したことが原因ではない。公開側のエッジ（`public_document_describes_product`）は3件取れる。
  限定文書（`lh_restricted`）と製品（`lh_public`）という**別ソースのノードを結ぶエッジだけが拒否される**可能性【要確認】
- ポータル（試用容量）で `quality_document_restricted` のインスタンス：**太郎 2件／次郎 `Unauthorized`（Authentication is required.）**。
  次郎の左メニューに新ワークスペースは出ない → **オントロジー画面での分離は回復**【確認済】
- 旧 `lh_restricted`（元ワークスペース、SQL エンドポイントごと）を API で削除。削除後も管理者の GQL で限定文書2件が取れる
- `00_check_status.py`（限定用ワークスペースと閲覧者ロールを点検、元ワークスペースに `lh_restricted` があれば警告）と
  `30_fabric_items.py`（限定用ワークスペース・閲覧者ロール・`lh_restricted` の作成）を新構成に合わせた。手順書・knowledge も更新
- F2 起動後、**両ワークスペースを F2 に割り当て**（試用容量に残すと、試用の期限切れで限定データが読めなくなるため）
- 管理者で Fabric の MCP（`ontologyEndpoint`）に直接 `search_ontology`：
  限定文書（A008 → QD-A008）も、A008 → PG01 樹脂部品も取れた（後者は1回目だけ `-32603 internal error`、再試行で成功・約34秒）
- F2 でデモ UI の3問を2人で実行 → **§18-4 と同じ結果**（1問目：太郎は内容、次郎は「閲覧可能な文書なし」／2問目：同じ数字／
  3問目：2人とも Fabric IQ で A008 → PG01 → 502,320,000円、次郎は品質の欠落を明示）
- ただし3問で**品質文書を返しているのは AI Search だけ**。Fabric IQ への問い合わせは2人とも商品群だけで、限定文書エンティティを引いていない。
  つまりデモ3問の「見え方の差」は AI Search の ACL によるもので、オントロジーの分離はこの3問では使われていない
- 2問目で太郎の回答に「他の地域のデータは閲覧可能な情報には含まれていませんでした」という根拠のない一文が付いた（次郎には無し）
- 残り：太郎が MCP 経由で限定文書を引けるか（閲覧者には ReadAll が付かない）を、限定文書を直接聞く質問で確認

### 20-2. 限定文書 → 製品のリレーションが使えない（別レイクハウスをまたぐエッジ）

- デモ UI に4問目「A008の品質文書を、オントロジー（Fabric IQ）から調べて」を追加 → **太郎・次郎とも `Error: Function failed.`**
- 管理者で MCP を直接呼んでも、品質文書に関する問い合わせは `(:quality_document_restricted)-[:restricted_document_describes_product]->(:%) does not match any edge type … security configuration enforcement` で失敗。
  **`product_id` プロパティで絞るよう書いても、リレーションが定義されているとそれを使った問い合わせに変換された**
- GQL でエッジ型ごとの件数を見ると `belongs_to_product_group` 5 / `produced_at_site` 5 / `public_document_describes_product` 3 だけで、
  **`restricted_document_describes_product` はグラフに存在しない**（取り込まれていない）。`Refresh` は Completed でエラーも無い
- 公開側のエッジは文書・エッジ・製品がすべて `lh_public`。限定側は文書・エッジが `lh_restricted`、製品が `lh_public`。
  テーブルの形は同じ（`edge_id` / `document_id` / `product_id`）なので、**別のレイクハウスのノードを結ぶエッジが取り込まれない**と判断【推測：仕様か不具合かは未確認】。
  元ワークスペースにあった頃から同じ
- 対処：`03_create_ontology_definition.py` から `restricted_document_describes_product` を外し（エッジテーブルは残す）、
  `agent/main.py` の指示を「限定文書は product_id プロパティで絞る。公開・限定は1回ずつ順に聞く」に変更 → オントロジー更新（Refresh 約3分）・`azd deploy`（version 7）
- 変更後、管理者で MCP を直接：限定文書だけ → QD-A008 が取れる（約32秒）／A008 → PG01 も取れる／
  **公開と限定を1つの質問にまとめると、エラーなしで空**（A008 は公開文書が無いので結合が空になったとみられる）→ 指示で「まとめない」とした

### 20-3. 太郎が限定文書を引けない（閲覧者だけでは ReadAll が無い）

- 4問目の文言を「A008の限定の品質文書（quality_document_restricted）を、文書検索は使わずFabric IQだけで調べて」に変更（「オントロジーから」だけだと AI Search で済ませた）
- 太郎・次郎とも `Function failed.`。管理者は Toolbox 経由でも約7秒で取れる → **太郎の権限の問題**
- **太郎がエージェント経由で限定文書を引けたことは、移動前も含めて一度も無かった**（§18-4 の品質情報は AI Search 由来）
- 太郎の資格情報（`AZURE_CONFIG_DIR` を分けて `az login`）で Fabric の MCP を直接呼ぶと、
  `The label expression (quality_document_restricted) does not match any node type … due to security configuration enforcement`。GQL でも同じ、公開文書は取れる
- 太郎は `grp-quality-team` のメンバー（Graph で確認）。限定用ワークスペースの閲覧者だが、**閲覧者には ReadAll が付かない**
- 対処（ポータル）：`lh_restricted` を `grp-quality-team` に ReadAll 付きで共有 → 約20分待っても太郎は拒否のまま（GraphModel の再取り込みもした）→
  **太郎個人にも ReadAll 付きで直接共有** → 太郎の MCP・デモ UI の4問目とも **QD-A008 が取れた**
- グループ共有が遅れて効いたのか、直接共有が効いたのかは**切り分けていない**（F2 の CU 消費を抑えるため打ち切り）。現在は両方付いたまま
- 容量メトリクス（14日間）：`Ontology AI` が約 148,468 CU(s) と大半を占める（自然文検索の変換）。F2 で試行を繰り返すと重い
  → 最終値と Throttling の読み方、コストは knowledge.md §7「オントロジーの自然文検索は容量を大きく使う」

### 20-4. 最終状態（2026-09-22）

| 問い合わせ | 太郎（所属） | 次郎（未所属） |
|---|---|---|
| ポータル：`quality_document_restricted` のインスタンス | 2件 | 401 |
| `lh_restricted` の SQL エンドポイント | （限定用ワークスペースの閲覧者） | ワークスペース自体が見えない |
| デモ UI 1〜3問 | §18-4 と同じ | §18-4 と同じ |
| デモ UI 4問目（Fabric IQ で限定文書） | QD-A008 が返る | `Function failed`（拒否）→「閲覧できない」と明示 |

権限：`ws-agent-search-iq` = 2人とも閲覧者＋モデルのビルド／`ws-agent-search-iq-restricted` = `grp-quality-team` が閲覧者＋`lh_restricted` を `grp-quality-team` と太郎個人に ReadAll 付きで共有（太郎個人の共有は §20-5 で削除）

確認後、**両ワークスペースを試用容量に戻し、F2 を停止**（Inactive を API で確認）。Fabric IQ の自然文検索を使う確認・デモは F2 に戻してから行う

### 20-5. グループ共有だけで ReadAll が効くか → 効く（確定）

- 自然文検索は使わず、太郎の資格情報で GraphModel に GQL（`MATCH (d:quality_document_restricted) …`）を投げて判定する。
  **GQL は試用容量でも動き、権限不足は同じ `security configuration enforcement` で出る**ので、CU をほぼ使わずに権限だけを確かめられる
- 個人共有ありの状態で太郎の GQL → 2件（QD-A007 / QD-A008）
- `lh_restricted` から太郎個人の共有を削除（10:30 JST ごろ）。ポータルの通知：**「この変更を有効にするには最大で 2 時間がかかることがあります」**
  → 前回グループ共有が約20分で効かなかったのは、この反映待ちだった可能性が高い
- 削除直後（10:30）は2件取れる。5分おきに試した（10:30〜10:50、12:06〜12:11。間は PC のスリープで途切れた）→ すべて2件
- **12:36（削除から約2時間6分、反映の上限を過ぎた時点）でも2件取れた → グループへの共有だけで効く、と確定**。
  前回グループ共有が約20分で効かなかったのは反映待ちだった
- 現在の権限：`lh_restricted` の共有は `grp-quality-team`（ReadAll）だけ。太郎個人の共有は削除済み

### 20-6. 容量メトリクスの読み直し（2026-09-23）

- 「何をやってコストが上がったのか」を Fabric Capacity Metrics の Compute タブで確認。
  ワークスペース `ws-agent-search-iq` の `ont_agent_search_iq` を選び、Operation name ごとに分解した
- 合計 177,059 CU(s) のうち `Ontology AI` が 163,936（93%）、`Ontology Modeling` が 2,267、OneLake 経由は 10 未満
- **CU(s) を Duration(s) で割って「CU/秒」に直すと原因が一目で分かる。**`Ontology AI` は 40.8 CU/秒＝F2（2 CU）の 20 倍。
  Spark ノートブックが 4.0、Warehouse が 2.0、Ontology Modeling が 0.13 なので、**桁が違うのは自然文検索だけ**
- §20-2・§20-3 で「引けない」を再試行した分は、**変換が通ったあとにグラフ側で拒否**されているので全部課金されている
- 結論と対策（1回あたりの単価、F8 に上げて短時間にするほうが安い、操作別の注意）は
  knowledge.md §7「容量とコスト」に追記した

### 20-7. 公式の消費レートを確認して §7 を裏取り（2026-09-23）

- Microsoft Learn の [ontology の Billing and Capacity Usage](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-capacity-usage) で
  メーター名と消費レートを確認。**`Ontology AI` は所要時間ではなくトークン課金（入力 400 / 出力 1,600 CU(s) per 1,000 tokens）**、
  **`Ontology Modeling` は「定義数 × 時間 × 0.0039 CU/時」で、CUD API のたびに30分窓が開く**
- **検算が合った**：実測 2,267.46 CU(s) ÷ 4.833h ÷ 0.0039 = 約33定義。
  `03_create_ontology_definition.py` の定義数（エンティティ5＋プロパティ24＋リレーション3 = 32）とほぼ一致
- `Ontology AI` も 163,936 CU(s) ÷ 800〜1,600 CU(s) で 100〜200回となり、記録してある試行回数・所要時間と整合
- **前版の「40.8 CU/秒」は割り算しただけの数字で課金の仕組みではない**と分かったので、
  §7 では「犯人探しの指標」として位置づけ直し、節約策を**トークンを減らす**話に書き換えた
- 新たに分かった重要事項（いずれも §7 に反映）：
  - **`Ontology AI` は Copilot のちょうど4倍の単価**（入力 400 対 100、出力 1,600 対 400）
  - **出力トークンは入力の4倍**。`naturalLanguageResponse: false` の節約効果が裏付けられた
  - **`Ontology Logic and Operations`（0.666667 CU/分）は「現在は無効」**。
    GQL・グラフ探索がいま無料なのはこのためで、有効化されると「権限検証は GQL で」の方針が崩れる【要確認】
  - **2026-10-01 に AI の消費測定が固定→動的に変わる（MC1469820、Fabric IQ オントロジーが対象）。**
    今回の実測値はそのベースラインになる

---

## 未完了（2026-09-22 時点）

- 不要だったアプリ登録②の削除
- PPT へのオントロジーの章の追加（`ppt_update_ontology_section.md` の指示で別 PC で作業）
- **自然文変換を自前に寄せる（案A）** → 下記「ネクストアクション」

---

## ネクストアクション：自然文変換を自前に寄せる（2026-09-23 検討）

> **解説ページ（初心者向け）**：「質問がクエリになるまで」 https://claude.ai/artifact/XPPkmn32QgU2gboMb2cxyE
> NL2SQL / NL2DAX / NL2GQL の違い、3つの経路の方式の違い、呼び方の対応表、50倍のコスト差、案Aの進め方。
> **非公開**なので、他の人に見せるにはページの Share から共有が要る。

### 背景

`Ontology AI` が検証コストの93%（§20-6・§20-7）。実効単価を 1M トークンあたりに揃えると
**入力 $20 / 出力 $80** で、いま Foundry にデプロイ済みの **gpt-4.1-mini（$0.40 / $1.60）のちょうど50倍**
（$0.18/CU時 = F2 $0.36/hr ÷ 2 CU で換算）。参考：GPT-4.1 フルモデル（$2.00 / $8.00）と比べても10倍。

**高いのは「AI だから」ではなく、同じ変換に50倍の値札が付いているから。**
→ Fabric を出る必要はない。差し替えるのは **NL→クエリ変換の1点だけ**。

### 案A：オントロジーを残し、NL→GQL の変換だけ自前にする【推奨】

```
現状： 質問 → [Ontology AI が NL→GQL]      → GQL 実行 → 結果
案A： 質問 → [自前: gpt-4.1-mini で NL→GQL] → GQL 実行 → 結果
                  ↑ ここだけ差し替え
```

- **実現可能なことは §20-5 で実証済み。**`POST /graphModels/<id>/executeQuery?preview=true` に
  **本人のトークン**で GQL を投げると、権限が正しく効き（`security configuration enforcement`）、
  **試用容量でも動き、CU をほぼ使わない**。権限検証の手段として見つけたものを、本番経路に使う
- 残るもの：オントロジー定義・グラフ・リレーション・権限モデル・Fabric IQ という製品の位置づけ・お客様への説明の筋
- 失うもの：Microsoft 製の変換精度。ただし現状も指示（英語で・スキーマ名で・1回にまとめて）で相当補正しているので、
  実質は自前プロンプトへの移設
- **今 Fabric IQ で直せない問題が自前なら直せる**：「日本語の業務用語（商品群名称）が変換できない」（§3）は、
  自前プロンプトなら日本語の用語辞書を入れるだけ。§11 の未確認事項が1つ消える

| | 現状 | 案A |
|---|---:|---:|
| Ontology Modeling | ほぼ無料 | ほぼ無料（変わらず） |
| GQL 実行 | ほぼ無料 | ほぼ無料（変わらず） |
| **NL→GQL 変換** | **¥6 / 問** | **¥0.12 / 問** |
| 200回の検証 | ¥1,200 | **¥25** |

**実装方針：売上ツールと同じ「Query Plan 方式」にする。**
`agent/query_fabric.py` + `agent/fabric_schema.py` は、LLM にクエリ文字列を書かせず
**構造化 JSON（どのメジャーを・何で集計し・何で絞るか）だけを出させ、Allowlist で検証してから DAX を組み立てる**
（fail closed）。**案Aは新しい方式ではなく、既にこのリポジトリで動いている方式をオントロジー側にも適用するだけ。**

- `agent/ontology_schema.py`（新規）… エンティティ型・プロパティ・リレーションの Allowlist ＋ **日本語同義語**
- `agent/query_ontology.py`（新規）… Query Plan（JSON）→ GQL 組み立て → `executeQuery` を本人トークンで実行
- `agent/main.py` … Toolbox（MCP）経由の `search_ontology` を上記ツールに差し替え。`FoundryToolbox` は残すか要判断

### 案B：オントロジーを使わず、SQL エンドポイント + RLS

**コストではなく「ご質問③への回答」が変わる案。**
§9.5 の3つの【大】の制約（見せ分けはレイクハウス単位／範囲ごとにワークスペースを分ける／
**閲覧範囲をまたぐリレーションが張れない**）は、すべて**オントロジー経路固有**であって Fabric 自体の制約ではない。

- [レイクハウスの SQL 分析エンドポイントと Warehouse は RLS に対応](https://learn.microsoft.com/en-us/fabric/data-warehouse/row-level-security)。
  Entra グループを述語に使える
- 限定文書と公開文書を**同じレイクハウス・同じワークスペース**に置ける／
  **部署をまたぐリレーションが普通の JOIN として成立する**（ご質問③に正面から答えられる）／行・列レベルで制御できる
  （オントロジーには**エンティティ単位の権限設定が無い**、§1）
- コストは Warehouse メーター（2 CU/秒）＋自前モデルで 1問 **¥0.15** 程度
- **注意：OneLake の ReadAll を与えると SQL の RLS は迂回される**。
  現状はグラフが OneLake を直接読むため ReadAll を付けている（§20-3）。RLS 方式なら ReadAll は付けない設計になる
- 失うもの：「業務の地図」という Fabric IQ の売り

### 案C：Fabric の外（Azure SQL / PostgreSQL / Cosmos DB Gremlin）【非推奨】

- お客様は Synapse → Fabric の移行中で、Fabric に寄せる前提がある
- データを外に出す ETL が増え、権限が Fabric と二重管理になる
- グラフDB が効くのは多段・可変長のパス探索。今回は**5エンティティ・3リレーション・2ホップ**で JOIN で十分

### 判断にあたっての注意

- **この案件は「Fabric IQ を評価すること」自体が成果物**の側面がある。案Aは Fabric IQ を残すので問題ないが、
  **案Bは「Fabric IQ を使わない」提案**になるため、お客様の期待とずれる可能性がある。提案の仕方に注意
- **2026-10-01 の動的課金変更（§7）で Ontology AI の単価が下がる可能性がある。**
  50倍が10倍になれば判断は変わる → **案Aを実装しつつ、10/1 後に再計測してから最終判断**する順序が安全

### 次にやること（順）

1. `executeQuery` を本人トークンで叩く最小コードを `scripts/dev/` に置き、公開・限定の両方で権限が効くことを再確認（試用容量で可）
2. `ontology_schema.py` の Allowlist と日本語同義語を定義
3. `query_ontology.py` で Query Plan → GQL の組み立てと検証（fail closed）
4. `main.py` のツールを差し替え、デモ UI の4問で太郎・次郎の結果が現状と一致することを確認
5. 10/1 以降に Ontology AI の単価を再計測し、案Aを続けるか戻すかを判断
