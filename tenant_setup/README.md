# tenant_setup

**テナント側の準備（Entra ID・Fabric のアイテム・Foundry のロール）を行うスクリプト。**
新テナントで手作業／ワンショットのコマンドで行った操作を、元テナントでもそのまま
（あるいは管理者に渡して）再実行できる形にしたもの。

エージェント本体からは import されず、`.agentignore` でデプロイ対象から外してある。
すべて手元の PC で実行する（`az login` 済みであること）。

## 共通の約束

- **全スクリプトに `--dry-run` がある。**何を作るか／既にあるかを表示するだけで、何も変更しない。
  管理者に依頼するときは、まず `--dry-run` の結果を見てもらうと話が早い。
- **冪等。**同名のものがあれば作らずにスキップする。途中で失敗しても、直して再実行すればよい。
- **実値をファイルに書かない。**ID は実行結果として画面に出す。パスワードとシークレットは
  **リポジトリ外**の `~/.agent_search_iq/secrets/secrets.txt` に追記し、画面には出さない
  （保存先は環境変数 `AGENT_SEARCH_IQ_SECRETS_DIR` で変えられる）。
- **シェルを介さずに REST を直接叩く**（`_http.py`）。Windows の `cmd` が URL 中の `&` を
  区切りと解釈する問題や、Git Bash が `/subscriptions/...` を Windows パスに変換する問題を避けるため。

## 実行順

`fabric_notebooks/` と交互に進む。

| # | スクリプト | 作るもの | 必要な権限 |
|---|---|---|---|
| 1 | `infra/main.bicep` | Foundry・AI Search・モデル・Fabric 容量（`infra/README.md`） | サブスクリプションの共同作成者以上 |
| 2 | `tenant_setup/10_entra_groups_users.py` | セキュリティグループ2つ、テストユーザー2人、ライセンス | ユーザー管理者＋グループ管理者 |
| 3 | `tenant_setup/20_entra_app_registration.py` | OBO 用アプリ登録、委任権限、**管理者同意**、シークレット | アプリケーション管理者＋同意できる管理者 |
| 4 | `tenant_setup/30_fabric_items.py` | ワークスペース（容量へ割り当て）、`lh_public` / `lh_restricted`、オントロジー | Fabric 容量の共同作成者以上 |
| 5 | `fabric_notebooks/01_create_delta_tables.py` | Delta テーブル（Fabric ノートブックで2回） | ワークスペースの共同作成者 |
| 6 | `fabric_notebooks/02_create_semantic_model.py` | Direct Lake のセマンティックモデル | 同上 |
| 7 | `fabric_notebooks/03_create_ontology_definition.py` | オントロジーの定義とバインド | 同上 |
| 8 | **ポータル** | レイクハウスとオントロジーへの読み取り付与（API 非対応） | アイテムの管理者 |
| 9 | `tenant_setup/40_foundry_rbac.py` | Foundry のロール割り当て | プロジェクトの所有者／ユーザーアクセス管理者 |
| 10 | **ポータル** | Foundry の **ビルド > ツール** で Fabric IQ ツールを作成 | Foundry Project Manager |
| 11 | `setup_toolbox.py` | Toolbox | 同上 |
| 12 | `ingest_sample_docs.py` | AI Search のインデックス作成と文書投入（グループIDが GUID でなければ拒否） | Search の管理キー |
| 13 | `azd deploy` | エージェント本体（azd 環境に `AZURE_AI_PROJECT_ID` と `FOUNDRY_PROJECT_ENDPOINT` も必要） | Foundry Project Manager |

いつでも `fabric_notebooks/00_check_status.py` で現状を点検できる。

## 元テナントで使うときの違い

| スクリプト | 元テナントでの使い方 |
|---|---|
| `10` | 品質チームは既存グループを流用。`--groups grp-all-employees --no-users` で全社員グループだけ作る |
| `20` | **既存デモのアプリには触らない。**`OBO_APP_NAME` を既存と別名にして新規作成。管理者にスクリプトごと渡して実行してもらうのが早い。同意を付与できなかった場合は管理者の同意 URL が表示される |
| `30` | **新しいワークスペース名で作る**（`FABRIC_WORKSPACE_NAME`）。既存ワークスペースには作らない |
| `40` | 管理者に実行してもらう。付与先はグループ（`AGENT_USERS_GROUP_ID`） |

## 検証状況（新テナント、2026-09-21）

| スクリプト | 既存に対する再実行（冪等） | 新規作成の経路 |
|---|---|---|
| `10` | 確認済み（何も変更しない） | 同じ API 呼び出しを手作業で実行して成功。**スクリプトとしての新規作成は未実行** |
| `20` | 確認済み | **確認済み**（使い捨ての名前で作成→同意の付与を確認→削除） |
| `30` | 確認済み | **確認済み**（使い捨てのワークスペースで作成→削除） |
| `40` | 確認済み | 同じ割り当てを手作業（`az role assignment create`）で実行して成功。**スクリプトとしての新規作成は未実行** |
