"""
Fabric ノートブックに貼り付けて実行する PySpark。オントロジーにバインドする
Delta マネージドテーブルを作る。

★ このノートブックは **2回実行する**。1回目は lh_public、2回目は lh_restricted。
   先頭の TARGET を切り替え、対応するレイクハウスをアタッチしてから実行すること。

【なぜレイクハウスを2つに分けるのか（実機で決着した設計）】
  レイクハウスのアイテム権限は **全か無か** で、テーブル単位の制御は効かない。
  実機検証の結果：
    - オントロジーへの読み取りだけ → 全エンティティが 401（スキーマは見えるがデータは引けない）
    - バインド元レイクハウスへの読み取りを追加 → **そのレイクハウス上の全エンティティが引ける**
  つまり1つのレイクハウスに公開データと限定データを同居させると、
  読み取りを与えた瞬間に限定データまで見えてしまう。
  OneLake セキュリティなら行・列制御ができるが、**有効にするとオントロジーの
  バインド対象から外れる**ため使えない。

  エンティティのデータ問い合わせは **バインド元のレイクハウス単位で個別に認可される**ので、
  レイクハウスを分ければエンティティ単位の分離が成立する。オントロジーは1つのままでよい。

      lh_public      … 全社公開          → 全社員グループに読み取り
      lh_restricted  … 品質チーム限定    → 品質チームグループのみに読み取り
              ↓ どちらも同じオントロジーにバインドする
      ont_agent_search_iq

  ※ マネージドテーブルは saveAsTable でアタッチ中のレイクハウスにしか作れない
    （path 指定で別レイクハウスに書くと外部テーブル扱いになりバインド不可）。
    そのため「1ノートブックで両方に書く」ことはできず、2回実行する形にしている。

【Fabric Ontology のバインド制約】
  制約を破っても**エラーは出ず**、「値が全部 null」「テーブルが一覧に出ない」という形で出る。
    - 金額など数値は DoubleType。**オントロジーの型システムに Decimal が無い**
      （valueType は String / Boolean / DateTime / Object / BigInt / Double の6つだけ）
    - テーブル名・列名は ASCII の snake_case。空白・ハイフン・日本語・GQL予約語を避ける
      （日本語の表示名は「列の値」として持たせる。値は日本語で構わない）
    - delta.columnMapping.mode を設定しない
    - saveAsTable でマネージドテーブルとして作る
    - エンティティ間の同名プロパティ（name / status など）は型をそろえる
    - エッジテーブルには一意の行キー（edge_id）を持たせる
      ※ リレーションのバインド画面が要求するのは両端のキー列2つだけだったが、
        公式ドキュメントの記述に従い、重複検出にも有用なので付けておく
  レイクハウス側の条件：外部テーブルでないこと／OneLake セキュリティが無効であること／
  「受信パブリックアクセス」が有効なワークスペースにあること。

【SCD Type 2 について】
  このデータセットでは履歴を持たせていないため SCD2 は使っていない。
  導入する場合は、静的バインド用に「業務キーで一意な現在行テーブル」を別に作り
  （is_current = true の行だけ）、履歴は *_monthly と同じ列形式
  （エンティティキー＋タイムスタンプ＋値）で時系列バインドに回すこと。
  静的バインドは1エンティティ型につき1つだけ、時系列バインドは複数可。
"""

# %% [markdown]
# ## 0. 実行対象の切り替え
#
# **ここを書き換えてから実行する。** アタッチしているレイクハウスと一致させること。

# %%
TARGET = "public"        # "public" もしくは "restricted"

# %%
import re

from pyspark.sql import SparkSession
from pyspark.sql.types import (
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)
from datetime import datetime

spark = SparkSession.builder.getOrCreate()

if TARGET not in ("public", "restricted"):
    raise ValueError(f"TARGET は 'public' か 'restricted'。指定値: {TARGET!r}")

