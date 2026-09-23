# ナレッジ集：Fabric IQ 権限デモを新テナントで組んで分かったこと

新テナント（Japan East）での構築（2026-09-20〜21）で得た知見を、**テーマ別に**まとめたもの。
時系列の記録は [operations_log.md](operations_log.md)、手順は [new_tenant_setup.html](../guides/new_tenant_setup.html)、
元テナントへの反映は [apply_to_original_tenant.html](../guides/apply_to_original_tenant.html)。

- 【確認済】= 実機または公式ドキュメントで確認した事実
- 【要確認】= まだ確かめていないこと
- 実値（ID・シークレット）は書かない。現在の値は `python fabric_notebooks/00_check_status.py` で表示できる

---

## 0. デモが成立するための条件（ここだけは外せない）

| # | 条件 | 外すとどうなるか |
|---|---|---|
| 1 | **閲覧範囲ごとに「レイクハウス」を分け、限定側は「別ワークスペース」に置く**（テーブル分割では不可） | 読み取りを与えた瞬間、同じレイクハウス上の限定データまで見える。同じワークスペースに置くと、閲覧者（#8）に SQL エンドポイント経由で見える |
| 1b | 限定側レイクハウスを所属グループに **ReadAll 付きで共有**する（ワークスペースの閲覧者だけでは不可） | 所属ユーザーでも Fabric IQ で限定データが引けず、`Function failed` になる（ポータルでは見えるので気づきにくい） |
| 1c | 閲覧範囲をまたぐリレーションを定義しない（限定文書→製品は属性でつなぐ） | エッジが取り込まれず、品質文書に関する自然文検索が**全員**失敗する |
| 2 | 容量が米国・EU 以外なら、AI の国外「**処理**」と「**格納**」の**両方**のテナント設定を有効にする | Fabric IQ の自然文検索が `Failed to translate NL query to ontology query.` で失敗 |
| 3 | Fabric IQ を使うときは**有料容量**に載せる。検証・デモ当日は **F8 以上を短時間**（F2 は回数が足りない、§7） | 試用容量では自然文検索が動かない。F2 だと自然文検索を1時間に7回ほどでスロットル圏に入り、前借りが翌日まで残る |
| 4 | フロントは**本人のトークン**で Foundry を呼ぶ（`FOUNDRY_CALL_AS=user`） | Fabric IQ だけ運用者の権限で動き、2人の差が出ない |
| 5 | AI Search の ACL は**実グループの GUID** で投入し、グループを変えたら**再投入** | エラーにならずに誰にも文書が見えなくなる |
| 6 | エージェントには「Fabric IQ へは英語・スキーマ名・1回にまとめて」と指示する | 変換失敗、並列呼び出しの失敗、推測での誤答 |
| 7 | デモ UI のサインインは**ブラウザ方式**（デバイスコードは使わない） | セキュリティ既定値・条件付きアクセスで `AADSTS530035` |
| 8 | デモユーザーを**ワークスペースの閲覧者**にする（アイテム共有だけでは不可） | Fabric IQ のツール一覧が 403 になり、**どの質問も回答が空**になる。**閲覧者には同じワークスペースの全レイクハウスの SQL 読み取りが付くので、限定データは別ワークスペースに置く（#1、§1）** |
| 9 | セマンティックモデルにデモユーザーの**ビルド権限**を付ける | 売上の DAX 照会が 404 になり、全員「権限がない」と回答される |
| 10 | 製品に関連する売上は「**Fabric IQ で商品群コードを取る → そのコードで売上を照会**」の順にさせる | 製品IDで絞っても効かず、全商品群の売上を A008 の売上として答える／推測で商品群を埋める |

---

## 1. 権限分離（デモの核心）【確認済】

- **オントロジー自体では見せ分けはできない。**オントロジーのアイテム権限（読み取り）は「開けるかどうか」の入口でしかなく、
  エンティティ単位・リレーション単位の権限設定は無い。**誰にどのデータを見せるかは、バインド元のレイクハウス（とそのワークスペース）の権限だけで決まる**
  （オントロジーは権限を迂回しないが、権限を足すこともできない）
- オントロジーへの読み取りだけでは、**スキーマは見えるがデータは全エンティティ 401**。
  データの問い合わせは**バインド元レイクハウスに対して個別に認可**される
