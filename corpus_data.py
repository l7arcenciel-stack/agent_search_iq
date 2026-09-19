"""
実データ規模検証用の合成コーパスのマスタデータ。
架空の製品10件について、品質報告書・取扱説明書・仕様書を
複数フォーマット（PDF/Word/PowerPoint/テキスト/Excel）で生成するための元データ。

A001/A002は、これまでの疎通確認で使っていたquality_report_a.txt / b.txtの
内容を踏襲している（既存の動作確認クエリ「製品Aの品質基準は？」がそのまま通ることを確認済み）。
"""
from common import QUALITY_TEAM_GROUP_ID

PRODUCTS = [
    {
        "id": "A001",
        "name": "バルブユニットX",
        "domain": "化学プラント部品",
        "quality_format": "pdf",
        "manual_format": "pptx",
        "checks": [
            ("原料純度", "99.5%以上を合格ラインとする"),
            ("耐熱温度", "120度まで変形が生じないことを確認する"),
            ("外観検査", "傷・変色・異物混入がないことを目視および画像検査で確認する"),
        ],
        "issue": "2026年度第2四半期の検査結果で、一部ロットの原料純度が99.2%まで低下する事象が確認された。"
                 "原因は原料供給元の配合工程における混合時間の短縮と推定。再発防止として供給元との合同レビュー、"
                 "受入検査の抜き取り率引き上げを実施予定。",
        "spec": [("定格圧力", "1.6", "MPa"), ("動作温度範囲", "-10〜120", "℃"),
                 ("材質", "ステンレス鋼(SUS316)", "-"), ("重量", "4.2", "kg"),
                 ("型番", "VLV-X-100", "-")],
    },
    {
        "id": "A002",
        "name": "ポンプアッセンブリY",
        "domain": "化学プラント部品",
        "quality_format": "docx",
        "manual_format": "pptx",
        "checks": [
            ("寸法公差", "設計値に対して±0.3mm以内とする"),
            ("耐荷重試験", "定格荷重の1.5倍まで破損しないことを確認する"),
        ],
        "issue": "直近の検査では基準からの逸脱は確認されておらず、品質は安定して推移している。",
        "spec": [("定格流量", "120", "L/min"), ("動作温度範囲", "-5〜90", "℃"),
                 ("材質", "鋳鉄+ステンレス", "-"), ("重量", "18.5", "kg"),
                 ("型番", "PMP-Y-220", "-")],
    },
    {
        "id": "A003",
        "name": "センサーモジュールZ",
        "domain": "計測機器",
        "quality_format": "txt",
        "manual_format": "pptx",
        "checks": [
            ("測定精度", "フルスケールに対し±0.5%以内とする"),
            ("応答速度", "100ms以内に測定値が安定することを確認する"),
            ("防塵防水性能", "IP65相当であることを確認する"),
        ],
        "issue": "2026年度第1四半期に、高湿度環境下で応答速度が150ms程度まで低下する事象が数件報告された。"
                 "内部基板のコーティング材の経年劣化が要因の一つとして疑われており、供給元と原因調査中。",
        "spec": [("測定範囲", "0〜10", "MPa"), ("動作温度範囲", "-20〜85", "℃"),
                 ("防塵防水等級", "IP65", "-"), ("重量", "0.3", "kg"),
                 ("型番", "SNS-Z-050", "-")],
    },
    {
        "id": "A004",
        "name": "触媒コンテナα",
        "domain": "触媒関連",
        "quality_format": "pdf",
        "manual_format": "docx",
        "checks": [
            ("充填密度", "設計値に対し±2%以内とする"),
            ("気密性試験", "0.3MPa加圧下でのリーク量が規定値以下であることを確認する"),
        ],
        "issue": "直近半年間、気密性試験の不合格率は0.1%未満で安定しており、特段の傾向変化は見られない。",
        "spec": [("容量", "500", "L"), ("最大許容圧力", "0.5", "MPa"),
                 ("材質", "炭素鋼(内面ライニング)", "-"), ("重量(空)", "85", "kg"),
                 ("型番", "CAT-A-500", "-")],
    },
    {
        "id": "A005",
        "name": "熱交換器β",
        "domain": "熱交換設備",
        "quality_format": "docx",
        "manual_format": "pptx",
        "checks": [
            ("伝熱性能", "定格条件下で設計伝熱量の95%以上を確保する"),
            ("耐圧試験", "設計圧力の1.5倍で30分間保持し漏れがないことを確認する"),
        ],
        "issue": "2026年度第3四半期に、一部設置環境で伝熱性能が設計値の90%程度まで低下する事象が報告された。"
                 "配管内のスケール付着が主因と推定され、定期洗浄サイクルの見直しを検討中。",
        "spec": [("伝熱面積", "45", "m2"), ("最大許容圧力", "1.0", "MPa"),
                 ("材質", "チタン", "-"), ("重量", "620", "kg"),
                 ("型番", "HEX-B-450", "-")],
    },
    {
        "id": "A006",
        "name": "配管継手γ",
        "domain": "配管部材",
        "quality_format": "txt",
        "manual_format": "docx",
        "checks": [
            ("耐圧性能", "定格圧力の2倍まで破損しないことを確認する"),
            ("シール性能", "規定トルクでの締結後、漏れがないことを確認する"),
        ],
        "issue": "品質基準からの逸脱事例は報告されていない。",
        "spec": [("呼び径", "50", "A"), ("最大許容圧力", "2.0", "MPa"),
                 ("材質", "ステンレス鋼(SUS304)", "-"), ("重量", "1.1", "kg"),
                 ("型番", "JNT-C-050", "-")],
    },
    {
        "id": "A007",
        "name": "制御盤δ",
        "domain": "電気設備",
        "quality_format": "pdf",
        "manual_format": "docx",
        "checks": [
            ("絶縁抵抗", "1MΩ以上であることを確認する"),
            ("動作試験", "全制御シーケンスが設計通りに動作することを確認する"),
        ],
        "issue": "2026年度上期に、特定ロットで絶縁抵抗が基準値をわずかに下回る事象が1件発生。"
                 "端子部の防湿処理の不備が原因と特定され、当該工程の作業手順を改訂済み。",
        "spec": [("定格電圧", "AC200", "V"), ("動作温度範囲", "0〜40", "℃"),
                 ("防塵防水等級", "IP54", "-"), ("重量", "35", "kg"),
                 ("型番", "CTL-D-200", "-")],
    },
    {
        "id": "A008",
        "name": "フィルターユニットε",
        "domain": "ろ過設備",
        "quality_format": "docx",
        "manual_format": "docx",
        "checks": [
            ("ろ過精度", "公称ろ過精度5μm以下であることを確認する"),
            ("圧力損失", "定格流量時の初期圧力損失が0.05MPa以下であることを確認する"),
        ],
        "issue": "品質基準からの逸脱事例は報告されていない。",
        "spec": [("ろ過精度", "5", "μm"), ("定格流量", "200", "L/min"),
                 ("材質", "ステンレス鋼", "-"), ("重量", "12", "kg"),
                 ("型番", "FLT-E-200", "-")],
    },
    {
        "id": "A009",
        "name": "貯蔵タンクζ",
        "domain": "貯蔵設備",
        "quality_format": "txt",
        "manual_format": "docx",
        "checks": [
            ("液位計精度", "全レンジに対し±1%以内とする"),
            ("防食性能", "内面コーティングに欠陥がないことを確認する"),
        ],
        "issue": "2026年度第2四半期に、経年劣化による内面コーティングの微小な剥離が1基で確認された。"
                 "点検周期を12ヶ月から6ヶ月に短縮して対応中。",
        "spec": [("容量", "10000", "L"), ("最大許容圧力", "大気圧", "-"),
                 ("材質", "炭素鋼(内面コーティング)", "-"), ("重量(空)", "1200", "kg"),
                 ("型番", "TNK-F-10K", "-")],
    },
    {
        "id": "A010",
        "name": "圧力容器η",
        "domain": "圧力設備",
        "quality_format": "pdf",
        "manual_format": "docx",
        "checks": [
            ("耐圧試験", "設計圧力の1.5倍で保持し変形・漏れがないことを確認する"),
            ("溶接部検査", "放射線透過試験で規定の合否基準を満たすことを確認する"),
        ],
        "issue": "品質基準からの逸脱事例は報告されていない。直近の法定検査も無指摘で完了している。",
        "spec": [("設計圧力", "2.5", "MPa"), ("内容積", "3.0", "m3"),
                 ("材質", "圧力容器用鋼板(SPV355)", "-"), ("重量", "1850", "kg"),
                 ("型番", "PRV-G-3000", "-")],
    },
]