# --- アタッチ先と TARGET の食い違いを検出する ---------------------------------
# TARGET を変え忘れたまま別のレイクハウスで実行すると、限定側に公開テーブルが
# 書かれる（あるいはその逆で**限定データが公開レイクハウスに漏れる**）。
# エラーにならず、テーブル名も同じなので気づきにくい。実際に一度やらかしたので
# ガードを入れてある。
_EXPECTED_LAKEHOUSE = {"public": "lh_public", "restricted": "lh_restricted"}
try:
    _attached = spark.conf.get("trident.lakehouse.name")
except Exception:
    _attached = None

if _attached:
    _want = _EXPECTED_LAKEHOUSE[TARGET]
    if _attached != _want:
        raise SystemExit(
            f"アタッチされているレイクハウス({_attached})と TARGET({TARGET}) が一致しません。\n"
            f"  TARGET='{TARGET}' なら '{_want}' をアタッチすること。\n"
            f"  TARGET を変え忘れていないか、アタッチ先が正しいかを確認してから再実行する。"
        )
    print(f"  アタッチ先: {_attached}  /  TARGET: {TARGET}  → 一致")
else:
    print("  ※ アタッチ先のレイクハウス名を取得できませんでした。")
    print(f"     TARGET={TARGET} に対応するレイクハウスをアタッチしているか、目視で確認してください。")

WRITTEN: list[str] = []


def write_delta(df, table_name: str, comment: str) -> None:
    """Delta マネージドテーブルとして保存する。

    delta.columnMapping.mode は**意図的に設定しない**。設定するとオントロジーの
    バインド対象から外れる（エラーは出ず、テーブルが一覧に現れないだけ）。
    """
    (
        df.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(table_name)
    )
    spark.sql(f"COMMENT ON TABLE {table_name} IS '{comment.replace(chr(39), chr(39)*2)}'")
    WRITTEN.append(table_name)
    print(f"  written: {table_name:34s} rows={df.count():4d}")


def ts(year: int, month: int) -> datetime:
    return datetime(year, month, 1)


# ---------------------------------------------------------------------------
# 共通のスキーマ定義。name / status は全エンティティで StringType にそろえる
# （エンティティ間の同名プロパティは同じ型でなければバインドできない）。
# ---------------------------------------------------------------------------
quality_document_schema = StructType([
    StructField("document_id", StringType(), False),
    StructField("product_id", StringType(), True),
    StructField("name", StringType(), True),
    StructField("status", StringType(), True),
    StructField("summary", StringType(), True),
    StructField("visibility", StringType(), True),
])

edge_document_schema = StructType([
    StructField("edge_id", StringType(), False),
    StructField("document_id", StringType(), True),
    StructField("product_id", StringType(), True),
])

# product_id は AI Search 側の corpus_data.py と同じ値を使う。
# これが「文書」と「売上」を結ぶ共通キーになる。
PRODUCT_IDS = ["A001", "A003", "A005", "A007", "A008"]


# %% [markdown]
# ## 1. lh_public 側のテーブル
#
# 全社公開。全社員グループに読み取りを与える。

