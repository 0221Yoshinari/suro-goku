"""訴求用シークレットデモの固定サンプル。

取得処理もデータベースも使わない。10月10日の昼時点の系列合同を、画面に出す数値のままここに置く。
"""

from __future__ import annotations

import json
from pathlib import Path

STATUS_LABEL = {
    "ALL_HIGH": "全6数値",
    "HIGH_56": "全56数値",
    "HIGH_BASE": "全456数値",
    "NORMAL": "通常",
}

SETTING_LABEL = {
    "ALL_HIGH": "設定6近似",
    "HIGH_56": "設定56近似",
    "HIGH_BASE": "設定456近似",
    "NORMAL": "通常挙動",
}


def _rb(units: int, games: int, first_hit: str) -> int:
    denominator = float(first_hit.split("/")[1])
    return max(1, round(units * games / denominator))


def _at(name: str, series: str, units: int, games: int, first_hit: str, status: str) -> dict:
    return {
        "machine_name": name,
        "series": series,
        "kind": "AT",
        "units": units,
        "games": games,
        "normal_games": units * games,
        "first_hit": first_hit,
        "rb_count": _rb(units, games, first_hit),
        "status": status,
    }


def _a(units: int, games: int, bb: str, reg: str, status: str, name: str = "マイジャグラーV") -> dict:
    return {
        "machine_name": name,
        "series": "ジャグラー",
        "kind": "A",
        "units": units,
        "games": games,
        "normal_games": units * games,
        "bb": bb,
        "reg": reg,
        "status": status,
    }


def _hall(hall_name: str, short_name: str, area: str, machines: list[dict]) -> dict:
    return {"hall_name": hall_name, "short_name": short_name, "area": area, "machines": machines}


# 平均回転がちょうど 3,234G になる並び。いずれも昼の 2,000〜3,500G。
_FACE_MONHAN = [
    ("メガフェイス880博多本店", "博多本店", "博多", 14, 3512, "1/308.4", "HIGH_56"),
    ("メガフェイス930朝倉", "朝倉", "朝倉", 9, 3388, "1/321.6", "HIGH_56"),
    ("メガフェイス1030伊都", "伊都", "西", 11, 3496, "1/314.2", "HIGH_56"),
    ("メガフェイス1000糸島", "糸島", "糸島", 8, 3054, "1/351.8", "HIGH_BASE"),
    ("メガフェイス1500宗像", "宗像", "宗像", 10, 3540, "1/306.9", "HIGH_56"),
    ("メガフェイス1300小倉南", "小倉南", "北九州", 12, 2986, "1/348.2", "HIGH_BASE"),
    ("フェイス707幸袋", "幸袋", "飯塚", 11, 3420, "1/318.7", "HIGH_56"),
    ("メガフェイス960飯塚", "飯塚", "飯塚", 8, 2764, "1/359.4", "HIGH_BASE"),
    ("フェイス701巨勢", "巨勢", "市街周辺", 9, 3414, "1/311.5", "HIGH_56"),
    ("フェイス850佐賀", "佐賀", "市街周辺", 10, 3126, "1/326.8", "HIGH_56"),
    ("メガフェイス1450本山", "本山", "本山周辺", 13, 3488, "1/303.2", "HIGH_56"),
    ("メガフェイス555新市街", "新市街", "新市街周辺", 7, 2616, "1/342.6", "HIGH_BASE"),
]

_FACE_EXTRAS = {
    "メガフェイス880博多本店": [
        _a(18, 2860, "1/312.4", "1/398.6", "HIGH_BASE"),
        _at("L ToLOVEる", "ToLOVEる", 6, 2140, "1/468.2", "NORMAL"),
    ],
    "フェイス707幸袋": [
        _a(16, 2480, "1/348.2", "1/441.6", "NORMAL"),
    ],
    "フェイス701巨勢": [
        _a(12, 2710, "1/336.8", "1/422.4", "NORMAL"),
    ],
    "フェイス850佐賀": [
        _a(14, 2644, "1/341.2", "1/428.8", "NORMAL"),
    ],
}


def _face_halls() -> list[dict]:
    halls = []
    for name, short, area, units, games, first_hit, status in _FACE_MONHAN:
        machines = [_at("スマスロ モンスターハンターライズ：サンブレイク", "サンブレイク", units, games, first_hit, status)]
        machines.extend(_FACE_EXTRAS.get(name, []))
        halls.append(_hall(name, short, area, machines))
    return halls