- レイクハウスへの読み取りは**全か無か**。テーブル単位では切れない
- OneLake セキュリティなら行・列制御ができるが、**有効にするとオントロジーのバインド対象から外れる**（共有画面にタブがあるので紛らわしい）
- 解決：**`lh_public`（全社員）と `lh_restricted`（品質チームのみ）に分け、1つのオントロジーから両方にバインド**。
  未所属ユーザーは公開エンティティだけ引け、限定エンティティは 401 になる（実機で確認）
- エッジテーブルも閲覧範囲で分ける（まとめると限定文書の存在と ID が公開側から見える）
- レイクハウスの共有では「すべての SQL エンドポイント データを読み取る」「すべての Apache Spark を読み取り…」の**両方にチェック**が要る（既定の共有だけでは 401）
- **ワークスペースの閲覧者にすると、権限の分離は崩れる（エージェント経由だけは守られる）。**【確認済・2026-09-22 訂正】
  - 守られる：OneLake（ReadAll が付かないので `lh_restricted` は 403）、Fabric IQ の MCP（制限付きエンティティは
    `does not match any node type in the graph, or you don't have access … due to security configuration enforcement` で拒否）
  - **漏れる：`lh_restricted` の SQL 分析エンドポイント**（閲覧者の「読み取り」で `SELECT * FROM quality_document_restricted` が通る）、
    **オントロジー画面のインスタンス一覧**（未所属ユーザーにも限定文書が表示される）
  - 当初「崩れない」と書いたのは MCP と OneLake だけで確かめたため。ポータルは閲覧者付与後に再確認していなかった
  - オントロジーが自動で作るレイクハウス（`ont_…_lh_…`）にはテーブルが無く、漏れの経路ではない
- **解決：`lh_restricted` を別ワークスペース（`ws-agent-search-iq-restricted`、閲覧者は品質チームグループだけ）に移す。**【確認済・2026-09-22】
  - オントロジーは**別ワークスペースのレイクハウスにもバインドできる**（API の `workspaceId` を変えるだけ。GraphModel の取り込みも通る）
  - 未所属ユーザーは限定エンティティのインスタンスが 401、所属ユーザーは見える
  - **所属ユーザーが Fabric IQ で限定データを引くには、`lh_restricted` の ReadAll が要る。**【確認済・2026-09-22】
    ワークスペースの閲覧者だけでは、ポータルのインスタンス表示は通るが、MCP・GQL は `security configuration enforcement` で拒否される
    （グラフは OneLake を本人の権限で読むため）。`lh_restricted` を品質チームに「すべての Apache Spark を読み取り…」付きで共有する
  - **グループへの共有だけで足りる。**【確認済・2026-09-22】付けて約20分では効かなかったが、それは反映待ちだった
    （個人の共有を外して約2時間たっても、グループ経由だけで取れた）
  - **アイテムの権限変更は反映に最大2時間かかる**（ポータルの通知「この変更を有効にするには最大で 2 時間がかかることがあります」）。
    付けた直後・外した直後の確認では判定できない。デモ前日までに付けておく
  - **管理者で試すと全部通る。**所属ユーザーの資格情報で MCP を直接呼ぶと、`Function failed` の中身（生のエラー）が見える（`AZURE_CONFIG_DIR` を分けて `az login`）
- **別のレイクハウスのノードを結ぶリレーションは使えなかった。**限定文書（`lh_restricted`）→ 製品（`lh_public`）のエッジは
  GraphModel に取り込まれず（Refresh は成功扱い）、管理者でも `does not match any edge type` になった【確認済。仕様か不具合かは未確認】
  - しかも**定義してあるだけで、品質文書に関する自然文検索が全員失敗する**（変換がそのリレーションを使うため）
  - 対処：リレーションを定義せず、限定文書は `product_id` プロパティで製品と対応付ける
  - 意味：**閲覧範囲をまたぐ「関係」はオントロジーで張れない**。部署ごとにデータを分けると、部署をまたぐ関係は属性（キー列）でしか表せない
