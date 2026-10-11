"""都道府県ごとの詳細エリア。住所と店舗名のキーワードで振り分ける。"""

from __future__ import annotations

# (id, label, keywords)。keywords が空の「その他」は、どのキーワードにも当たらない店。
# 全域は areas が空で、その県の全店舗。
_SPECS: tuple[tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...]], ...] = (
    (
        "tokyo",
        (
            ("tokyo_shinjuku", "新宿・渋谷・中野", ("新宿", "渋谷", "中野", "杉並", "高田馬場")),
            ("tokyo_ikebukuro", "池袋・豊島・板橋・練馬", ("豊島", "池袋", "板橋", "練馬")),
            ("tokyo_ueno", "上野・秋葉原・都心東部", ("台東", "千代田", "中央区", "文京", "秋葉原", "上野")),
            ("tokyo_south", "港・品川・大田・世田谷", ("港区", "品川", "大田", "目黒", "世田谷")),
            ("tokyo_east", "城東（江東・墨田・江戸川・葛飾・足立）", ("江東", "墨田", "江戸川", "葛飾", "足立", "荒川")),
            ("tokyo_tama", "多摩", ("八王子", "立川", "町田", "府中", "調布", "三鷹", "武蔵野", "国分寺", "国立", "昭島", "日野", "多摩", "青梅", "福生", "羽村", "あきる野", "西東京", "東村山", "東大和", "清瀬", "東久留米", "武蔵村山", "稲城", "狛江", "小金井")),
            ("tokyo_other", "その他", ()),
        ),
    ),
    (
        "kanagawa",
        (
            ("kanagawa_yokohama", "横浜", ("横浜",)),
            ("kanagawa_kawasaki", "川崎", ("川崎",)),
            ("kanagawa_shonan", "湘南・横須賀", ("藤沢", "鎌倉", "茅ヶ崎", "平塚", "逗子", "葉山", "横須賀", "三浦")),
            ("kanagawa_west", "県央・相模原", ("厚木", "海老名", "座間", "綾瀬", "大和", "伊勢原", "愛川", "相模原", "秦野", "小田原")),
            ("kanagawa_other", "その他", ()),
        ),
    ),
    (
        "saitama",
        (
            ("saitama_city", "さいたま市", ("さいたま",)),
            ("saitama_south", "南部", ("川口", "戸田", "蕨", "草加", "越谷", "八潮", "三郷", "吉川", "春日部")),
            ("saitama_west", "西部", ("川越", "所沢", "朝霞", "志木", "新座", "富士見", "ふじみ野", "狭山", "入間", "飯能")),
            ("saitama_other", "その他", ()),
        ),
    ),
    (
        "chiba",
        (
            ("chiba_city", "千葉市", ("千葉市",)),
            ("chiba_west", "東葛・湾岸", ("市川", "船橋", "浦安", "習志野", "松戸", "柏", "流山", "我孫子", "鎌ケ谷", "鎌ヶ谷", "八千代", "四街道", "印西")),
            ("chiba_other", "その他", ()),
        ),
    ),
    (
        "aichi",
        (
            ("aichi_nagoya", "名古屋", ("名古屋",)),
            ("aichi_owari", "尾張", ("一宮", "春日井", "小牧", "稲沢", "津島", "あま", "清須", "北名古屋", "江南", "犬山", "岩倉")),
            ("aichi_mikawa", "三河", ("豊田", "岡崎", "安城", "刈谷", "豊橋", "西尾", "碧南", "高浜", "知立", "大府", "東海", "半田", "常滑")),
            ("aichi_other", "その他", ()),
        ),
    ),
    (
        "osaka",
        (
            ("osaka_sakai", "堺", ("堺",)),
            ("osaka_hokusetsu", "北摂・北河内", ("吹田", "豊中", "茨木", "高槻", "摂津", "箕面", "池田", "守口", "門真", "寝屋川", "枚方", "交野")),
            ("osaka_kita", "大阪市北・淀川", ("北区", "福島", "都島", "淀川", "東淀川", "旭区", "城東", "鶴見")),
            ("osaka_minami", "大阪市ミナミ・南部", ("中央区", "西区", "浪速", "天王寺", "阿倍野", "住吉", "住之江", "西成", "東成", "生野", "平野", "東住吉")),
            ("osaka_other", "その他", ()),
        ),
    ),
    (
        "hyogo",
        (
            ("hyogo_kobe", "神戸", ("神戸",)),
            ("hyogo_hanshin", "阪神間", ("尼崎", "西宮", "芦屋", "伊丹", "宝塚", "川西")),
            ("hyogo_other", "その他", ()),
        ),
    ),
    (
        "hokkaido",
        (
            ("hokkaido_sapporo", "札幌", ("札幌",)),
            ("hokkaido_donan", "道南", ("函館", "室蘭", "苫小牧", "千歳")),
            ("hokkaido_dohoku", "道北・道東", ("旭川", "釧路", "帯広", "北見", "網走")),
            ("hokkaido_other", "その他", ()),
        ),
    ),
    (
        "kyoto",
        (
            ("kyoto_city", "京都市", ("京都市",)),
            ("kyoto_other", "その他", ()),
        ),
    ),
    (
        "hiroshima",
        (
            ("hiroshima_city", "広島市", ("広島市",)),
            ("hiroshima_other", "その他", ()),
        ),
    ),
    (
        "miyagi",
        (
            ("miyagi_sendai", "仙台", ("仙台",)),
            ("miyagi_other", "その他", ()),
        ),
    ),
    (
        "kumamoto",
        (
            ("kumamoto_city", "熊本市", ("熊本市", "中央区", "新市街")),
            ("kumamoto_other", "その他", ()),
        ),
    ),
    (
        "saga",
        (
            ("saga_karatsu", "唐津", ("唐津",)),
            ("saga_city", "佐賀市", ("佐賀市",)),
            ("saga_other", "その他", ()),
        ),
    ),
    (
        "okinawa",
        (
            ("okinawa_naha", "那覇", ("那覇",)),
            ("okinawa_other", "その他", ()),
        ),
    ),
    (
        "fukuoka",
        (
            ("kitakyushu", "北九州", ("北九州", "八幡", "門司", "戸畑", "小倉", "若松")),
            ("chikugo", "筑後・久留米", ("久留米", "柳川", "大牟田", "八女", "大川", "みやま", "筑後", "うきは")),
            ("chikushi", "筑紫", ("春日", "大野城", "太宰府", "筑紫野", "那珂川")),
            ("fukuoka_west", "福岡西・糸島", ("糸島", "早良", "西区", "姪浜", "室見")),
            ("fukuoka_east", "福岡東・糟屋", ("東区", "糟屋", "粕屋", "古賀", "新宮", "香椎", "志免")),
            ("fukuoka_central", "福岡中央", ("博多", "中央区", "南区", "天神", "城南")),
        ),
    ),
)