HALLS: list[dict] = _face_halls() + [
    _hall("123博多店", "123博多", "博多", [
        _at("Lリコリス・リコイル", "リコリス・リコイル", 11, 2894, "1/334.6", "HIGH_BASE"),
        _a(14, 2410, "1/352.8", "1/448.2", "NORMAL"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 6, 2180, "1/452.4", "NORMAL"),
    ]),
    _hall("123＋N新市街店", "新市街", "新市街周辺", [
        _a(16, 2680, "1/328.6", "1/412.4", "NORMAL"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 5, 2760, "1/368.8", "HIGH_BASE"),
    ]),
    _hall("玉屋∞本店", "本店", "博多", [
        _at("L真打吉宗", "吉宗", 10, 3055, "1/368.4", "HIGH_56"),
        _a(20, 2840, "1/308.4", "1/386.2", "HIGH_BASE"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 8, 2260, "1/448.6", "NORMAL"),
    ]),
    _hall("玉屋∞新宮", "新宮", "新宮", [
        _at("L真打吉宗", "吉宗", 8, 2688, "1/398.2", "HIGH_BASE"),
        _at("スマスロ モンキーターンV", "モンキーターン", 6, 2510, "1/362.4", "HIGH_BASE"),
        _a(16, 2320, "1/366.2", "1/468.4", "NORMAL"),
    ]),
    _hall("玉屋∞室見", "室見", "室見", [
        _at("L真打吉宗", "吉宗", 8, 2510, "1/406.8", "HIGH_BASE"),
        _at("スマスロ バイオハザード RE:3", "バイオハザード", 7, 2380, "1/412.4", "NORMAL"),
    ]),
    _hall("Super D'station 39福岡本店", "福岡本店", "東", [
        _at("スマスロ 戦国乙女4", "戦国乙女", 10, 3312, "1/286.4", "HIGH_56"),
        _a(22, 2780, "1/322.4", "1/408.8", "HIGH_BASE"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 6, 2140, "1/458.2", "NORMAL"),
    ]),
    _hall("Super D'station 39筑紫野店", "筑紫野", "筑紫", [
        _at("スマスロ 戦国乙女4", "戦国乙女", 8, 2748, "1/338.2", "HIGH_BASE"),
        _a(18, 2360, "1/372.6", "1/468.2", "NORMAL"),
    ]),
    _hall("Super D'station 39伊都店", "伊都", "西", [
        _at("スマスロ 戦国乙女5", "戦国乙女", 9, 2966, "1/292.4", "HIGH_56"),
        _a(16, 2580, "1/348.4", "1/432.8", "HIGH_BASE"),
    ]),
    _hall("D'station 39福重店", "福重", "西", [
        _at("スマスロ 戦国乙女5", "戦国乙女", 11, 3224, "1/278.6", "HIGH_56"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 5, 2210, "1/464.6", "NORMAL"),
    ]),
    _hall("D'station 39中間店", "中間", "北九州", [
        _at("スマスロ 戦国乙女4", "戦国乙女", 8, 3088, "1/304.2", "HIGH_56"),
        _a(14, 2460, "1/364.8", "1/456.2", "NORMAL"),
    ]),
    _hall("D'station 39飯塚店", "飯塚", "飯塚", [
        _at("スマスロ 戦国乙女4", "戦国乙女", 7, 2610, "1/351.6", "HIGH_BASE"),
        _at("スマスロ からくりサーカス", "からくりサーカス", 6, 2080, "1/478.2", "NORMAL"),
    ]),
    _hall("D'station 39門司店", "門司", "北九州", [
        _at("スマスロ 戦国乙女5", "戦国乙女", 8, 2684, "1/334.8", "HIGH_BASE"),
        _at("スマスロ モンキーターンV", "モンキーターン", 6, 3180, "1/248.4", "ALL_HIGH"),
    ]),
]

GROUPS: list[dict] = [
    {
        "id": "face",
        "name": "フェイスグループ",
        "lead": "10月16日の昼。系列の店舗で、共通して上振れた機種を比べます。",
        "halls": [row[0] for row in _FACE_MONHAN],
    },
    {
        "id": "tamaya",
        "name": "玉屋グループ",
        "lead": "10月16日の昼。本店・新宮・室見の3店舗で、共通機種の強さを比べます。",
        "halls": ["玉屋∞本店", "玉屋∞新宮", "玉屋∞室見"],
    },
    {
        "id": "dstation",
        "name": "Dステーショングループ",
        "lead": "10月16日の昼。Dステーション7店舗です。",
        "halls": [
            "Super D'station 39福岡本店",
            "Super D'station 39筑紫野店",
            "Super D'station 39伊都店",
            "D'station 39福重店",
            "D'station 39中間店",
            "D'station 39飯塚店",
            "D'station 39門司店",
        ],
    },
]


def _digit(digit: int, units: int, games: int, setting: float, status: str) -> dict:
    return {
        "digit": digit,
        "units": units,
        "games": games,
        "normal_games": units * games,
        "avg_setting": setting,
        "status": status,
    }


