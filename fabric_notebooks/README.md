# fabric_notebooks

**Fabric のリソースを作るコード。**
エージェント本体（`main.py`）からは一切 import されず、デプロイパッケージにも含まれない
（`.agentignore` でディレクトリごと除外している）。

## ファイル

番号は実行順。増やす場合も順番が分かる名前にする。
**実行場所がファイルによって違う**ので注意。

| ファイル | 役割 | 実行場所 |
|---|---|---|
| `00_check_status.py` | **作業再開時に最初に実行する。** 構築状況を点検し、次の手順と現在の環境変数を表示する（読み取りのみ） | **手元の PC** |
| `01_create_delta_tables.py` | オントロジーにバインドする Delta マネージドテーブルを作る。**`TARGET` を変えて2回実行**（lh_public / lh_restricted） | **Fabric のノートブック**（pyspark。手元では動かない） |
| `02_create_semantic_model.py` | Direct Lake のセマンティックモデルを作る | **手元の PC**（`az login` 済みで REST API を叩く） |
| `03_create_ontology_definition.py` | オントロジーの定義（エンティティ型・バインド・リレーション型）を流し込む | **手元の PC** |

### 01（Fabric ノートブック）

1. Fabric で対象のワークスペースを開く
2. `新規 > ノートブック` を作成
3. **左ペインからレイクハウスをアタッチする**（これを忘れると `saveAsTable` の
   書き込み先が定まらず失敗する）
4. ファイルの中身をそのまま貼り付けて上から実行

`# %%` / `# %% [markdown]` はセルの区切りを示すコメント。そのまま貼っても動くし、
セルに分けてもよい。

```powershell
Get-Content -Raw -Encoding utf8 .\fabric_notebooks\01_create_delta_tables.py | Set-Clipboard
```

### 02（手元の PC）

対象は環境変数で渡す（実値をコードに埋め込まないため）。
`--dry-run` を付けると POST せずに生成した TMSL を表示するだけ。

```powershell
$env:FABRIC_WORKSPACE_ID    = "<ワークスペースID>"
$env:FABRIC_SQL_ENDPOINT    = "<...datawarehouse.fabric.microsoft.com>"
$env:FABRIC_SQL_DATABASE_ID = "<SQLエンドポイントのID>"
python fabric_notebooks/02_create_semantic_model.py --dry-run
python fabric_notebooks/02_create_semantic_model.py
```

ポータルの「新しいセマンティックモデル」でも同じものは作れる。
スクリプトにしてあるのは、**モデル定義がコードとして残り、別テナントで同じものを
再現できる**ため。ポータルだと手作業の再現になり、メジャー定義の写し間違いが起きる。

### 03（手元の PC）

オントロジーのアイテムを作ってから実行する。

```powershell
$env:FABRIC_WORKSPACE_ID = "<ワークスペースID>"
$env:FABRIC_LAKEHOUSE_ID = "<バインド元のレイクハウスID>"
$env:FABRIC_ONTOLOGY_ID  = "<オントロジーのアイテムID>"
python fabric_notebooks/03_create_ontology_definition.py --dry-run
python fabric_notebooks/03_create_ontology_definition.py
```

> **API の癖その1**：`updateDefinition` に**既存と同じ名前・別のid**を送ると
> `ALMOperationImportFailed: Duplicate Name-Namespace combinations` で拒否される。
> **id は一意ならよいが、名前は namespace 内で一意**という非対称な制約。
> スクリプトは実行前に `getDefinition` で既存の名前→idを読み取って引き継ぐ。
>
> **API の癖その2**：リレーションのバインドだけ `DataBindings` ではなく
> **`Contextualizations`** というフォルダ名で、中の形も違う
> （`sourceKeyRefBindings` / `targetKeyRefBindings`）。

> **API の癖その3**：`semanticModels` の作成は **TMSL（`model.bim`）しか受け付けない**。
> TMDL（`definition/*.tmdl`）で投げると
> `Workload_FailedToParseFile: Missing required artifact 'model.bim'` で失敗する。
> ところが `getDefinition` で読み出すと **TMDL に変換されて返ってくる**。
> 入力と出力で形式が違うので、読み出した内容をそのまま投げ直すことはできない。

## 名前は3ファイルで一致させる

テーブル名・列名・メジャー名は次の3つが常に同じものを指していなければならない。
どれか1つを直したら、残り2つも必ず合わせること。

| ファイル | 持っているもの |
|---|---|
| `01_create_delta_tables.py` | Spark スキーマ（テーブル名・列名・型） |
| `02_create_semantic_model.py` | TMSL の `TABLES` / `MEASURES` / `RELATIONSHIPS` |
| `../fabric_schema.py` | Agent に見せる日本語の表示名 → DAX 識別子の対応 |

## なぜ書き方に制約があるか

Fabric Ontology のデータバインドは、**制約を破ってもエラーを返さない**。
「バインドはできたのに値が全部 null」「テーブルが選択肢に出てこない」という形でしか
現れないため、原因の切り分けに時間がかかる。

そこで各ノートブックは次を守って書く。詳細は `docs/new_tenant_setup.html` の 4-1 を参照。

- 金額など数値は `DoubleType`（`DecimalType` はバインド後に null になる）
- テーブル名・列名は ASCII の snake_case（日本語の表示名は「列の値」として持たせる）
- `delta.columnMapping.mode` を設定しない
- `saveAsTable` でマネージドテーブルとして作る（`path` 指定の外部テーブルは不可）
- エンティティ間の同名プロパティ（`name` / `status` など）は型をそろえる
- エッジ（リレーション）テーブルには一意の行キーを持たせる
- レイクハウスの OneLake セキュリティは有効にしない

`01_create_delta_tables.py` の最後のセルに、これらを自動チェックする検証コードが入っている。
NG があればそこで止まるので、**オントロジーのバインドに進む前に必ず OK を確認すること。**

## 閲覧範囲の分離

このデモの主題は「権限の違う2人に違う結果を返す」こと。
レイクハウス直バインドのオントロジーで行レベルの制御が効くかは未確認のため、
**閲覧範囲ごとにテーブルを分けてある**。

| テーブル | 閲覧範囲 | 権限の付与先 |
|---|---|---|
| `quality_document_public` | 全社公開 | 全社員グループ |
| `quality_document_restricted` | 品質チーム限定 | 品質チームグループのみ |
| `quality_document_mixed_probe` | 公開＋限定（混合） | **検証用。本番のオントロジーにバインドしない** |

混合テーブルは、行レベルの制御が効くかを実機で判定するためだけのもの。
判定が終わったら本番のオントロジーからは必ず外す。付けっぱなしにすると、
テーブルを分けた意味がなくなる。