_LABELS = {
    "tokyo": "東京都",
    "kanagawa": "神奈川県",
    "saitama": "埼玉県",
    "chiba": "千葉県",
    "aichi": "愛知県",
    "osaka": "大阪府",
    "hyogo": "兵庫県",
    "hokkaido": "北海道",
    "kyoto": "京都府",
    "hiroshima": "広島県",
    "miyagi": "宮城県",
    "kumamoto": "熊本県",
    "saga": "佐賀県",
    "okinawa": "沖縄県",
    "fukuoka": "福岡県",
}

AREA_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    pref: tuple((area_id, keywords) for area_id, _label, keywords in areas if keywords)
    for pref, areas in _SPECS
}

EXTRA_AREA_PRESETS: tuple[dict, ...] = tuple(
    item
    for pref, areas in _SPECS
    if pref != "fukuoka"
    for item in (
        *[
            {
                "id": area_id,
                "prefecture": pref,
                "label": label,
                "areas": (area_id,),
            }
            for area_id, label, _keywords in areas
        ],
        {
            "id": "all",
            "prefecture": pref,
            "label": f"全域（{_LABELS.get(pref, pref)}内すべて）",
            "areas": (),
            "default": True,
        },
    )
)


def infer_area(prefecture: str, address: str = "", name: str = "") -> str:
    """住所と店舗名から詳細エリアIDを返す。福岡で当てはまらなければ空。"""
    pref = (prefecture or "").strip()
    rules = AREA_RULES.get(pref)
    if not rules:
        return ""
    blob = f"{address or ''} {name or ''}"
    for area_id, keywords in rules:
        if any(keyword and keyword in blob for keyword in keywords):
            return area_id
    if pref == "fukuoka":
        return ""
    return f"{pref}_other"