- 複数のエンティティを1つの自然文にまとめて聞くと、片方が0件のとき**エラーなしで全体が空**になった。エンティティごとに順に聞く
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
- **ワークスペースのロールが無いユーザーは、Fabric IQ の MCP が 403**（`InsufficientPrivileges`）。オントロジーのアイテム共有だけでは足りない。
  管理者は通るので気づきにくい。詳細は operations_log.md §18
  - エージェントは質問のたびに**最初にツール一覧（`tools/list`）を取得**し、ここで失敗すると
    `Failed to enter context manager` … `HTTP_403` で**応答全体が失敗**する（Fabric IQ を使わない質問も空になる）
  - 本人の資格情報でオントロジー MCP を直接呼んでも 403 → デモ UI のサインイン方式の問題ではなかった
  - **ワークスペースの閲覧者にすると解消**（2人とも）。閲覧者でも権限の分離は崩れない（§1）
  - 公式ドキュメントが挙げる Fabric ライセンス（無料版のまま）・Foundry User ロール（Foundry Agent Consumer のまま）は変えずに通った
- エージェントのログ（`azd ai agent monitor`）には `ToolExecutionException` までしか出ない。
  **Toolbox の MCP エンドポイントへ直接 `tools/call` すると生のエラーが見える**（手順は operations_log.md §16）

### Fabric Data Agent は使わない【公式ドキュメントで確認済・2026-09-23】

