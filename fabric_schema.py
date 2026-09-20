"""
Fabricの実セマンティックモデルに存在する、Agentからの参照を許可する
メジャー・列のAllowlist（設計書 第6・11章 Guardrail方針）。

対象は Direct Lake のセマンティックモデル sm_agent_search_iq。
定義は fabric_notebooks/02_create_semantic_model.py（TMSL）が生成しており、
元データは fabric_notebooks/01_create_delta_tables.py が作る Delta テーブル。
**この3つは常に同じ名前を指していなければならない。** どれか1つを直したら
残り2つも合わせること。

  01_create_delta_tables.py  … Spark スキーマ（テーブル名・列名・型）
  02_create_semantic_model.py … TMSL の TABLES / MEASURES / RELATIONSHIPS
  fabric_schema.py（このファイル）… Agent に見せる表示名 → DAX 識別子の対応

【テーブル名・列名が snake_case なのはなぜか】
Fabric Ontology のデータバインドは、名前に空白・ハイフン・日本語が含まれると
バインドできない（エラーは出ず、テーブルが選択肢に現れないだけ）。
同じテーブルをオントロジーとセマンティックモデルの両方から使うため、
物理名は ASCII の snake_case に統一してある。
そのぶん**日本語の表示名はこのAllowlistのキーが担う**。Agent には
MEASURES / COLUMNS のキー（日本語）だけを選ばせ、実際のテーブル名・列名への
変換は query_fabric.py が行う（LLMに生のDAX識別子を書かせない。
設計書 第11章 Guardrail：フィルタ生成の分離）。

【メジャー名を「売上金額」で統一していない理由】
売上のファクトテーブルが商品群別・拠点別の2つあり、両者にリレーションが無い。
「売上金額」という1つの名前にすると、Agentがどちらを指したのか判別できず、
別テーブルの集計軸と組み合わせて空の結果を返す事故になる。
名前自体に集計軸を含めることで、そもそも誤った組み合わせを選べないようにしている。

【モデル構造を確認する方法】
Power BI Desktop の DAX Query View で
  EVALUATE INFO.VIEW.MEASURES()
  EVALUATE INFO.VIEW.COLUMNS()
（ExecuteQueries REST API では INFO 系関数が非対応のため、Desktop/Service の
UI 上で確認した値をここへ手動反映する運用）。
API から見る場合は semanticModels の getDefinition で TMDL を取得できる。

【テナントを移すときの注意】
このファイルの内容は**接続先のセマンティックモデルの実構造そのもの**であり、
環境変数では吸収できない（テーブル名・列名・メジャー名が一致しなければDAXが通らない）。
ただし上の3ファイルが揃っていれば、別テナントでも
01 → 02 を順に実行するだけで同じ構造のモデルが再現でき、このファイルは無変更で済む。
FABRIC_WORKSPACE_ID / FABRIC_DATASET_ID の差し替えは別途必要。
"""

MEASURES = {
    # 売上テーブルが商品群別と拠点別の2つあるため、「売上金額」という1つの名前にすると
    # どちらを集計すべきか曖昧になる。意図的に名前を分けてある
    # （Agentが質問文から選べるよう、名前自体に集計軸を含めている）。
    "商品群売上金額": {"table": "product_group_sales_monthly", "name": "商品群売上金額"},
    "商品群粗利": {"table": "product_group_sales_monthly", "name": "商品群粗利"},
    "拠点売上金額": {"table": "site_sales_monthly", "name": "拠点売上金額"},
    "拠点粗利": {"table": "site_sales_monthly", "name": "拠点粗利"},
}

COLUMNS = {
    # --- 商品群（product_group） ---
    "商品群コード": {"table": "product_group", "name": "product_group_code"},
    "商品群名称": {"table": "product_group", "name": "name"},  # 樹脂部品・金属部品・電子部品・機構部品
    # --- 拠点（site） ---
    "拠点コード": {"table": "site", "name": "site_code"},
    "拠点名称": {"table": "site", "name": "name"},
    "地域名称": {"table": "site", "name": "region_name"},  # 日本・アジア・欧州
    # --- 製品（product） ---
    "製品ID": {"table": "product", "name": "product_id"},  # A001等。AI Search側の文書と共通のキー
    "製品名称": {"table": "product", "name": "name"},
    # --- 時系列の集計軸 ---
    # このモデルには日付ディメンションテーブルが無いため、ファクト側の period_start を
    # 直接集計軸に使う。売上テーブルが2つあるので、どちらの月次かを名前で区別する
    # （別テーブルの列なので、片方のメジャーともう片方の月を組み合わせると
    #  リレーションが無く解決できない。名前を分けてその誤用を防いでいる）。
    "商品群売上の年月": {"table": "product_group_sales_monthly", "name": "period_start"},
    "拠点売上の年月": {"table": "site_sales_monthly", "name": "period_start"},
}

# 意図的にAllowlistへ載せていないもの：
#  - quality_document_public / quality_document_restricted の各列
#    文書の中身は search_documents_tool（AI Search）と Fabric IQ（オントロジー）が扱う。
#    DAX経路から本文を引けるようにすると、同じ情報に3つの経路ができて
#    「どの権限で取得した結果か」が曖昧になる。
#  - edge_* テーブルの列
#    リレーションをたどるのは Fabric IQ（オントロジー）の役割。DAX側は
#    SUMMARIZECOLUMNS がモデルのリレーションを自動で辿るため、明示的に指定させる必要がない。
#  - quality_document_mixed_probe
#    権限の挙動を確かめるための検証用テーブル。セマンティックモデルにも含めていない。
