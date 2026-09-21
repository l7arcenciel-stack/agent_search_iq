# ナレッジ集：Fabric IQ 権限デモを新テナントで組んで分かったこと

新テナント（Japan East）での構築（2026-09-20〜21）で得た知見を、**テーマ別に**まとめたもの。
時系列の記録は [operations_log.md](operations_log.md)、手順は [new_tenant_setup.html](new_tenant_setup.html)、
元テナントへの反映は [apply_to_original_tenant.html](apply_to_original_tenant.html)。

- 【確認済】= 実機または公式ドキュメントで確認した事実
- 【要確認】= まだ確かめていないこと
- 実値（ID・シークレット）は書かない。現在の値は `python fabric_notebooks/00_check_status.py` で表示できる

---

## 0. デモが成立するための条件（ここだけは外せない）

| # | 条件 | 外すとどうなるか |
|---|---|---|
| 1 | **閲覧範囲ごとに「レイクハウス」を分ける**（テーブル分割では不可） | 読み取りを与えた瞬間、同じレイクハウス上の限定データまで見える |
| 2 | 容量が米国・EU 以外なら、AI の国外「**処理**」と「**格納**」の**両方**のテナント設定を有効にする | Fabric IQ の自然文検索が `Failed to translate NL query to ontology query.` で失敗 |
| 3 | Fabric IQ を使うときは**有料容量（F2 以上）**に載せる | 試用容量では自然文検索が動かない |
| 4 | フロントは**本人のトークン**で Foundry を呼ぶ（`FOUNDRY_CALL_AS=user`） | Fabric IQ だけ運用者の権限で動き、2人の差が出ない |
| 5 | AI Search の ACL は**実グループの GUID** で投入し、グループを変えたら**再投入** | エラーにならずに誰にも文書が見えなくなる |
| 6 | エージェントには「Fabric IQ へは英語・スキーマ名・1回にまとめて」と指示する | 変換失敗、並列呼び出しの失敗、推測での誤答 |
| 7 | デモ UI のサインインは**ブラウザ方式**（デバイスコードは使わない） | セキュリティ既定値・条件付きアクセスで `AADSTS530035` |

---

## 1. 権限分離（デモの核心）【確認済】

- オントロジーへの読み取りだけでは、**スキーマは見えるがデータは全エンティティ 401**。
  データの問い合わせは**バインド元レイクハウスに対して個別に認可**される
- レイクハウスへの読み取りは**全か無か**。テーブル単位では切れない
- OneLake セキュリティなら行・列制御ができるが、**有効にするとオントロジーのバインド対象から外れる**（共有画面にタブがあるので紛らわしい）
- 解決：**`lh_public`（全社員）と `lh_restricted`（品質チームのみ）に分け、1つのオントロジーから両方にバインド**。
  未所属ユーザーは公開エンティティだけ引け、限定エンティティは 401 になる（実機で確認）
- エッジテーブルも閲覧範囲で分ける（まとめると限定文書の存在と ID が公開側から見える）
- レイクハウスの共有では「すべての SQL エンドポイント データを読み取る」「すべての Apache Spark を読み取り…」の**両方にチェック**が要る（既定の共有だけでは 401）
- **エンティティ名と列名は権限に関係なく見える。**名前自体を秘密にしない
- アイテム単位の権限付与は **API が見つからずポータル作業**（`/items/<id>/permissions` などは 404）

## 2. Fabric Ontology（オントロジー）

### データとバインド【確認済】
- `valueType` は **String / Boolean / DateTime / Object / BigInt / Double のみ**。**Decimal が無い**ので、Decimal 列は null になる（回避策なし。Double にする）
- テーブル・列は **ASCII の snake_case**、`delta.columnMapping.mode` を設定しない、**マネージドテーブル**（`saveAsTable`）
- 値は日本語で構わない（名前だけ ASCII）
- **エンティティ型キー**を定義しないとバインドを保存できない
- 列名が一致していればプロパティのマッピングは自動で埋まる
- バインドを保存すると取り込みが走る。**リフレッシュはオントロジー本体ではなく、付随の GraphModel の `Refresh` ジョブ**（定義保存ごとに自動）
- マネージドテーブルはアタッチ中のレイクハウスにしか作れない → ノートブックはレイクハウスごとに実行（`TARGET` とアタッチ先の食い違いに注意。スクリプトにガードあり）