DIGIT_HALLS: list[dict] = [
    {
        "hall_name": "玉屋∞本店",
        "area": "博多",
        "digits": [
            _digit(0, 15, 2280, 2.81, "NORMAL"),
            _digit(1, 14, 2460, 3.18, "NORMAL"),
            _digit(2, 16, 2188, 2.64, "NORMAL"),
            _digit(3, 16, 3120, 4.62, "HIGH_BASE"),
            _digit(4, 13, 2640, 3.84, "NORMAL"),
            _digit(5, 17, 2980, 4.28, "HIGH_BASE"),
            _digit(6, 12, 2060, 2.28, "NORMAL"),
            _digit(7, 18, 3286, 5.41, "HIGH_56"),
            _digit(8, 14, 2520, 3.36, "NORMAL"),
            _digit(9, 15, 2340, 2.94, "NORMAL"),
        ],
    },
    {
        "hall_name": "メガフェイス880博多本店",
        "area": "博多",
        "digits": [
            _digit(0, 12, 2320, 3.02, "NORMAL"),
            _digit(1, 13, 3088, 4.71, "HIGH_BASE"),
            _digit(2, 11, 2410, 3.22, "NORMAL"),
            _digit(3, 14, 2580, 3.55, "NORMAL"),
            _digit(4, 10, 2260, 2.88, "NORMAL"),
            _digit(5, 15, 2710, 3.71, "NORMAL"),
            _digit(6, 12, 2140, 2.76, "NORMAL"),
            _digit(7, 16, 3234, 5.36, "HIGH_56"),
            _digit(8, 13, 2860, 3.96, "NORMAL"),
            _digit(9, 11, 2380, 3.08, "NORMAL"),
        ],
    },
    {
        "hall_name": "123博多店",
        "area": "博多",
        "digits": [
            _digit(0, 11, 2210, 2.92, "NORMAL"),
            _digit(1, 12, 2480, 3.41, "NORMAL"),
            _digit(2, 14, 3012, 5.02, "HIGH_56"),
            _digit(3, 13, 2680, 3.78, "NORMAL"),
            _digit(4, 10, 2180, 2.84, "NORMAL"),
            _digit(5, 15, 2540, 3.52, "NORMAL"),
            _digit(6, 11, 2060, 2.58, "NORMAL"),
            _digit(7, 16, 2860, 4.34, "HIGH_BASE"),
            _digit(8, 12, 2360, 3.12, "NORMAL"),
            _digit(9, 13, 2280, 2.96, "NORMAL"),
        ],
    },
    {
        "hall_name": "Super D'station 39福岡本店",
        "area": "東",
        "digits": [
            _digit(0, 18, 2340, 3.04, "NORMAL"),
            _digit(1, 16, 2520, 3.28, "NORMAL"),
            _digit(2, 14, 2160, 2.52, "NORMAL"),
            _digit(3, 15, 2680, 3.88, "NORMAL"),
            _digit(4, 20, 3188, 5.48, "HIGH_56"),
            _digit(5, 17, 2740, 4.02, "HIGH_BASE"),
            _digit(6, 13, 2080, 2.71, "NORMAL"),
            _digit(7, 16, 2610, 3.66, "NORMAL"),
            _digit(8, 18, 2910, 4.66, "HIGH_BASE"),
            _digit(9, 14, 2420, 3.18, "NORMAL"),
        ],
    },
]


def _unit(
    no: int,
    games: int,
    status: str,
    est: int,
    *,
    bb: str = "",
    reg: str = "",
    first_hit: str = "",
    target: bool = False,
) -> dict:
    row = {"no": no, "games": games, "status": status, "est": est, "target": target}
    if bb:
        row["bb"] = bb
    if reg:
        row["reg"] = reg
    if first_hit:
        row["first_hit"] = first_hit
    return row


