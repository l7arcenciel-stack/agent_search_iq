# 実行記録（新テナントで API / CLI から行った操作）

新テナントの構築で、**何を・どうやって・なぜ**実行したかの時系列の記録。
結果とハマった点も併記する。手順としてまとめ直したものは
[new_tenant_setup.html](new_tenant_setup.html)、再実行できる形にしたものは
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
- 「Microsoft サブプロセッサーとしての OpenAI」の2設定は必須一覧に無いため、有効化していない。
- 元テナントでは**国外処理と国外保存**の両方が要るため、データ所在地の社内承認が必要になる見込み（元テナント手順書の依頼 #8）。

---

## 未完了（2026-09-21 時点）

- 指示変更後のエージェント経由での確認（Fabric IQ を使う質問）
- 試用容量で AI 機能（自然文検索）が動くかの確認
- 2人での比較（デモ UI）
- 不要だったアプリ登録②の削除