# %%
if TARGET == "public":
    # --- product ---------------------------------------------------------
    product_schema = StructType([
        StructField("product_id", StringType(), False),
        StructField("name", StringType(), True),
        StructField("status", StringType(), True),
        StructField("product_group_code", StringType(), True),
        StructField("unit_price", DoubleType(), True),   # ← Decimal ではなく Double
    ])
    product_rows = [
        ("A001", "バルブユニットX",     "active", "PG05", 48000.0),
        ("A003", "センサーモジュールZ", "active", "PG03", 125000.0),
        ("A005", "熱交換器β",           "active", "PG02", 980000.0),
        ("A007", "制御盤δ",             "active", "PG03", 640000.0),
        ("A008", "フィルターユニットε", "active", "PG01", 210000.0),
    ]
    write_delta(spark.createDataFrame(product_rows, product_schema), "product",
                "製品マスタ。product_id は AI Search 側の文書と共通のキー")

    # --- product_group ---------------------------------------------------
    product_group_schema = StructType([
        StructField("product_group_code", StringType(), False),
        StructField("name", StringType(), True),
        StructField("status", StringType(), True),
    ])
    write_delta(spark.createDataFrame([
        ("PG01", "樹脂部品", "active"),
        ("PG02", "金属部品", "active"),
        ("PG03", "電子部品", "active"),
        ("PG05", "機構部品", "active"),
    ], product_group_schema), "product_group",
        "商品群マスタ。売上の集計軸であり、文書と売上を繋ぐ経路でもある")

    # --- site ------------------------------------------------------------
    site_schema = StructType([
        StructField("site_code", StringType(), False),
        StructField("name", StringType(), True),
        StructField("status", StringType(), True),
        StructField("region_name", StringType(), True),
    ])
    write_delta(spark.createDataFrame([
        ("S01", "千葉工場",         "active", "日本"),
        ("S02", "シンガポール工場", "active", "アジア"),
        ("S03", "ロッテルダム工場", "active", "欧州"),
    ], site_schema), "site", "拠点マスタ。地域別売上の集計軸")

    # --- quality_document_public -----------------------------------------
    public_docs = [
        ("QD-A001", "A001", "バルブユニットX 品質基準", "published",
         "原料純度99.5%以上、耐熱120度、外観検査で傷・変色・異物混入なきこと", "public"),
        ("QD-A003", "A003", "センサーモジュールZ 品質基準", "published",
         "測定精度±0.5%以内、応答速度100ms以内、防塵防水IP65相当", "public"),
        ("QD-A005", "A005", "熱交換器β 品質基準", "published",
         "設計伝熱量の95%以上を確保、設計圧力1.5倍で30分保持し漏れなきこと", "public"),
    ]
    write_delta(spark.createDataFrame(public_docs, quality_document_schema),
                "quality_document_public",
                "品質文書（全社公開）。全社員グループに読み取りを付与する")

    # --- エッジ ------------------------------------------------------------
    write_delta(spark.createDataFrame(
        [(f"E-PG-{p[0]}", p[0], p[3]) for p in product_rows],
        StructType([
            StructField("edge_id", StringType(), False),
            StructField("product_id", StringType(), True),
            StructField("product_group_code", StringType(), True),
        ])), "edge_product_product_group", "製品 → 商品群")

    product_site_pairs = [("A001", "S01"), ("A003", "S02"), ("A005", "S03"),
                          ("A007", "S02"), ("A008", "S01")]
    write_delta(spark.createDataFrame(
        [(f"E-PS-{a}-{b}", a, b) for a, b in product_site_pairs],
        StructType([
            StructField("edge_id", StringType(), False),
            StructField("product_id", StringType(), True),
            StructField("site_code", StringType(), True),
        ])), "edge_product_site", "製品 → 生産拠点")

    # 公開文書のエッジだけをこちらに置く。限定文書のエッジを混ぜると、
    # 中身は見えなくても「限定文書が存在すること」とそのIDが公開側から見えてしまう。
    write_delta(spark.createDataFrame(
        [(f"E-DP-{d[0]}", d[0], d[1]) for d in public_docs], edge_document_schema),
        "edge_document_public_product", "公開の品質文書 → 製品")

    # --- 時系列 ------------------------------------------------------------
    sales_schema_group = StructType([
        StructField("product_group_code", StringType(), False),
        StructField("period_start", TimestampType(), False),
        StructField("sales_amount", DoubleType(), True),
        StructField("gross_profit", DoubleType(), True),
    ])
    rows = []
    for code, base in {"PG01": 42_000_000.0, "PG02": 88_000_000.0,
                       "PG03": 61_000_000.0, "PG05": 35_000_000.0}.items():
        for m in range(1, 13):
            amt = base * (1.0 + 0.04 * ((m % 5) - 2))
            rows.append((code, ts(2026, m), round(amt, 2), round(amt * 0.27, 2)))
    write_delta(spark.createDataFrame(rows, sales_schema_group),
                "product_group_sales_monthly",
                "商品群別の月次売上。時系列バインド用")

    sales_schema_site = StructType([
        StructField("site_code", StringType(), False),
        StructField("period_start", TimestampType(), False),
        StructField("sales_amount", DoubleType(), True),
        StructField("gross_profit", DoubleType(), True),
    ])
    rows = []
    for code, base in {"S01": 96_000_000.0, "S02": 74_000_000.0, "S03": 56_000_000.0}.items():
        for m in range(1, 13):
            amt = base * (1.0 + 0.03 * ((m % 4) - 1))
            rows.append((code, ts(2026, m), round(amt, 2), round(amt * 0.25, 2)))
    write_delta(spark.createDataFrame(rows, sales_schema_site),
                "site_sales_monthly", "拠点別の月次売上。時系列バインド用")