### アイテムの構造【確認済】
- オントロジーを作ると **Lakehouse・GraphModel・SQLEndpoint が自動生成**される。削除すると一緒に消える
- GraphModel の定義（`graphType.json` / `dataSources.json` / `graphDefinition.json`）でスキーマとバインドの実体を確認できる
- `GET /ontologies` は 200 だが**空配列**。存在確認は `GET /items`

### 定義の API【確認済】
- 作成・削除・`getDefinition`・`updateDefinition` が使える。**公開スキーマあり**：
  `https://developer.microsoft.com/json-schemas/fabric/item/ontology/{entityType|dataBinding|relationshipType|contextualization}/1.0.0/schema.json`
- 構造：`EntityTypes/<id>/definition.json`、`EntityTypes/<id>/DataBindings/<guid>.json`、
  `RelationshipTypes/<id>/definition.json`、**`RelationshipTypes/<id>/Contextualizations/<guid>.json`**（リレーションのバインドだけ名前も形も違う）
- `updateDefinition` で**既存と同名・別 ID** を送ると `ALMOperationImportFailed: Duplicate Name-Namespace`。
  **既存の名前→ID を読み取って引き継ぐ**（ID は一意ならよいが、名前は namespace 内で一意）
- ポータルで概要ページを開くと `EntityTypes/<id>/Overviews/definition.json` が増える。`endswith("/definition.json")` で拾うと誤読する
- エッジテーブルの一意の行キー（`edge_id`）は UI では要求されなかった（公式の記述に従って付けてはいる）

### ポータルの挙動【確認済】
- API で追加したエンティティは **Ctrl+Shift+R** するまで表示されない
- インスタンス画面の時間窓が既定で「1時間」。月次の時系列は範囲を広げないと見えない

## 3. Fabric IQ（Foundry からオントロジーを使う）

### 接続とツール【確認済】
- Foundry ポータルの **「ビルド > ツール」から Fabric IQ を作る**。「接続」画面の「Microsoft Fabric（プレビュー）」は**データエージェント用**で別物
- 認証は「OAuth ID パススルー」＝ `authType: UserEntraToken`、`audience: https://api.fabric.microsoft.com`。
  **本人のトークンが渡るので、独自の OAuth アプリもリダイレクト URI も不要**
- 接続 ID は **ARM のフルパス**（`/subscriptions/.../projects/<p>/connections/<name>`）
- 作成直後の接続は `isSharedToAll: false`。作成者以外が使えるか【要確認】
- MCP ツール名は **`fabric_iq_ontology___list_ontology_entity_types`** と **`fabric_iq_ontology___search_ontology`**。接頭辞なしで呼ぶと `No tool config matches tool name`
- エージェントのログ（`azd ai agent monitor`）には `ToolExecutionException` までしか出ない。
  **Toolbox の MCP エンドポイントへ直接 `tools/call` すると生のエラーが見える**（手順は operations_log.md §16）

### AI の要件【確認済】
- オントロジーの必須設定は**データエージェントの必須設定**を参照している。容量が米国・EU 以外なら次がすべて必要：
  Copilot / AI エージェントの利用、Copilot 容量の指定、Azure OpenAI の国外**処理**、Azure OpenAI の国外**格納**（会話履歴を最長28日保存）
- 「格納」の反映に **20〜30分**かかった（ドキュメント上は最大1時間）
- 「Microsoft サブプロセッサーとしての OpenAI」の設定は**不要**
- **試用容量では自然文検索（AI）が動かない。**F2 では同じ質問が成功。構築・閲覧・DAX・エンティティ一覧は試用容量でも動く
- 切り分けの目安：AI に届く前の失敗は約2秒、届いたうえでの変換失敗は約10秒