# --- ACLグループ割り当て（疎通確認レベルのGuardrail検証用） ---
# 「電気設備・ろ過設備・貯蔵設備・圧力設備」の4製品(A007〜A010)を、
# quality-team相当のグループのみが閲覧できる想定にしている（品質インシデントの
# 詳細を含むため、というシナリオ）。それ以外は全社員(all-employees)に公開。
#
# quality-team相当のグループの実体（common.QUALITY_TEAM_GROUP_ID）は、
# .envの QUALITY_TEAM_GROUP_ID で指定する（OBOの動作確認のためEntra IDに実際に
# 作成したグループのObject ID(GUID)を設定する想定）。コードへの直書きを避けるため
# 環境変数化しており、未設定時はデモ用の文字列スラッグ"quality-team"にフォールバックする。
# all-employees側は引き続きデモ用の文字列スラッグのまま（OBO経由のall-employees相当の
# 実グループが用意でき次第、同様に環境変数化して差し替えること）。
ACL_GROUPS_BY_PRODUCT_ID = {
    "A001": ["all-employees"],
    "A002": ["all-employees"],
    "A003": ["all-employees"],
    "A004": ["all-employees"],
    "A005": ["all-employees"],
    "A006": ["all-employees"],
    "A007": [QUALITY_TEAM_GROUP_ID],
    "A008": [QUALITY_TEAM_GROUP_ID],
    "A009": [QUALITY_TEAM_GROUP_ID],
    "A010": [QUALITY_TEAM_GROUP_ID],
}