# %% [markdown]
# ## 2. lh_restricted 側のテーブル
#
# 品質チーム限定。**品質チームグループだけ**に読み取りを与える。

# %%
if TARGET == "restricted":
    restricted_docs = [
        ("QD-A007", "A007", "制御盤δ 品質基準および逸脱記録", "published",
         "絶縁抵抗1MΩ以上。特定ロットで基準値をわずかに下回る事象が1件発生、"
         "端子部の防湿処理不備が原因と特定し作業手順を改訂済み", "quality_team_only"),
        ("QD-A008", "A008", "フィルターユニットε 品質基準", "published",
         "公称ろ過精度5μm以下、定格流量時の初期圧力損失0.05MPa以下", "quality_team_only"),
    ]
    write_delta(spark.createDataFrame(restricted_docs, quality_document_schema),
                "quality_document_restricted",
                "品質文書（品質チーム限定）。品質チームグループにだけ読み取りを付与する")

    write_delta(spark.createDataFrame(
        [(f"E-DP-{d[0]}", d[0], d[1]) for d in restricted_docs], edge_document_schema),
        "edge_document_restricted_product", "限定の品質文書 → 製品")


# %% [markdown]
# ## 3. 検証 — バインド制約を満たしているか自動チェック
#
# ここで NG が出たら、オントロジーのバインドに進んでも「値が全部 null」になる。

# %%
print("\n" + "=" * 72)
print(f"バインド制約チェック（TARGET={TARGET}）")
print("=" * 72)

ALLOWED_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
problems: list[str] = []

for table in WRITTEN:
    detail = spark.sql(f"DESCRIBE DETAIL {table}").collect()[0]
    if (detail["format"] or "").lower() != "delta":
        problems.append(f"{table}: Delta 形式ではない ({detail['format']})")

    props = {r["key"]: r["value"] for r in spark.sql(f"SHOW TBLPROPERTIES {table}").collect()}
    if props.get("delta.columnMapping.mode", "none") != "none":
        problems.append(f"{table}: delta.columnMapping.mode が有効 → バインド不可")

    if not ALLOWED_NAME.match(table):
        problems.append(f"{table}: テーブル名が snake_case ではない")
    for field in spark.table(table).schema.fields:
        if not ALLOWED_NAME.match(field.name):
            problems.append(f"{table}.{field.name}: 列名が snake_case ではない")
        if field.dataType.typeName() == "decimal":
            problems.append(f"{table}.{field.name}: Decimal はオントロジーに対応する型が無い")

# 同名プロパティの型がそろっているか（このレイクハウス内で判定）
seen: dict[str, tuple[str, str]] = {}
for table in WRITTEN:
    for field in spark.table(table).schema.fields:
        t = field.dataType.typeName()
        if field.name in seen and seen[field.name][0] != t:
            problems.append(
                f"同名プロパティの型不一致: {field.name} は "
                f"{seen[field.name][1]} で {seen[field.name][0]}、{table} で {t}"
            )
        seen.setdefault(field.name, (t, table))

if problems:
    print("\n".join("  NG  " + p for p in problems))
    raise SystemExit("バインド制約を満たしていないテーブルがある。上の NG を直すこと")

print(f"  OK  {len(WRITTEN)} 件、制約違反なし")
for t in WRITTEN:
    print(f"    - {t}")

if TARGET == "public":
    print("\n  次: TARGET='restricted' に変えて、lh_restricted をアタッチしたノートブックで実行する")
else:
    print("\n  次: 手元の PC で 02 → 03 を実行し、モデルとオントロジーを作る")
print("\n  ※ レイクハウスの共有では「すべての SQL エンドポイント データを読み取る」と")
print("     「すべての Apache Spark を読み取り…」にチェックが必要。既定の共有だけでは 401 になる。")