出典：[Fabric data agent creation](https://learn.microsoft.com/en-us/fabric/data-science/concept-data-agent)、
[Data agent consumption](https://learn.microsoft.com/en-us/fabric/fundamentals/data-agent-consumption)

- **Data Agent とオントロジーは別レイヤー。**Data Agent は司会役（質問の解釈 → データソース選択 → ツール呼び出し → 整形）で、
  **オントロジーは Data Agent がつなげる5つまでのデータソースの1つ**。「どちらか」ではない
- **Data Agent からオントロジーを呼ぶことは可能**（データソースに ontologies が明記されている）。
  ただし **Data Agent は Copilot と同じメーター（入力 100 / 出力 400 CU秒）で、Ontology AI はその4倍**なので、
  **両方通すと二重に課金される**

| 構成 | 司会役 | 翻訳 | 1問 |
|---|---|---|---:|
| Fabric Data Agent → オントロジー | Data Agent 400 CU(s) ≒ ¥3 | Ontology AI 800 CU(s) ≒ ¥6 | **¥9** |
| **Foundry Agent → Fabric IQ（現在）** | gpt-4.1-mini ≒ ¥0.1 | Ontology AI 800 CU(s) ≒ ¥6 | **¥6** |
| Foundry Agent → 自前 NL2GQL（案A） | gpt-4.1-mini ≒ ¥0.1 | gpt-4.1-mini ≒ ¥0.12 | **¥0.2** |

- **いまの構成（Foundry Hosted Agent が司会役）は、すでに Copilot メーターを1円も使っていない。**
  Data Agent に乗り換えると**悪化する**
- **そもそもこのデモでは要件を満たさない**（公式の制限）：
  - **非英語に未対応** → デモは日本語
  - **非構造化データ（.pdf / .docx / .txt）に未対応** → 文書検索（AI Search）の経路が載らない
  - **使用する LLM を変更できない** → 安いモデルに寄せる余地がない
  - 回答は25行×25列で打ち切り／2人分を並べる独自 UI も作れない
- 参考：Data Agent が使う変換は **NL2SQL（レイクハウス・ウェアハウス）／NL2DAX（セマンティックモデル）／
  NL2KQL（KQL DB）／Microsoft Graph**。セマンティックモデルは**読み取りだけでよく、ビルド権限は不要**
  （§4 のデモ独自経路＝本人トークンでの `executeQueries` はビルドが要る、という違い）

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
- **製品から売上を求める質問（「A008の商品群の売上」）で Fabric IQ を呼ばず、売上ツールを製品IDで直接絞ろうとした。**指示とツール説明に「Fabric IQ で商品群コード → そのコードで照会」の手順を書き、売上ツールの列から製品を外したら、2人とも Fabric IQ → 売上の順に呼ぶようになった【確認済】
- 以上は `agent/main.py` の `AGENT_INSTRUCTIONS` に反映済み。より根本的には説明欄に日本語の同義語を書く【要確認：効果未検証】

## 4. セマンティックモデル（Direct Lake）【確認済】

- 作成 API は **TMSL（`model.bim`）しか受け付けない**。TMDL で投げると `Workload_FailedToParseFile`。一方 `getDefinition` は **TMDL で返る**
- レイクハウスから作れば全テーブル `mode: directLake`。接続は `Sql.Database("<SQLエンドポイント>", "<SQLエンドポイントID>")`
- 売上のファクトが複数あるときは、メジャー名に集計軸を含める（`商品群売上金額` / `拠点売上金額`）。「売上金額」で統一すると誤った組み合わせで空の結果になる
- 01（テーブル）/ 02（モデル）/ 03（オントロジー）/ `agent/fabric_schema.py` の名前は常に一致させる
- このデモのモデルは **`lh_public` の SQL エンドポイント**に接続し、**RLS のロールは無い**＝売上は全員が同じ結果になる設計（README の期待結果どおり）
- 本人のトークンで `executeQueries` を呼ぶには、モデルへの**読み取り＋ビルド（ReadExplore）**が要る。
  **ワークスペースの閲覧者にはビルドが付かず**、`404 PowerBIEntityNotFound`（not found or you do not have permission）になる。
  エージェントはこれを「閲覧権限がない」と回答する。**ビルドを付けると解消**【確認済】
- ポータルの「権限の管理」でユーザーを追加すると、既定で**書き込み・再共有**まで付く。デモユーザーには**読み取り＋ビルドだけ**にする
- 閲覧ユーザーが **Power BI 無料版のままでも、F2 上のモデルに本人トークンで DAX 照会できた**【確認済】
- **product → product_group は多対一で、絞り込みは product_group → product の向きにしか伝わらない。**`product[product_id]` で絞って `商品群売上金額` を出すと、エラーにならず**全商品群の行が返る**（誤答の元）。このため `agent/fabric_schema.py` の列から製品を外した【確認済】

## 5. Foundry と azd

- `azd deploy` には `azure.yaml` の設定とは別に **`AZURE_AI_PROJECT_ID`** と **`FOUNDRY_PROJECT_ENDPOINT`** が要る（足りないと1つずつしか教えてくれない）【確認済】
- `azd` は `auth.useAzCliAuth true` で `az login` の資格情報を使える。`azure.ai.agents` 拡張が要る【確認済】
- ロール名が **Foundry User / Foundry Project Manager / Foundry Agent Consumer** に変わっている【確認済】。
  エージェント呼び出しだけなら **Foundry Agent Consumer**（`endpoints/interact/action`）。**これで Invocations（OBO 登録）と Fabric IQ まで通った**（Foundry User は不要だった）【確認済】
- **サブスクリプション Owner だけでは Toolbox を作れない**（dataActions を含まない）【確認済】
- **チャットモデルの容量 1（1,000 TPM）では、Fabric IQ のツール定義込みで即レート制限**。50 にした（Standard は従量課金で固定費は増えない）【確認済】
  元テナントでは**既存デモと容量を共有**するので注意
- `function_call_output` は Responses の `output` に含まれる【確認済】
- **agent_framework（1.19.0）の `@tool` は、`list[BaseModel]` の引数を BaseModel ではなく dict のまま関数に渡す。**`f.model_dump()` が `AttributeError` になり、モデル側には `Error: Function failed.` としか見えない。絞り込み付きの売上照会は、これが原因で**最初から一度も成功していなかった**。dict でも受けるように直した【確認済】
  - ツール内の想定外の例外や引数検証の失敗も、すべて `Function failed.` になる。ツールの中で例外を捕まえて中身を返すと原因を追える
  - ローカルで `await main.query_fabric_tool.invoke(arguments={...})` を呼べば、デプロイせずに再現できる
- デモ UI の「処理詳細を見る」に、各ツールの**引数と結果**を表示するようにした。セッションが休止するとログ（`azd ai agent monitor`）は取れなくなるので、画面で見るほうが早い
- `azure.yaml` の `project` フォルダの中身はすべてリモートビルドに送られる【確認済】。以前はリポジトリ直下が project で、手元のインストーラ（`*.msi`）まで送られていた → project を `agent/` に分けた（2026-09-22）
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
- `POWER_BI_STANDARD` は **Power BI 無料版**。これで Fabric へのサインインとオントロジー閲覧はできた【確認済】。F2 上のモデルへの本人トークンでの DAX 照会も無料版で通った。レポートの閲覧に Pro が要るかは【要確認】（このデモでは使わない）
- 既定の「All Company」は M365 グループなので ACL に使わない【確認済】
- 認証方法の登録状況を API で読むには追加の権限、登録レポートには Entra ID P1 が要る【確認済】

## 7. 容量とコスト【確認済】

### 基本

- **容量が停止していると、アイテム一覧は取れるのに中身（`/tables`、`getDefinition`）だけ 404**。トークンの問題と誤診した
- 構築・データ作業は試用容量、**Fabric IQ の自然文検索を使う確認とデモ本番だけ有料容量**にする
- ワークスペースを付け替えても **F2 は自動で止まらない**
- `infra/main.parameters.json` の `chatModelCapacity` は 50（1 に戻すとレート制限）

### 【要注意】2026-10-01 に AI の課金方式が変わる

- Microsoft は **2026年10月1日から、Fabric の AI 機能の消費測定を「固定」から「動的」に変更**する
  （Message Center **MC1469820**）。**対象に Fabric IQ のオントロジーが明記されている**
  （ほかに Copilot in Fabric、Fabric データエージェント、AI Functions）
- 変更後は「タスクが実際に必要としたリソース」を測る方式になり、**モデルの大きさ・要求の複雑さ・使ったツール・処理量で CU が変動**する。
  つまり**下記の 400 / 1,600 CU(s) という固定レートは 10/1 以降そのままではない**
- Microsoft 自身が管理者に「**10/1 より前にメトリクスアプリでベースラインを取っておく**」「変更後1か月は毎週比較する」と案内している
  → **本節の実測値（163,936 CU(s) / 約200回）が、そのベースラインになる**
- デモ・お客様説明が 10/1 以降になる場合、**コストの話は「この数字は9月時点の実測。10/1に方式が変わるので再計測が要る」と添える**
- 公式の消費レート表も「レートは予告なく変更される」と明記している

### 課金メーターと消費レート【公式ドキュメントで確認済・2026-09-23】

出典：[Billing and Capacity Usage（ontology）](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-capacity-usage)、
[Copilot consumption, usage, and billing in Fabric](https://learn.microsoft.com/en-us/fabric/fundamentals/copilot-fabric-consumption)

| メーター名（メトリクスアプリの Operation name） | 何を測っているか | 単位 | 消費レート |
|---|---|---|---|
| **Ontology AI**（`Ontology AI Operations`） | オントロジーに対する**AI の推論・自然文クエリ** | 1,000 トークン | **入力 400 CU(s)／出力 1,600 CU(s)** |
| **Ontology Modeling** | **オントロジー定義**（エンティティ型・リレーション型・プロパティ・データバインド）の保持 | 定義1件・1時間 | **0.0039 CU/時** |
| Ontology Logic and Operations | 可視化・ロジック・グラフ作成・探索・クエリ（API / SQL エンドポイント） | 分 | 0.666667 CU/分 ※**現在は無効** |
| OneLake Cache | グラフのキャッシュ保存 | 月間の使用量 | OneLake Cache と同レート |
| （参考）Copilot in Fabric | Copilot 全般 | 1,000 トークン | 入力 100／**キャッシュ入力 10**／出力 400 CU(s) |

ここから読み取れること：

- **`Ontology AI` は所要時間ではなく「トークン数」で課金される。**速く終わらせても安くならない。**減らせるのはトークンだけ**
  （前版で書いた「40.8 CU/秒」は実測から割り算しただけの数字で、課金の仕組みではない。
  ただし**メトリクスアプリで犯人を探すときの指標としては有効**なので、下の「見つけ方」に残した）
- **出力トークンは入力の4倍高い**（1,600 対 400）。`naturalLanguageResponse: false`（要約文を作らせない）は**確認済みの節約策**。
  要約で200〜300トークン出力が増えると、それだけで **320〜480 CU(s)＝1回あたり4〜6割増し**になる
- **`Ontology AI` は Copilot のちょうど4倍の単価**（入力 400 対 100、出力 1,600 対 400）。
  **同じ質問でも、Copilot 経由より Fabric IQ 経由のほうが4倍高い**
- **入力トークンにはオントロジーのスキーマが毎回載る。**前版で【推測】としていた
  「エンティティ・プロパティを増やすと1回の単価が上がる」は、この仕組みから**そうなる**
- 【要確認】Copilot 側には**キャッシュ入力 10 CU(s)（1/10）**があり、
  「システム指示・スキーマ・会話履歴など直前のリクエストと共通する前置きはキャッシュから安く提供される」と書かれている。
  ontology 側の表にキャッシュ入力の行は**無い**。効くなら、**間を空けずに連続で試すほうが安い**ことになる
- **`Ontology Logic and Operations`（0.666667 CU/分）は「現在は無効」。**
  いまグラフ探索・GQL・SQL エンドポイントがほぼ無料なのは**このメーターが止まっているから**であって、恒久的に無料ではない。
  有効化されると 2時間のセッションで約 5,600 CU(s)（140分 × 0.666667）になる。
  **「権限検証は GQL でやる」という方針は、これが有効になった時点で見直しが要る**【要確認：有効化の予定は未公表】

### 公式の計算式と、今回の実測値の検算【確認済・2026-09-23】

**Ontology AI**（公式例：入力2,000・出力500トークン）

```
(2,000 × 400 + 500 × 1,600) / 1,000 = 1,600 CU(s) ＝ 26.67 CU分 ／ 1リクエスト
```

- 今回の実測 163,936 CU(s) をこれで割ると **約102回**。今回のオントロジーは5エンティティと小さいので
  1回 800 CU(s) 程度（入力 約1,200・出力 約200トークン）とすると **約200回**。
  実測 Duration 4,020 秒 ÷ 200回 = 1回 20秒で、記録してある実測（7〜32秒）と合う → **今回は1回 800〜1,000 CU(s)、150〜200回**と見てよい
- **F2 の1日の枠は 172,800 CU(s)（2 CU × 86,400秒）→ 1日 170〜200回が上限**
- 参考：公式は F64（1,536 CU時/日）で 1,600 CU(s) のリクエストを**1日3,456回**としている

**Ontology Modeling**

```
定義数 × 課金窓の時間(h) × 0.0039 CU/時
```

- **CUD（作成・更新・削除）API を叩くたびに30分の課金窓が開く。**窓が重なる分は二重課金されない
  （公式例：30分窓の15分後にもう1回叩くと、合計45分）
- **検算**：今回の実測 2,267.46 CU(s)（= 0.6299 CU時）÷ 4.833時間（Duration 17,400秒）÷ 0.0039 = **約33定義**。
  `03_create_ontology_definition.py` の定義数（エンティティ5 ＋ プロパティ24 ＋ リレーション3 = 32）と**ほぼ一致**。式が正しいことを確認できた
- Duration 17,400秒 ÷ 1,800秒 ≒ **約10回ぶんの30分窓** ＝ オントロジー定義を10回ほど更新した、という意味
- **重要：このメーターは定義数に比例する。**今回は32定義だから安かっただけ。
  公式例の**1,000定義なら1回の編集（30分窓）で 1.95 CU時 = 7,020 CU(s)**。F2 の1日の4%が、編集1回で飛ぶ
  → **お客様の本番規模のオントロジーでは、AI を使わなくても「定義を編集しているだけ」で容量を食う**

### メトリクスアプリでの犯人の見つけ方【確認済・2026-09-23】

合計 CU(s) だけ見ても原因は分からない。**Compute タブでアイテムを選び、Operation name ごとに分解する**（2枚目の画面）。
さらに **CU(s) ÷ Duration(s) = 「CU/秒」**に直すと、桁違いのものが一目で分かる。今回の14日間を換算した値：

| 操作 | CU(s) | Duration(s) | CU/秒 | 目安 |
|---|---:|---:|---:|---|
| **Ontology AI** | 163,936 | 4,020 | **40.8** | **F2（2 CU）の 20 倍** |
| SynapseNotebook（Delta 作成、Spark） | 1,500 | 375 | 4.0 | F2 の 2 倍 |
| Warehouse（SQL 分析エンドポイント） | 3,603 | 1,776 | 2.0 | ちょうど F2 一杯 |
| Ontology Modeling | 2,267 | 17,400 | 0.13 | ほぼ無料 |
| GraphIndex | 253 | 12,010 | 0.02 | ほぼ無料 |
| Dataset（DAX 照会） | 41 | 82 | 0.5 | 安い |

- **この CU/秒 は課金の単位ではない**（上記のとおり Ontology AI はトークン課金）。**犯人探しの指標**として使う
- Peak utilization 138.93K%（約1,389倍）は、この 40.8 CU/秒 が瞬間的に出たもの
- **オントロジーで高いのは「自然文で聞く」ところだけ。**定義の作成・Refresh・GraphIndex・エンティティ一覧・GQL は**ほぼ無料**
  （ただし無料の理由は上記「Logic and Operations が現在無効」だから。恒久的ではない）

### コストを抑える進め方【今回の反省】

1. **権限・リレーションの切り分けは試用容量で GQL。**自然文検索は使わない（§20-5 で確立）。
   `security configuration enforcement` は GQL でも同じ文言で出るので、**CU をほぼ使わずに権限だけ判定できる**。これを既定にする
2. **自然文検索が要る日だけ SKU を上げて短時間。**Fabric の従量課金は **SKU サイズ × 稼働時間**なので、
   同じ検証を F8 で 1/4 の時間でやれば**金額はほぼ同じでスロットリングだけ消える**。F2 に張り付く必要はない

   | SKU | CU | 1時間に打てる回数（1回1,000 CU(s) 換算） | 1時間の概算コスト【要確認】 |
   |---|---:|---:|---:|
   | F2 | 2 | **約 7 回**でスロットル圏 | 約 ¥55 |
   | **F8（推奨）** | 8 | **約 29 回** | 約 ¥220 |
   | F16 | 16 | 約 58 回 | 約 ¥440 |

   （$0.36/時・¥150/$ 換算の概算。日本リージョンの実単価は未確認【要確認】）
3. **デモ本番も F8 以上で当日だけ。**F2 だと前日の予行の前借りが当日まで残る
4. **減らせるのはトークンだけ。**所要時間ではなくトークン数で課金されるので、効くのは次の3つ：
   - **出力を出させない** — `naturalLanguageResponse: false`。出力は入力の4倍単価なので、これだけで1回あたり4〜6割違う【確認済】
   - **スキーマを小さく保つ** — 入力トークンに毎回オントロジーのスキーマが載る。
     **デモで使わないエンティティ・プロパティ・長い説明文を足すと、使っていなくても全クエリの単価が上がる**
   - **呼び出し回数を減らす** — 「必要な情報は1回の質問にまとめる」指示（並列呼び出しの失敗対策でもある）。
     デモ UI の質問は4問。1人あたり4回 × 人数 × 試行回数、で見積もる
5. **打つ前に回数を決める。**「動くまで試す」をやると 1回10分ぶんが積み上がる。
   10回試して駄目なら、AI を使わない経路（GQL・MCP 直叩き）に切り替えて原因を特定する
6. **オントロジー定義の編集は、まとめて一気に。**CUD API 1回で30分の課金窓が開き、窓が重なる分は二重課金されない。
   定義数が多いほど高くなるので、**本番規模では「1日中ちょこちょこ直す」が一番高い**
7. **グラフの自動更新スケジュールを確認する。**Graph アイテムの定期 Refresh も容量を使う。
   使っていない期間は無効にできる（[公式](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-capacity-usage)）

### 何をするときにコストを気にするか

| やること | メーター | 今回（32定義） | 注意 |
|---|---|---|---|
| Delta テーブル作成・ノートブック | Spark | 安い | 気にしなくてよい |
| **オントロジー定義の作成・更新・バインド** | **Ontology Modeling** | ほぼ無料 | **定義数に比例。本番規模（1,000定義）だと編集1回＝7,020 CU(s)。編集はまとめて** |
| Graph の Refresh・GraphIndex | Graph | ほぼ無料 | 自動更新スケジュールは使わない期間は無効に |
| エンティティ一覧・インスタンス表示 | Logic and Operations | ほぼ無料 | **メーターが現在無効だから。有効化されたら有料になる**【要確認】 |
| **GQL / MCP でのグラフ照会** | Logic and Operations | **ほぼ無料** | **権限検証はこれでやる。**ただし上と同じ理由で恒久的ではない |
| セマンティックモデルへの DAX 照会 | Dataset | 安い | |
| SQL 分析エンドポイント | Warehouse | 中 | F2 を一杯使う速度。繰り返すなら意識する |
| **Fabric IQ の自然文検索** | **Ontology AI** | **93%** | **ここだけ桁違い。トークン課金・出力は入力の4倍・失敗も満額。回数を決めて打つ** |

### スロットリングの読み方【確認済・2026-09-22】

- 今回はスロットリング未発生（Rejected count 0）。Throttling の最大値は Interactive delay（10分先）約60%、
  Interactive rejection（60分先）約40%、**Background rejection（24時間先）約40%** で、いずれも 100% 未満
- ただし **24時間枠の4割を前借りした状態**。**同じ量の検証を同じ日にあと1〜2回やると拒否が始まる**計算
- **AI の消費は24時間かけて平滑化される**ので、**前借りが翌日まで残る**。当日の朝に枠が空いているとは限らない
- **容量を一時停止すると、前借り（超過）分がまとめて請求に乗る**（Microsoft Learn の説明。今回の金額は未確認）
- **お客様には、既存業務と同じ容量で試さないよう勧める**：拒否が始まると、同じ容量の既存レポート・モデルも遅延・拒否される。
  公式の目安では F64（1,536 CU時/日）で 1,600 CU(s) のリクエストを1日3,456回まで、なので**F64 なら容量自体は足りる**。
  問題は**24時間平滑化で同じプールを食い合う**こと。検証のピークが既存ワークロードの拒否を招く

### 実績値（メトリクスは 2026-09-23 時点、コストは 2026-09-22 時点）

- 容量メトリクス（F2、14日間）：合計 **177,059 CU(s)** のうち **`Ontology AI` が 163,936 CU(s)（93%）**。
  Ontology Modeling は 2,267、Warehouse・Notebook は各 1,000〜3,600 程度。Avg utilization 553%、Peak 138.93K%
- **2日間（§20-2 のリレーション切り分け、§20-3 の権限切り分け、デモ UI の2人分の通し実行）で、
  F2 のほぼ丸1日分を自然文検索だけで使った**。Users 3 は太郎・次郎・管理者
- 自然文検索だけを F2 の稼働時間に換算すると **約22.8時間ぶん ≒ $8 ≒ ¥1,200**（1回あたり ¥6〜9）。
  **金額としては小さい。効いているのは金額ではなく F2 の 2 CU という天井**
- 今月のコスト（2026-09-22 時点、リソースグループ）：合計 ¥2,303、うち Microsoft Fabric ¥1,957、Foundry Tools ¥178、
  Azure Container Apps ¥147、Foundry Models ¥20、AI Search ¥1。
  コストの反映は1日程度遅れるので、停止時の精算分は翌日以降に出る可能性がある
- 止めているあいだの課金：F2 は停止で 0（OneLake の保存は課金されるが今回はごくわずか）、AI Search は free、Foundry は従量。
  **Container Apps（Hosted Agent の実行環境）が待機中も課金されるかは未確認**【要確認】
- 公式レートで裏付けた解釈：`Ontology AI` 163,936 CU(s) ≒ **1回 800〜1,000 CU(s) × 150〜200回**
  （1回あたり入力 約1,200・出力 約200トークン相当）。`Ontology Modeling` 2,267 CU(s) ≒ **32定義 × 約4.8時間ぶんの課金窓**（更新10回程度）

### 出典

- [Billing and Capacity Usage（ontology）](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-capacity-usage) — 消費レート表、計算例、背景ジョブ扱い、管理のヒント
- [Copilot consumption, usage, and billing in Fabric](https://learn.microsoft.com/en-us/fabric/fundamentals/copilot-fabric-consumption) — Copilot のレート（比較用）、プロンプトキャッシュ、平滑化とスロットリング
- [Throttling in Microsoft Fabric](https://learn.microsoft.com/en-us/fabric/enterprise/throttling) — 平滑化（対話 5分・背景 24時間）と拒否の段階
- [Fabric operations](https://learn.microsoft.com/en-us/fabric/enterprise/fabric-operations) — メトリクスアプリの Operation name 一覧
- Message Center **MC1469820** — 2026-10-01 の AI 消費測定の変更（固定 → 動的）

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

- 説明欄に日本語の同義語を書くと、日本語の業務用語のままでも自然文検索が通るか
- 上流テーブルを更新したとき、オントロジーに自動で反映されるか
- **`Ontology AI` にプロンプトキャッシュ（Copilot の「キャッシュ入力 10 CU(s)」相当）が効くか**（§7）。
  効くなら「間を空けずに連続で試すほうが安い」ことになるが、ontology の公式レート表にキャッシュ入力の行が無い
- **`Ontology Logic and Operations`（0.666667 CU/分）がいつ有効になるか**（§7）。
  有効化されると、いま無料でやっている GQL・グラフ探索・SQL エンドポイントが有料になり、**「権限検証は GQL で」の方針が崩れる**
- **2026-10-01 の動的消費モデルで、自然文検索1回の CU がどう変わるか**（§7）。10/1 以降に再計測して今回の実測と比べる
- **Japan East の F SKU の実単価**（§7 の概算は $0.36/時・F2 換算。米国比 10〜15% 高い見込み。請求書で突き合わせる）
- **容量を一時停止したときの精算額**（前借り分がいくら乗ったか）