LINEUP_HALLS: list[dict] = [
    {
        "hall_name": "極光ホール天神",
        "area": "天神",
        "islands": [
            {
                "machine_name": "マイジャグラーV",
                "series": "ジャグラー",
                "kind": "A",
                "finding": "全15台のうち、641〜645の5台が並びで良さそうな箇所です。推定は6と5が続いています。前後の台は通常です。",
                "units": [
                    _unit(636, 2088, "NORMAL", 2, bb="1/428.4", reg="1/536.2"),
                    _unit(637, 2214, "NORMAL", 3, bb="1/396.8", reg="1/512.4"),
                    _unit(638, 2146, "NORMAL", 2, bb="1/441.2", reg="1/548.6"),
                    _unit(639, 2302, "NORMAL", 3, bb="1/384.6", reg="1/502.8"),
                    _unit(640, 2140, "NORMAL", 2, bb="1/412.6", reg="1/528.4"),
                    _unit(641, 2860, "HIGH_56", 5, bb="1/268.4", reg="1/331.2", target=True),
                    _unit(642, 3120, "HIGH_56", 6, bb="1/241.6", reg="1/298.4", target=True),
                    _unit(643, 2740, "HIGH_BASE", 5, bb="1/289.1", reg="1/352.8", target=True),
                    _unit(644, 3055, "HIGH_56", 6, bb="1/236.8", reg="1/304.2", target=True),
                    _unit(645, 2688, "HIGH_56", 5, bb="1/276.4", reg="1/348.6", target=True),
                    _unit(646, 2280, "NORMAL", 3, bb="1/388.4", reg="1/496.2"),
                    _unit(647, 2196, "NORMAL", 2, bb="1/424.8", reg="1/534.2"),
                    _unit(648, 2360, "NORMAL", 3, bb="1/372.4", reg="1/488.6"),
                    _unit(649, 2072, "NORMAL", 2, bb="1/456.2", reg="1/552.8"),
                    _unit(650, 2248, "NORMAL", 3, bb="1/390.6", reg="1/516.4"),
                ],
            },
            {
                "machine_name": "スマスロ北斗の拳",
                "series": "北斗の拳",
                "kind": "AT",
                "finding": "全15台のうち、210〜213の4台が並びで良さそうな箇所です。推定は6が2台、5が2台。前後の台は通常です。",
                "units": [
                    _unit(204, 2120, "NORMAL", 2, first_hit="1/508.4"),
                    _unit(205, 2284, "NORMAL", 3, first_hit="1/446.2"),
                    _unit(206, 2066, "NORMAL", 2, first_hit="1/532.6"),
                    _unit(207, 2348, "NORMAL", 3, first_hit="1/438.8"),
                    _unit(208, 2210, "NORMAL", 2, first_hit="1/486.2"),
                    _unit(209, 2088, "NORMAL", 3, first_hit="1/452.8"),
                    _unit(210, 3312, "HIGH_56", 6, first_hit="1/246.4", target=True),
                    _unit(211, 3188, "HIGH_56", 6, first_hit="1/258.6", target=True),
                    _unit(212, 2940, "HIGH_56", 5, first_hit="1/286.2", target=True),
                    _unit(213, 2864, "HIGH_56", 5, first_hit="1/298.4", target=True),
                    _unit(214, 2160, "NORMAL", 3, first_hit="1/468.4"),
                    _unit(215, 2224, "NORMAL", 2, first_hit="1/494.6"),
                    _unit(216, 2118, "NORMAL", 3, first_hit="1/462.8"),
                    _unit(217, 2390, "NORMAL", 2, first_hit="1/518.4"),
                    _unit(218, 2176, "NORMAL", 3, first_hit="1/454.2"),
                ],
            },
        ],
    },
    {
        "hall_name": "蒼天アミューズ香椎",
        "area": "香椎",
        "islands": [
            {
                "machine_name": "Lスマスロ モンキーターンＶ",
                "series": "モンキーターン",
                "kind": "AT",
                "finding": "全15台のうち、118〜121の4台が並びで良さそうな箇所です。推定は6が2台、5と4が1台ずつ。前後の台は通常です。",
                "units": [
                    _unit(112, 2094, "NORMAL", 2, first_hit="1/526.4"),
                    _unit(113, 2260, "NORMAL", 3, first_hit="1/448.6"),
                    _unit(114, 2148, "NORMAL", 2, first_hit="1/502.2"),
                    _unit(115, 2312, "NORMAL", 3, first_hit="1/436.8"),
                    _unit(116, 2064, "NORMAL", 2, first_hit="1/512.6"),
                    _unit(117, 2340, "NORMAL", 3, first_hit="1/441.8"),
                    _unit(118, 3420, "HIGH_56", 6, first_hit="1/232.4", target=True),
                    _unit(119, 3288, "HIGH_56", 6, first_hit="1/248.6", target=True),
                    _unit(120, 3012, "HIGH_56", 5, first_hit="1/274.2", target=True),
                    _unit(121, 2760, "HIGH_BASE", 4, first_hit="1/318.4", target=True),
                    _unit(122, 2196, "NORMAL", 2, first_hit="1/498.2"),
                    _unit(123, 2184, "NORMAL", 2, first_hit="1/488.4"),
                    _unit(124, 2056, "NORMAL", 3, first_hit="1/514.6"),
                    _unit(125, 2402, "NORMAL", 2, first_hit="1/472.8"),
                    _unit(126, 2230, "NORMAL", 3, first_hit="1/456.2"),
                ],
            },
            {
                "machine_name": "L甲鉄城のカバネリ海門決戦",
                "series": "カバネリ",
                "kind": "AT",
                "finding": "全15台のうち、330〜332の3台が並びで良さそうな箇所です。推定は6・5・6。前後の台は通常です。",
                "units": [
                    _unit(324, 2168, "NORMAL", 2, first_hit="1/496.4"),
                    _unit(325, 2082, "NORMAL", 3, first_hit="1/528.6"),
                    _unit(326, 2336, "NORMAL", 2, first_hit="1/442.8"),
                    _unit(327, 2204, "NORMAL", 3, first_hit="1/468.2"),
                    _unit(328, 2110, "NORMAL", 2, first_hit="1/524.6"),
                    _unit(329, 2288, "NORMAL", 3, first_hit="1/462.4"),
                    _unit(330, 3360, "HIGH_56", 6, first_hit="1/238.8", target=True),
                    _unit(331, 3084, "HIGH_56", 5, first_hit="1/268.4", target=True),
                    _unit(332, 3216, "HIGH_56", 6, first_hit="1/252.2", target=True),
                    _unit(333, 2144, "NORMAL", 2, first_hit="1/488.6"),
                    _unit(334, 2272, "NORMAL", 2, first_hit="1/484.6"),
                    _unit(335, 2136, "NORMAL", 3, first_hit="1/512.4"),
                    _unit(336, 2418, "NORMAL", 2, first_hit="1/438.6"),
                    _unit(337, 2064, "NORMAL", 3, first_hit="1/534.2"),
                    _unit(338, 2290, "NORMAL", 2, first_hit="1/476.8"),
                ],
            },
        ],
    },
    {
        "hall_name": "虹彩ホール箱崎",
        "area": "箱崎",
        "islands": [
            {
                "machine_name": "マイジャグラーV",
                "series": "ジャグラー",
                "kind": "A",
                "finding": "全15台のうち、219〜222の4台が並びで良さそうな箇所です。推定は6が2台、5が2台。前後の台は通常です。",
                "units": [
                    _unit(214, 2112, "NORMAL", 2, bb="1/448.6", reg="1/556.2"),
                    _unit(215, 2246, "NORMAL", 3, bb="1/392.4", reg="1/508.6"),
                    _unit(216, 2078, "NORMAL", 2, bb="1/462.8", reg="1/538.4"),
                    _unit(217, 2354, "NORMAL", 3, bb="1/378.2", reg="1/494.6"),
                    _unit(218, 2060, "NORMAL", 2, bb="1/436.8", reg="1/542.2"),
                    _unit(219, 3188, "HIGH_56", 6, bb="1/232.4", reg="1/286.6", target=True),
                    _unit(220, 2944, "HIGH_56", 5, bb="1/258.2", reg="1/324.8", target=True),
                    _unit(221, 3310, "HIGH_56", 6, bb="1/226.4", reg="1/278.8", target=True),
                    _unit(222, 2860, "HIGH_56", 5, bb="1/271.6", reg="1/338.4", target=True),
                    _unit(223, 2210, "NORMAL", 3, bb="1/396.2", reg="1/508.4"),
                    _unit(224, 2186, "NORMAL", 2, bb="1/418.4", reg="1/526.8"),
                    _unit(225, 2308, "NORMAL", 3, bb="1/386.6", reg="1/502.4"),
                    _unit(226, 2142, "NORMAL", 2, bb="1/434.2", reg="1/544.6"),
                    _unit(227, 2068, "NORMAL", 3, bb="1/458.8", reg="1/548.2"),
                    _unit(228, 2384, "NORMAL", 2, bb="1/368.4", reg="1/486.6"),
                ],
            },
            {
                "machine_name": "スマスロ北斗の拳",
                "series": "北斗の拳",
                "kind": "AT",
                "finding": "全15台のうち、88〜90の3台が並びで良さそうな箇所です。推定は6・6・5。前後の台は通常です。",
                "units": [
                    _unit(82, 2144, "NORMAL", 2, first_hit="1/498.6"),
                    _unit(83, 2276, "NORMAL", 3, first_hit="1/452.4"),
                    _unit(84, 2086, "NORMAL", 2, first_hit="1/522.8"),
                    _unit(85, 2362, "NORMAL", 3, first_hit="1/434.6"),
                    _unit(86, 2180, "NORMAL", 2, first_hit="1/476.4"),
                    _unit(87, 2320, "NORMAL", 3, first_hit="1/448.2"),
                    _unit(88, 3488, "HIGH_56", 6, first_hit="1/228.6", target=True),
                    _unit(89, 3260, "HIGH_56", 6, first_hit="1/241.2", target=True),
                    _unit(90, 2988, "HIGH_56", 5, first_hit="1/272.8", target=True),
                    _unit(91, 2096, "NORMAL", 2, first_hit="1/506.8"),
                    _unit(92, 2218, "NORMAL", 2, first_hit="1/486.4"),
                    _unit(93, 2132, "NORMAL", 3, first_hit="1/508.2"),
                    _unit(94, 2406, "NORMAL", 2, first_hit="1/428.8"),
                    _unit(95, 2194, "NORMAL", 3, first_hit="1/464.6"),
                    _unit(96, 2062, "NORMAL", 2, first_hit="1/536.4"),
                ],
            },
        ],
    },
    {
        "hall_name": "月光ステージ姪浜",
        "area": "姪浜",
        "islands": [
            {
                "machine_name": "Lスマスロ モンキーターンＶ",
                "series": "モンキーターン",
                "kind": "AT",
                "finding": "全15台のうち、449〜452の4台が並びで良さそうな箇所です。推定は6が3台、5が1台。前後の台は通常です。",
                "units": [
                    _unit(444, 2172, "NORMAL", 2, first_hit="1/492.6"),
                    _unit(445, 2098, "NORMAL", 3, first_hit="1/518.4"),
                    _unit(446, 2324, "NORMAL", 2, first_hit="1/446.8"),
                    _unit(447, 2242, "NORMAL", 3, first_hit="1/458.2"),
                    _unit(448, 2240, "NORMAL", 3, first_hit="1/458.6"),
                    _unit(449, 3396, "HIGH_56", 6, first_hit="1/236.4", target=True),
                    _unit(450, 3180, "HIGH_56", 6, first_hit="1/254.8", target=True),
                    _unit(451, 3052, "HIGH_56", 5, first_hit="1/281.2", target=True),
                    _unit(452, 3274, "HIGH_56", 6, first_hit="1/244.6", target=True),
                    _unit(453, 2168, "NORMAL", 2, first_hit="1/492.4"),
                    _unit(454, 2286, "NORMAL", 2, first_hit="1/478.4"),
                    _unit(455, 2154, "NORMAL", 3, first_hit="1/502.6"),
                    _unit(456, 2070, "NORMAL", 2, first_hit="1/528.8"),
                    _unit(457, 2392, "NORMAL", 3, first_hit="1/442.4"),
                    _unit(458, 2216, "NORMAL", 2, first_hit="1/488.6"),
                ],
            },
            {
                "machine_name": "L甲鉄城のカバネリ海門決戦",
                "series": "カバネリ",
                "kind": "AT",
                "finding": "全15台のうち、701〜704の4台が並びで良さそうな箇所です。推定は6が2台、5が2台。前後の台は通常です。",
                "units": [
                    _unit(696, 2188, "NORMAL", 2, first_hit="1/494.2"),
                    _unit(697, 2106, "NORMAL", 3, first_hit="1/516.8"),
                    _unit(698, 2342, "NORMAL", 2, first_hit="1/448.4"),
                    _unit(699, 2264, "NORMAL", 3, first_hit="1/462.6"),
                    _unit(700, 2124, "NORMAL", 2, first_hit="1/518.2"),
                    _unit(701, 3410, "HIGH_56", 6, first_hit="1/234.8", target=True),
                    _unit(702, 3126, "HIGH_56", 5, first_hit="1/266.4", target=True),
                    _unit(703, 3288, "HIGH_56", 6, first_hit="1/248.2", target=True),
                    _unit(704, 2960, "HIGH_56", 5, first_hit="1/284.6", target=True),
                    _unit(705, 2304, "NORMAL", 3, first_hit="1/446.8"),
                    _unit(706, 2192, "NORMAL", 2, first_hit="1/486.8"),
                    _unit(707, 2084, "NORMAL", 3, first_hit="1/524.2"),
                    _unit(708, 2376, "NORMAL", 2, first_hit="1/436.4"),
                    _unit(709, 2148, "NORMAL", 3, first_hit="1/504.6"),
                    _unit(710, 2236, "NORMAL", 2, first_hit="1/472.2"),
                ],
            },
        ],
    },
]