### 自然文検索の書き方【確認済】
- スキーマが英語で説明も同義語も無いと、**日本語の業務用語（「商品群名称」）は変換できない**。英語でスキーマ名を使うと通る
- 関係は**動詞でなくリレーション名**で書く（「produces」は失敗、「via produced_at_site」は成功）
- **並列に呼ぶと片方が `Function failed`** になった。必要な情報は**1回の質問にまとめる**と安定（3回中3回成功）
- `naturalLanguageResponse: true` だと要約生成がときどき `naturalLanguageResponseError: 403 (Forbidden)` を返す。
  **データ（`raw.Fields` / `raw.Value`）は取れている**のに、エージェントが「403」を権限不足と誤読した → `false` にする
- 取得できなかった項目を**推測で埋める**ことがあった（製品名を商品群名として回答）。指示で禁止する
- 以上は `main.py` の `AGENT_INSTRUCTIONS` に反映済み。より根本的には説明欄に日本語の同義語を書く【要確認：効果未検証】

## 4. セマンティックモデル（Direct Lake）【確認済】

- 作成 API は **TMSL（`model.bim`）しか受け付けない**。TMDL で投げると `Workload_FailedToParseFile`。一方 `getDefinition` は **TMDL で返る**
- レイクハウスから作れば全テーブル `mode: directLake`。接続は `Sql.Database("<SQLエンドポイント>", "<SQLエンドポイントID>")`
- 売上のファクトが複数あるときは、メジャー名に集計軸を含める（`商品群売上金額` / `拠点売上金額`）。「売上金額」で統一すると誤った組み合わせで空の結果になる
- 01（テーブル）/ 02（モデル）/ 03（オントロジー）/ `fabric_schema.py` の名前は常に一致させる

## 5. Foundry と azd

- `azd deploy` には `azure.yaml` の設定とは別に **`AZURE_AI_PROJECT_ID`** と **`FOUNDRY_PROJECT_ENDPOINT`** が要る（足りないと1つずつしか教えてくれない）【確認済】
- `azd` は `auth.useAzCliAuth true` で `az login` の資格情報を使える。`azure.ai.agents` 拡張が要る【確認済】
- ロール名が **Foundry User / Foundry Project Manager / Foundry Agent Consumer** に変わっている【確認済】。
  エージェント呼び出しだけなら **Foundry Agent Consumer**（`endpoints/interact/action`）。これで Invocations（OBO 登録）まで通るかは【要確認】
- **サブスクリプション Owner だけでは Toolbox を作れない**（dataActions を含まない）【確認済】
- **チャットモデルの容量 1（1,000 TPM）では、Fabric IQ のツール定義込みで即レート制限**。50 にした（Standard は従量課金で固定費は増えない）【確認済】
  元テナントでは**既存デモと容量を共有**するので注意
- `function_call_output` は Responses の `output` に含まれる【確認済】
- リポジトリ直下の大きなファイルはリモートビルドに送られる → `.agentignore` で除外【確認済】
- ログは `azd ai agent sessions list <agent>` → `azd ai agent monitor <agent> --session-id <id> --tail 300`
- プロジェクトの Bicep デプロイは、既存アカウントへの追加時に `RequestConflict`（一時的）になることがある。再実行で通る【確認済】

## 6. Entra ID

- **セキュリティ既定値**が有効なテナントでは【確認済】：
  - 管理者の Graph 操作（`az`）が `AADSTS530035` → `az login --scope "https://graph.microsoft.com//.default"` で MFA を通す
  - **デモ UI のデバイスコード方式サインインが `AADSTS530035` で拒否**（パスワード＋MFA は通ったうえで）。
    → **ブラウザ方式（認可コード＋PKCE、リダイレクト `http://localhost`）に切り替えた**。企業テナントの条件付きアクセスでもデバイスコードは禁止されがち
  - テストユーザーも MFA 登録が必須。デモ当日にやらせない