PRODUCTS_BY_ID = {p["id"]: p for p in PRODUCTS}

# 既知の製品ID一覧（Allowlist）。search_tool.build_product_id_filter() /
# main.pyのsearch_documents_tool（agent_framework @tool定義）のproduct_id引数の
# 型ヒント（Literal）から参照する。
# 質問文から製品が明確に特定できる場合に、LLMにはこの一覧の中からのみ選ばせ
# （Fabric側のmeasures/columnsと同じAllowlist方式）、実際のフィルタ文字列の組み立ては
# コード側（search_tool.build_product_id_filter）が行う（設計書 第11章 Guardrail）。
PRODUCT_IDS = [p["id"] for p in PRODUCTS]


def product_id_for_document_id(document_id: str) -> str | None:
    """document_id（例: quality_report_A007-0 や manual_A007）から製品IDを取り出す。
    該当製品が見つからない場合はNone。ingest_sample_docs.pyがインデックスへの投入時に
    product_idフィールドへ設定するために使う（search_tool.build_product_id_filter()の
    フィルタ対象フィールド）。"""
    for product_id in PRODUCT_IDS:
        if product_id in document_id:
            return product_id
    return None


def acl_groups_for_document_id(document_id: str) -> list[str]:
    """document_id（例: quality_report_A007-0 や manual_A007）から製品IDを取り出し、
    対応するACLグループを返す。該当製品が見つからない場合は安全側に倒し、
    quality-team相当のグループのみに限定する（誤って全社公開にしないため）。"""
    for product_id, groups in ACL_GROUPS_BY_PRODUCT_ID.items():
        if product_id in document_id:
            return groups
    return [QUALITY_TEAM_GROUP_ID]


# --- Cross Source連携（設計書 第9章）のデモ用共通キー ---
# 本来のCross Sourceは、Fabricの構造化データとAI Searchの文書を実データ上の共通キー
# （製品ID・拠点ID等）で結びつけるものだが、現時点では実データが無い（架空の製品
# マスタと、別途Fabric側の実セマンティックモデルから転記した架空の売上データ、という
# 2つの独立した合成データしかない）。そこで、Fabric側の実セマンティックモデルに
# 存在する「商品群名称」（樹脂部品・金属部品・電子部品・内装部品・機構部品の5分類、
# fabric_schema.py参照）をAI Search側の各文書にも人為的に付与し、両ソースを結ぶ
# デモ用の共通キーとする。実データが用意でき次第、実際の共通キー（製品ID等）に
# 差し替えること。各製品への割り当ては、製品の材質・用途からの大まかな連想で
# 決めたデモ用の値であり、Fabric側の実データとは対応していない。
PRODUCT_GROUPS = ["樹脂部品", "金属部品", "電子部品", "内装部品", "機構部品"]

PRODUCT_GROUP_BY_PRODUCT_ID = {
    "A001": "機構部品",  # バルブユニットX
    "A002": "機構部品",  # ポンプアッセンブリY
    "A003": "電子部品",  # センサーモジュールZ
    "A004": "樹脂部品",  # 触媒コンテナα
    "A005": "金属部品",  # 熱交換器β
    "A006": "金属部品",  # 配管継手γ
    "A007": "電子部品",  # 制御盤δ
    "A008": "樹脂部品",  # フィルターユニットε
    "A009": "金属部品",  # 貯蔵タンクζ
    "A010": "金属部品",  # 圧力容器η
}


def product_group_for_document_id(document_id: str) -> str | None:
    """document_idから製品IDを取り出し、対応する商品群（Cross Source用共通キー）を返す。
    該当製品が見つからない場合はNone（フィルタ対象外）とする。"""
    for product_id, group in PRODUCT_GROUP_BY_PRODUCT_ID.items():
        if product_id in document_id:
            return group
    return None