def joint_score(rows: list[dict]) -> int:
    """昼の回転で、挙動の強さと店舗数から共通機種を並べる。"""
    count = len(rows)
    setting_pts = {"ALL_HIGH": 16, "HIGH_56": 12, "HIGH_BASE": 7, "NORMAL": 0}
    setting = sum(setting_pts[row["status"]] for row in rows) / count
    games = min(10, max(0, (sum(row["games"] for row in rows) / count - 1800) / 200))
    breadth = min(20, (count - 1) * 2)
    return round(setting + games + breadth + 40)


def hall_score(row: dict) -> int:
    setting_pts = {"ALL_HIGH": 40, "HIGH_56": 30, "HIGH_BASE": 18, "NORMAL": 4}
    games = min(12, max(0, (row["games"] - 1800) / 150))
    return round(setting_pts[row["status"]] + games)


def promo_badges(rows: list[dict], *, winner: bool) -> list[dict]:
    badges: list[dict] = []
    if winner:
        badges.append({"text": "🏆 グループ1位機種", "class_name": "promo-gold"})
    strong = sum(1 for row in rows if row["status"] in {"HIGH_56", "ALL_HIGH"})
    if winner or strong * 2 >= len(rows):
        badges.append({"text": "🔥 合同濃厚", "class_name": "promo-hot"})
    if any(row["status"] == "HIGH_BASE" for row in rows):
        badges.append({"text": "📊 全456挙動", "class_name": "promo-base"})
    return badges