- `https://ai.azure.com` の委任権限の実体は **Azure Machine Learning Services の `user_impersonation`**【確認済】
- デモ UI は自アプリの API（`api://<appId>/.default`）のトークンを取るので、**自分自身の `access_as_user` にも同意**が要る【確認済】
- ライセンス割り当ての前に**利用場所（usageLocation）**を設定する。直後は反映待ちで失敗するので再試行【確認済】
- `POWER_BI_STANDARD` は **Power BI 無料版**。これで Fabric へのサインインとオントロジー閲覧はできた【確認済】。F64 未満で Pro が要るかは、エージェント経由を含めて【要確認】
- 既定の「All Company」は M365 グループなので ACL に使わない【確認済】
- 認証方法の登録状況を API で読むには追加の権限、登録レポートには Entra ID P1 が要る【確認済】

## 7. 容量とコスト【確認済】

- **容量が停止していると、アイテム一覧は取れるのに中身（`/tables`、`getDefinition`）だけ 404**。トークンの問題と誤診した
- 構築・データ作業は試用容量、**Fabric IQ を使う確認とデモ本番だけ F2** が安い
- ワークスペースを付け替えても **F2 は自動で止まらない**
- `infra/main.parameters.json` の `chatModelCapacity` は 50（1 に戻すとレート制限）

## 8. テナント設定の確認【確認済】

- `GET /v1/admin/tenantsettings` は**全設定を返さない**（Graph 作成・データエージェント関連などが無い）。ポータルで確認する
- 「データエージェント アイテムの種類を作成および共有できる」は**テナント設定に存在しなかった**（Copilot の設定に統合された模様）
- テナント設定の反映には時間がかかる（今回は20〜30分）

## 9. 作業環境（Windows）【確認済】

| 問題 | 対処 |
|---|---|
| Git Bash が `/subscriptions/...` を Windows パスに変換 | `export MSYS_NO_PATHCONV=1` |
| cmd が URL の `&` を区切りと解釈（`shell=True` の `az rest`） | シェルを介さず HTTP を直接叩く（`tenant_setup/_http.py`） |
| シェル経由の日本語 JSON が文字化け | UTF-8 ファイルか Python から送る |
| PowerShell 5.1 の `Set-Content -Encoding utf8` が BOM を付け、`.env` の1行目が壊れる | BOM なしで書く |
| **VS Code 内蔵ブラウザでの Microsoft サインインが `AADSTS900561`** | Edge / Chrome を使う |
| winget でインストールした直後は PATH に出ない | シェルを開き直す |
| Streamlit が全インターフェースで待ち受ける | `--server.address localhost` |

## 10. 元テナントへ持っていくときの要点

- **管理者依頼 #8（AI の国外処理・格納）は Fabric IQ 経路の成否を決める。**データ所在地の承認が要る見込みなので、休み明け初日に論点として出す
- 既存デモと**共有するもの**に注意：AI Search のインデックス（別名で作る）、モデルの容量（TPM）、Foundry プロジェクト
- デモ UI は**ブラウザ方式のサインイン**。アプリ登録にリダイレクト `http://localhost` が要る（`tenant_setup/20` に入っている）
- Fabric IQ の接続は本人トークン方式なので、**独自アプリの依頼は不要**（依頼は7件）
- 試用容量ではなく**有料容量**で Fabric IQ を使う

## 11. まだ確かめていないこと

- 2人での比較（デモ UI）の実地確認。特に **Foundry Agent Consumer で OBO 登録（Invocations）が通るか**、**Fabric IQ の接続が作成者以外でも使えるか**
- 説明欄に日本語の同義語を書くと、日本語の業務用語のままでも自然文検索が通るか
- 上流テーブルを更新したとき、オントロジーに自動で反映されるか
- F64 未満で閲覧ユーザーに Power BI Pro が要るか（エージェント経由を含めて）
