# agent_search_iq — Fabric IQ（オントロジー）を載せた Hosted Agent

`agent_search_hosted/`（第5回デモで使用・**凍結**）をコピーして作った**別エージェント**。
既存エージェントの 2 つのツール（AI Search / Power BI）に **Fabric IQ（オントロジー）** を加え、
「部署をまたいで聞けるようにしても、見せてはいけないものは見せない」ことを
**権限の違う2人で同じ質問をして見比べる**形で実演する。

OBO 登録（Invocations）、Tool Call Limit middleware、`_OBO_CACHE` の設計と経緯は
`agent_search_hosted/README.md` と同一なので、そちらを参照。

`fabric_notebooks/` は **Fabric のノートブック上で実行する PySpark**（オントロジーにバインドする
Delta テーブルの作成）。エージェント本体からは import されず、`.agentignore` でデプロイ対象外。
書き方の決まりは `fabric_notebooks/README.md` を参照。

`tenant_setup/` は **テナント側の準備**（Entra のグループ・ユーザー・アプリ登録と管理者同意、
Fabric のワークスペース・レイクハウス・オントロジー、Foundry のロール）を行うスクリプト。
すべて `--dry-run` 付き・冪等。実行順は `tenant_setup/README.md`。
新テナントで実際に行った操作の記録は [docs/operations_log.md](docs/operations_log.md)。

**別テナントで一式を組み直す場合**は
[docs/new_tenant_setup.html](docs/new_tenant_setup.html)（新テナントでの再構築手順）と
[docs/apply_to_original_tenant.html](docs/apply_to_original_tenant.html)（元テナントへの反映手順）、
記録用テンプレート [docs/tenant_config_record.md](docs/tenant_config_record.md) を参照。

---

## 0. 分離ルール（既存デモを壊さないため）

| 対象 | 既存（凍結） | 新規（このフォルダ） |
|---|---|---|
| フォルダ | `agent_search_hosted/` | `agent_search_iq/` |
| エージェント名 | `agent-search-hosted` | `agent-search-iq` |
| azd 環境 | 現行の環境 | **`azd env new` で別環境を作る** |
| デモUI | `demo_chat.py` / `demo_chat_simple.py` | `demo_chat_iq.py` |

`azd deploy` の前に**必ず `azd env list` で選択中の環境を確認する**。
既存環境に誤ってデプロイすると `_OBO_CACHE` が消え、既存デモの全員が再サインインになる。

> ⚠️ **既存エージェントにも関係する注意**：`agent_search_hosted/requirements.txt` は `>=` 指定で、
> `azure.yaml` が `remote_build` のため、**次に再デプロイした瞬間に未検証の最新版が入る**
> （2026-09-19 時点で agent-framework 1.17.0→1.19.0、foundry-hosting b260903→b260918）。
> 既存エージェントを再デプロイする場合は、先に版を固定すること。
> このフォルダの `requirements.txt` は検証済みの版に固定してある。

---

## 1. 既存からの変更点

| ファイル | 変更 |
|---|---|
| `main.py` | Fabric IQ の Toolbox を `FoundryToolbox` で追加。既存2ツールは**残す**（オントロジー経由と直接照会の答えが一致することを見せるため）。`AGENT_INSTRUCTIONS` を「3ツールの使い分け」と「権限を踏まえた回答方針」に更新。`max_function_calls` 6→8 |
| `azure.yaml` | エージェント名 `agent-search-iq`。`FABRIC_IQ_TOOLBOX_ENDPOINT` を追加。既存で漏れていた `CURRENT_USER_GROUPS` / `QUALITY_TEAM_GROUP_ID` も追加 |
| `requirements.txt` | **版を固定**。`azure-ai-projects==2.6.1` を追加 |
| `setup_toolbox.py` | **新規**。Fabric IQ の Toolbox を作成する一回実行スクリプト |
| `demo_chat_iq.py` | **新規**。デモUI（後述） |
| `.agentignore` | デモUI・`setup_toolbox.py` を除外（既存で漏れていた `demo_chat_simple.py` も） |
| `client_test_responses_session.py` | 接続先 URL を `agent-search-iq` に変更 |

`search_tool.py` `query_fabric.py` `fabric_client.py` `obo.py` は**無変更**。

`common.py` `corpus_data.py` `fabric_schema.py` は、テナント移行のために
**ハードコードの環境変数化**だけを行った（ロジックは無変更）:

- `ALL_EMPLOYEES_GROUP_ID` を新設。`corpus_data.py` の ACL が文字列スラッグ
  `"all-employees"` を直書きしていたのをやめ、Entra の実グループID を使えるようにした。
  旧環境では、この値がスラッグのままだったため **OBO 登録済みユーザーに全社公開文書が
  見えなくなる**（エラーが出ずに0件になる）問題があった。