# 公表値。ジャグラーは北電子のBB/RB。北斗・モンキーターン・カバネリは初当り。
# 北斗とモンキーターンの設定3は非公表なので、公表されている隣の設定の間に置く。
_PUBLISHED_ODDS = {
    "ジャグラー": {
        "bb": {6: 229.1, 5: 240.1, 4: 254.0, 3: 266.4, 2: 270.8},
        "reg": {6: 229.1, 5: 268.6, 4: 290.0, 3: 336.1, 2: 385.5},
    },
    "北斗の拳": {"first_hit": {6: 235.1, 5: 258.7, 4: 297.8, 3: 334.0, 2: 370.5}},
    "モンキーターン": {"first_hit": {6: 222.9, 5: 235.7, 4: 258.8, 3: 277.0, 2: 295.5}},
    "カバネリ": {"first_hit": {6: 195.1, 5: 203.2, 4: 214.0, 3: 239.6, 2: 242.3}},
}
_ODDS_WOBBLE = (-1.0, -0.4, 0.2, 0.6, 1.0, -0.8, 0.8, -0.6, 0.4, 0.0)


def _near_published(base: float, index: int) -> str:
    """昼の実測らしく、公表値から1ポイント以内にずらす。"""
    return f"1/{base + _ODDS_WOBBLE[index % len(_ODDS_WOBBLE)]:.1f}"


def align_published_odds(island: dict) -> None:
    table = _PUBLISHED_ODDS[island["series"]]
    for index, unit in enumerate(island["units"]):
        est = int(unit["est"])
        if "bb" in table:
            unit["bb"] = _near_published(table["bb"][est], index)
            unit["reg"] = _near_published(table["reg"][est], index + 3)
        if "first_hit" in table:
            unit["first_hit"] = _near_published(table["first_hit"][est], index)


def finish_lineup(island: dict) -> dict:
    """対象は連続した1区間。前後に通常台があり、平均だけでは場所が分からない。"""
    align_published_odds(island)
    units = island["units"]
    flags = [bool(unit.get("target")) for unit in units]
    if not any(flags) or not any(not flag for flag in flags):
        raise RuntimeError(f"並びイベントには対象と前後の通常台が要る: {island['machine_name']}")
    start = flags.index(True)
    end = len(flags) - 1 - flags[::-1].index(True)
    if not all(flags[start : end + 1]):
        raise RuntimeError(f"並びの対象は1区間にまとめる: {island['machine_name']}")
    if len(units) != 15:
        raise RuntimeError(f"並びは15台で見る: {island['machine_name']}")
    targets = units[start : end + 1]
    item = dict(island)
    item["floor_units"] = 15
    item["target_from"] = targets[0]["no"]
    item["target_to"] = targets[-1]["no"]
    item["target_count"] = len(targets)
    item["avg_games"] = round(sum(unit["games"] for unit in targets) / len(targets))
    return item


def _group(group_id: str) -> dict:
    return next(item for item in GROUPS if item["id"] == group_id)


def rank_group(group_id: str) -> list[dict]:
    group = _group(group_id)
    order = {name: index for index, name in enumerate(group["halls"])}
    by_name: dict[str, list[dict]] = {}
    for hall in HALLS:
        if hall["hall_name"] not in order:
            continue
        for machine in hall["machines"]:
            row = {
                "hall_name": hall["hall_name"],
                "short_name": hall["short_name"],
                "area": hall["area"],
                **machine,
                "status_label": STATUS_LABEL[machine["status"]],
                "setting_label": SETTING_LABEL[machine["status"]],
            }
            by_name.setdefault(machine["machine_name"], []).append(row)
    ranked: list[dict] = []
    for name, rows in by_name.items():
        if len(rows) < 2 or not any(row["status"] != "NORMAL" for row in rows):
            continue
        rows.sort(key=lambda row: order[row["hall_name"]])
        ranked.append({
            "machine_name": name,
            "series": rows[0]["series"],
            "kind": rows[0]["kind"],
            "rows": rows,
            "score": joint_score(rows),
            "avg_games": round(sum(row["games"] for row in rows) / len(rows)),
        })
    ranked.sort(key=lambda item: (-item["score"], -len(item["rows"]), item["machine_name"]))
    for index, item in enumerate(ranked):
        item["badges"] = promo_badges(item["rows"], winner=index == 0)
    return ranked


def describe_joint(group: dict, ranked: list[dict]) -> str:
    top = ranked[0]
    if group["id"] == "face":
        strong = sum(1 for row in top["rows"] if row["status"] == "HIGH_56")
        base = sum(1 for row in top["rows"] if row["status"] == "HIGH_BASE")
        low = min(row["games"] for row in top["rows"])
        high = max(row["games"] for row in top["rows"])
        return (
            f"昼時点の{len(top['rows'])}店舗。平均回転は{top['avg_games']:,}Gで、"
            f"{low:,}Gの店から{high:,}Gの店まで初当りは設定56が{strong}店、設定456が{base}店。"
            "2,000G台でも挙動は揃っていて、グループ1位です。"
        )
    if group["id"] == "tamaya":
        bits = [
            f"{row['short_name']}は{row['setting_label']}（初当り{row['first_hit']}、{row['games']:,}G）"
            for row in top["rows"]
        ]
        second = ranked[1]["machine_name"] if len(ranked) > 1 else ""
        follow = f"{second}が続きます。" if second else ""
        return "。".join(bits) + f"。昼の回転で3店舗共通の1位は{top['machine_name']}です。{follow}"
    otome: set[str] = set()
    for item in ranked:
        if "戦国乙女" not in item["machine_name"]:
            continue
        otome.update(row["hall_name"] for row in item["rows"])
    second = ranked[1]
    return (
        f"1位は{top['machine_name']}。"
        f"{second['machine_name']}は僅差の2位。"
        f"対象{len(group['halls'])}店舗のうち{len(otome)}店舗で乙女シリーズが全456以上です。"
        "門司のモンキーターンVは単店の全6ですが、1店舗だけなので合同の対象外です。"
        "いずれも昼の2,000〜3,000G台です。"
    )


# 詳細エリアに出す近くの店舗。都道府県名は入れない。先頭5店が全台系の反応店。
_NEARBY_HALLS = [
    "白金ステージ天神",
    "朱雀アミューズ",
    "青葉ホール",
    "星河ステージ",
    "灯台ホール",
    "朝霧アミューズ",
    "港南ステージ",
    "桜木ホール",
    "西新ステージ",
    "春吉アミューズ",
    "住吉ホール",
    "百道ステージ",
    "今泉ホール",
    "薬院ステージ",
    "赤坂ホール",
    "平尾ステージ",
    "大濠ホール",
    "警固ステージ",
    "渡辺通ホール",
    "中洲ステージ",
]

_AREA_LABEL = "天神・博多周辺"