- `IQ_AGENT_NAME` / `AGENT_DISPLAY_NAME` を新設。`main.py` の `Agent(name=...)` に
  組織名が直書きされていたのと、デモUI・検証スクリプトがエージェント名を
  直書きしていたのをやめた。
- `fabric_schema.py` は**環境変数では吸収できない**（接続先セマンティックモデルの
  実構造そのもの）ため、テナントを移すときは転記し直す必要がある旨を冒頭に明記した。

`FABRIC_IQ_TOOLBOX_ENDPOINT` が未設定のときは、**Fabric IQ 抜きで起動する**（起動ログに警告）。
Azure 側の準備が終わる前でも、コンテナの起動と既存2ツールの疎通を先に確認できるようにするため。
Fabric IQ の代替実装（フォールバック）ではない。

### 1.1 AGENT_INSTRUCTIONS の要点

- 結果が0件・空・認可エラーのとき、**「データが無い」と断定しない**。
  各ツールは質問者本人の権限で動くので、「閲覧権限が無い」可能性がある。
- 横断質問で一部のツールだけ結果が得られなかったら、**得られた範囲で答えたうえで、
  何が欠けているかを必ず明示する**（黙って省略しない）。

---

## 2. 誰の権限で動くか（このデモの核心）

| 経路 | 誰の権限で動くか |
|---|---|
| `search_documents_tool`（AI Search） | Invocations で OBO 登録した人（`_OBO_CACHE[user_id]` のグループで ACL フィルタ） |
| `query_fabric_tool`（Power BI） | 同上（OBO で交換した Power BI トークン → RLS） |
| Fabric IQ（Toolbox） | **Foundry の呼び出し元**。`FoundryToolbox` が `x-agent-foundry-call-id` を転送し、Foundry MCP プロキシがサーバー側で解決する（SDK ソースで確認済み） |

**ここに落とし穴がある。** 既存のデモUIは Foundry を**オペレーターの `az login`**
（`DefaultAzureCredential`）で呼んでいた。このままだと：

- AI Search / Power BI はサインインした人の権限
- **Fabric IQ だけオペレーターの権限**

となり、**経路によって「誰か」がずれる**。権限の違う2人で見比べても Fabric IQ の部分は
2人とも同じ結果になり、デモの主張が成立しない。

そこで `demo_chat_iq.py` は、**サインインした本人のトークンで Foundry を呼ぶ**
（`FOUNDRY_CALL_AS=user`、既定）。こうすると Foundry 上の `user_id` も本人になり、
3経路すべてが同一人物にそろう（副次的に `_OBO_CACHE` のキーも本人になる）。

`FOUNDRY_CALL_AS=operator` にすると従来どおりオペレーターで呼ぶ。Azure 側の準備前の
疎通確認用で、画面に「Fabric IQ は担当者の権限で動いている」旨の警告が出る。

---

## 3. セットアップ（この順で）

### 3.1 最初に確認すること（ここで結論が変わる）

**使うセマンティックモデルのストレージモードを確認する。**
オントロジーは、インポート／Direct Lake／DirectQuery のどれからでも「エンティティ・プロパティ・
リレーションシップの**定義**」は生成できるが、**実データへのバインドは Direct Lake のみ**対応
（Microsoft Learn「Generating an Ontology (Preview) from a Semantic Model」）。
`全拠点売上_全期間` がインポートモードの場合、そこから作ったオントロジーには
**データが紐づかず、Fabric IQ は中身のある回答を返せない**。

その場合は、同じデータをレイクハウスに置いて Direct Lake のセマンティックモデルを作り、
そこからオントロジーを生成する必要がある。

### 3.2 Azure / Fabric 側

1. Fabric 管理ポータルで **「Ontology item (preview)」** テナント設定を有効化（Fabric 管理者）
2. オントロジーを作成 → データバインド → **リフレッシュ**（上流の更新は手動リフレッシュしないと見えない）
3. Fabric IQ 接続用の Entra アプリ登録（Global Administrator 作業）
   - Power BI 委任権限 `Item.Execute.All` / `Item.Read.All` ＋ **管理者同意**
   - クライアントシークレット
   - Foundry 接続のリダイレクト URI
4. Foundry ポータル：Settings > Connections > New connection > **Fabric IQ** → 接続 ID を控える
5. `python setup_toolbox.py`（冒頭 docstring 参照）→ 表示された MCP エンドポイントを控える

### 3.3 `FOUNDRY_CALL_AS=user` のための追加設定