def city_floor() -> list[dict]:
    """選択中エリアの全台系。全6を1機種だけにして、全56と全456を混ぜる。

    ファンキージャグラー2の設定6公表は BB 1/219.9・REG 1/262.1。
    全56のカードはそれより浅い設定5付近（BB 1/236.8・REG 1/285.2）に置く。
    いざ！番長の設定6初当たりは 1/280.0、カバネリ設定5は 1/203.2、
    サンブレイク設定4は 1/346.8、北斗設定4は 1/297.8。
    """
    specs = [
        ("白金ステージ天神", "白金", _a(13, 2924, "1/236.8", "1/285.2", "HIGH_56", "ファンキージャグラー2")),
        ("朱雀アミューズ", "朱雀", _at("いざ！番長", "番長", 4, 3210, "1/279.2", "ALL_HIGH")),
        ("青葉ホール", "青葉", _at("L甲鉄城のカバネリ海門決戦", "カバネリ", 10, 3088, "1/204.1", "HIGH_56")),
        ("星河ステージ", "星河", _at("スマスロ モンスターハンターライズ：サンブレイク", "サンブレイク", 8, 2746, "1/347.6", "HIGH_BASE")),
        ("灯台ホール", "灯台", _at("スマスロ北斗の拳", "北斗", 6, 2512, "1/298.4", "HIGH_BASE")),
    ]
    rows = []
    for hall_name, short, machine in specs:
        rows.append({
            "hall_name": hall_name,
            "short_name": short,
            "area": _AREA_LABEL,
            **machine,
            "status_label": STATUS_LABEL[machine["status"]],
            "setting_label": SETTING_LABEL[machine["status"]],
        })
    return rows


# 10:00開店、16:30で6.5時間。1時間750回転前後。
# 4台で1巡。推定1（ハズレ）→推定5（当たり）→推定2（ハズレ）→推定6（当たり）。
# 公表値はマイジャグラーV。設定1と設定2はREGで分かれ、BBはどちらも270台。
_JUGGLER_ROWS = [
    (241, 4878, 18, 12, 1),
    (242, 5108, 21, 19, 5),
    (243, 5068, 19, 13, 2),
    (244, 5042, 22, 22, 6),
    (245, 4917, 18, 12, 1),
    (246, 4875, 20, 18, 5),
    (247, 4918, 18, 13, 2),
    (248, 5289, 23, 23, 6),
    (249, 4968, 18, 12, 1),
    (250, 5297, 22, 20, 5),
    (251, 5348, 20, 14, 2),
    (252, 4874, 21, 21, 6),
    (253, 5234, 19, 13, 1),
    (254, 5036, 21, 19, 5),
    (255, 4960, 18, 13, 2),
    (256, 5256, 23, 23, 6),
    (257, 5291, 19, 13, 1),
    (258, 5084, 21, 19, 5),
    (259, 5104, 19, 13, 2),
    (260, 5071, 22, 22, 6),
]

_JUGGLER_STATUS = {1: "NORMAL", 2: "NORMAL", 5: "HIGH_56", 6: "ALL_HIGH"}


def _odds(games: int, count: int) -> str:
    return f"1/{games / count:.1f}"


def juggler_floor() -> dict:
    units = []
    for no, games, bb, reg, est in _JUGGLER_ROWS:
        status = _JUGGLER_STATUS[est]
        units.append({
            "no": no,
            "games": games,
            "bb_count": bb,
            "reg_count": reg,
            "hit_count": bb + reg,
            "bb": _odds(games, bb),
            "reg": _odds(games, reg),
            "first_hit": _odds(games, bb + reg),
            "est": est,
            "status": status,
            "status_label": STATUS_LABEL[status],
            "setting_label": SETTING_LABEL[status],
        })
    return {
        "hall_name": "白金ステージ天神",
        "area": _AREA_LABEL,
        "machine_name": "マイジャグラーV",
        "opened_at": "10:00",
        "units": units,
    }


def demo_payload() -> dict:
    halls = []
    for hall in HALLS:
        machines = []
        for machine in hall["machines"]:
            item = dict(machine)
            item["score"] = hall_score(item)
            item["status_label"] = STATUS_LABEL[item["status"]]
            item["setting_label"] = SETTING_LABEL[item["status"]]
            machines.append(item)
        machines.sort(key=lambda row: row["score"], reverse=True)
        halls.append({**hall, "machines": machines})
    lineup_halls = []
    for hall in LINEUP_HALLS:
        islands = [finish_lineup(island) for island in hall["islands"]]
        lineup_halls.append({**hall, "islands": islands})
    groups = []
    for group in GROUPS:
        ranked = rank_group(group["id"])
        groups.append({**group, "summary": describe_joint(group, ranked), "ranked": ranked})
    return {
        "date": "2026-10-17",
        "judged_at": "2026-10-17 16:30",
        "area_label": _AREA_LABEL,
        "nearby": list(_NEARBY_HALLS),
        "halls": halls,
        "city": city_floor(),
        "juggler": juggler_floor(),
        "groups": groups,
        "digits": DIGIT_HALLS,
        "lineup_halls": lineup_halls,
    }


def render_demo_page(template_path: Path) -> str:
    template = template_path.read_text(encoding="utf-8")
    payload = json.dumps(demo_payload(), ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")
    if "__DEMO_DATA__" not in template:
        raise RuntimeError("demo template marker missing")
    return template.replace("__DEMO_DATA__", payload, 1)