1. サインインに使う Entra アプリ（`OBO_CLIENT_ID`）に、**`https://ai.azure.com` 向けの委任権限**を追加して
   管理者同意。未設定だと `demo_chat_iq.py` のサインイン直後に
   `AADSTS65001`（同意が必要）や `AADSTS650057`（リソース未登録）などで止まる。
   ポータルの「API のアクセス許可 > 所属する組織で使用している API」で該当リソースを探して追加する。
2. デモに使う**各アカウント**に、Foundry プロジェクト上でエージェントを呼び出せるロール
   （Foundry User 相当）を割り当てる。無いと 403。
3. 各アカウントで一度、Fabric IQ 接続の **OAuth 同意**を済ませておく
   （デモUIで最初に質問すると同意リンクが出る → 踏む → もう一度質問）。

### 3.4 デプロイ

```powershell
azd env new agent-search-iq            # 既存と別の環境を作る（初回のみ）
azd env list                            # ★必ず確認
# .env.example を見ながら、既存と同じ値＋Fabric IQ の値を azd env set で登録
azd env set FABRIC_IQ_TOOLBOX_ENDPOINT "<setup_toolbox.py が出力した URL>"
azd deploy
```

---

## 4. デモの手順（2人で見比べる）

1. ブラウザを2つ開く（通常ウィンドウ＋シークレット、または別プロファイル）
2. それぞれで `streamlit run demo_chat_iq.py` の画面を開き、**別のアカウントでサインイン**
   （品質チーム所属／未所属）。**デモ開始前に済ませておく**
3. 各ウィンドウで一度質問し、Fabric IQ の同意リンクが出たら踏んでおく
4. 本番では、同じ質問を左右で送る

画面には毎回、**参照元**・**閲覧できなかった情報**・**実行ユーザー**が出る。

| 質問 | 所属 | 未所属 |
|---|---|---|
| 製品A008の品質基準について教えて | 内容が返る | 「閲覧可能な情報からは回答できません」 |
| 地域別の売上金額を教えて | 同じ結果 | 同じ結果（権限差が無い質問） |
| A008の品質情報と、関連する商品群の売上をまとめて | 3経路を統合 | 売上は返り、**品質文書が欠けていることを明示** |

> 各タブは別々に Streamlit セッション・MSAL キャッシュ・`agent_session_id` を持つ。
> `agent_session_id` ごとに別サンドボックス（＝別プロセス）になる前提で、
> 2人の `_OBO_CACHE` は混ざらない（agent_search_hosted の実機確認結果に基づく）。

---

## 5. このサンドボックスで確認したこと（2026-09-19）

実パッケージ（agent-framework 1.19.0 / agent-framework-foundry-hosting 1.0.0b260918 /
azure-ai-agentserver-core 2.1.0 / azure-ai-agentserver-invocations 1.1.0 / azure-ai-projects 2.6.1）で：

1. `main.py` がダミー環境変数で import でき、`build_agent()` が成功する
2. `FABRIC_IQ_TOOLBOX_ENDPOINT` 未設定時は既存2ツールのみ、設定時は `FoundryToolbox` が
   `agent.mcp_tools` に入る（MCP ツールは実行時に `FunctionTool` として通常の関数呼び出し
   ループへ合流するため、Tool Call Limit middleware も**構造上は効く**）
3. `ResponsesHostServer(agent)` の生成、`mount` / `add_route` の存在
4. `FoundryToolbox` が `x-agent-foundry-call-id` を転送する実装であること、
   `ResponsesHostServer` が `oauth_consent_request` を返す実装であること（SDK ソース）
5. `FabricIQPreviewToolboxTool` と `project.toolboxes.create_version(name=, tools=, description=)` の存在
6. `demo_chat_iq.py` が user / operator 両モードでサインイン画面まで例外なく起動する
   （Streamlit AppTest）。operator モードでは警告が出る
7. 応答解析（`_analyze`）を合成データで確認：3経路取得／文書だけ見えない（部分回答）／
   Fabric IQ 未同意／同じツールを2回呼んで2回目で取得、の4ケースで期待どおりの判定

**実機（Azure）では何も確認していない。** 特に次は実機で見る必要がある。

- Fabric IQ が本当に「Foundry の呼び出し元」＝サインインした本人として動くか
- Fabric IQ の MCP サーバーが公開するツール名と、Responses の `output` 上での表現
  （デモUIは既存2ツール以外をすべて Fabric IQ とみなしている）
- `function_call_output` が Responses の `output` に含まれるか
  （含まれない場合、「閲覧できなかった情報」の表示は出ず、エージェントの回答文での明示だけになる）
- `https://ai.azure.com` 向けの委任権限の具体的な設定（3.3）
- Fabric IQ 経由の応答時間（同期実行でタイムアウトに縛られる）
