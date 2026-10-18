import json
import unittest
from pathlib import Path

from src.analyzer.judgment import judge_record, load_masters
from src.scraper.goraggio import fetch_goraggio_hall, parse_model_links, parse_unit_table, select_cycle_halls
from src.scraper.halls import Hall, TOKYO_PILOT_HALL_IDS, area_presets, configured_halls, hall_by_name, hall_groups, hall_in_active_scope, hall_locale_index, load_halls, load_halls_config, prefectures
from src.scraper.pscube import parse_assist_json, parse_pscube_tables
from src.scraper.registry import list_sources
from src.scraper.teramoba import _normalize_unit, target_from_hall

ROOT = Path(__file__).resolve().parents[1]

GORAGGIO_LIST = """
<ul class="m_list">
  <li class="Slot">
    <a href="https://daidata.goraggio.com/100969/unit_list?model=L%2F%EF%BE%90%EF%BE%98%EF%BD%B5%EF%BE%9D%EF%BD%BA%EF%BE%9E%EF%BD%AF%EF%BE%84%EF%BE%9E%2FCX&amp;ballPrice=20.00&amp;ps=S">
      <h2 class="model_name">L/ﾐﾘｵﾝｺﾞｯﾄﾞ/CX</h2>
      20円スロット | 3台
    </a>
  </li>
  <li class="Slot">
    <a href="https://daidata.goraggio.com/100969/unit_list?model=other&amp;ps=S">
      <h2 class="model_name">未監視機種</h2>
      20円スロット | 1台
    </a>
  </li>
</ul>
"""

GORAGGIO_UNITS = """
<table class="sorter tablesorter">
  <thead>
    <tr>
      <th></th>
      <th>台番号</th>
      <th>累計スタート</th>
      <th>BB回数</th>
      <th>RB回数</th>
      <th>最大持ち玉</th>
      <th>BB確率</th>
      <th>RB確率</th>
      <th>合成確率</th>
      <th>スタート回数</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td></td>
      <td><a href="/100969/detail?unit=376">376</a></td>
      <td class="today">3554</td>
      <td class="today">0</td>
      <td class="today">23</td>
      <td class="today">5313</td>
      <td class="today">1/0.0</td>
      <td class="today">1/154.5</td>
      <td class="today">154.5</td>
      <td class="today">49</td>
    </tr>
  </tbody>
  <tfoot>
    <tr><td></td><th>平均</th><td>4588</td><td>0</td><td>17</td><td>3075</td><td>1/0.0</td><td>1/269.9</td><td>269.9</td><td>69</td></tr>
  </tfoot>
</table>
"""

PSCUBE_HTML = """
<h2>Lミリオンゴッド</h2>
<table>
  <thead>
    <tr><th>台番号</th><th>通常G</th><th>BB</th><th>RB</th><th>ART</th><th>差枚</th></tr>
  </thead>
  <tbody>
    <tr><td>101</td><td>3200</td><td>2</td><td>8</td><td>5</td><td>+1200</td></tr>
    <tr><td>平均</td><td>1000</td><td>1</td><td>1</td><td>1</td><td>0</td></tr>
  </tbody>
</table>
"""


class NewHallConfigTest(unittest.TestCase):
    def test_four_halls_are_enabled_and_grouped(self) -> None:
        load_halls_config.cache_clear()
        payload = load_halls_config()
        self.assertFalse(payload.get("scrape_groups_only"))
        self.assertFalse(payload.get("acquisition_limited"))
        catalog = [hall.name for hall in load_halls()]
        self.assertGreaterEqual(len(catalog), 649)
        names = configured_halls()
        for raw in payload["halls"]:
            public = raw.get("archived") is not True and raw.get("data_public") is not False
            self.assertEqual(bool(raw.get("enabled")), public, raw.get("id"))
        self.assertNotIn("古賀プラザ", names)
        self.assertFalse(hall_by_name("古賀プラザ").enabled)
        for name in ("GOGOアリーナ天神", "ゾーン姪浜", "メガフェイス博多", "ブーム天神", "ワンダーランド柳川店"):
            self.assertIn(name, catalog)
            self.assertIn(name, names)
            self.assertTrue(hall_by_name(name).enabled)
        self.assertIn("玉屋666唐津", catalog)
        self.assertIn("玉屋666唐津", names)
        self.assertIn("玉屋∞本店", names)
        self.assertIn("プラザ 本店", names)
        self.assertTrue({"tamaya", "plaza", "wonderland"} <= {item["id"] for item in hall_groups()})
        enabled_names = {hall.name for hall in load_halls()}
        fukuoka_names = {hall.name for hall in load_halls() if hall.prefecture == "fukuoka"}
        tokyo_names = {hall.name for hall in load_halls() if hall.id in TOKYO_PILOT_HALL_IDS}
        self.assertEqual(set(names), enabled_names)
        self.assertTrue(fukuoka_names | tokyo_names <= set(names))
        scoped_groups = {item["id"] for item in hall_groups(scoped=True)}
        self.assertTrue({"tamaya", "plaza", "wonderland", "espace"} <= scoped_groups)
        self.assertEqual(hall_by_name("パーラーゾーン姪浜").name, "ゾーン姪浜")
        self.assertEqual(hall_by_name("GOGOアリーナ天神").source, "goraggio")
        self.assertEqual(hall_by_name("ゾーン姪浜").source, "pscube")
        self.assertEqual(hall_by_name("メガフェイス博多").source, "pscube")
        boom = hall_by_name("BOOM天神本店")
        self.assertEqual(boom.name, "ブーム天神")
        self.assertEqual(boom.source, "teramoba")
        target = target_from_hall(boom)
        self.assertEqual(target.hall_id, 338)
        self.assertEqual(target.department, "boomtengin")
        self.assertEqual(target.kind_codes, ("G", "D"))
        self.assertIn("goraggio", list_sources())
        self.assertIn("pscube", list_sources())
        wonderland = next(item for item in hall_groups() if item["id"] == "wonderland")
        self.assertEqual(wonderland["label"], "ワンダーランド")
        self.assertGreaterEqual(len(wonderland["halls"]), 15)
        self.assertIn("ワンダーランド1188福岡東店", wonderland["halls"])
        self.assertIn("ワンダーランド香椎本館", wonderland["halls"])
        self.assertEqual(hall_by_name("ワンダーランド福岡東").source, "goraggio")
        self.assertEqual(len(TOKYO_PILOT_HALL_IDS), 10)
        tokyo = [hall.name for hall in load_halls() if hall.id in TOKYO_PILOT_HALL_IDS]
        self.assertEqual(len(tokyo), 10)
        for name in tokyo:
            self.assertIn(name, names)
        self.assertEqual(len(names), len(enabled_names))
        self.assertGreater(len(names), len(fukuoka_names) + len(tokyo_names))
        self.assertGreater(len(fukuoka_names), 28)
        hakozaki = hall_by_name("マルハン箱崎店")
        self.assertEqual(hakozaki.source, "maruhan_app")
        self.assertTrue(hakozaki.enabled)
        self.assertEqual(hakozaki.prefecture, "fukuoka")
        self.assertEqual(hakozaki.area, "fukuoka_east")
        fukuoka = hall_by_name("スーパーDステーション39福岡本店")
        self.assertEqual(fukuoka.name, "Super D'station 39福岡本店")
        self.assertEqual(fukuoka.source, "pscube")
        self.assertTrue(fukuoka.enabled)
        self.assertEqual((fukuoka.source_config or {}).get("variant"), "netcube")

    def test_every_hall_has_prefecture_and_area(self) -> None:
        load_halls_config.cache_clear()
        prefs = prefectures()
        self.assertEqual(prefs[0]["id"], "fukuoka")
        self.assertEqual(prefs[0]["default_area"], "fukuoka_central")
        self.assertEqual(
            prefs[0]["hall_count"],
            sum(1 for hall in load_halls() if hall.prefecture == "fukuoka" and hall_in_active_scope(hall)),
        )
        self.assertGreaterEqual(prefs[0]["hall_count"], 10)
        pref_ids = [item["id"] for item in prefs]
        self.assertEqual(pref_ids[0], "fukuoka")
        self.assertIn("tokyo", pref_ids)
        self.assertIn("saga", pref_ids)
        self.assertGreater(len(pref_ids), 2)
        fukuoka_areas = [item["id"] for item in area_presets() if item["prefecture"] == "fukuoka"]
        self.assertEqual(
            fukuoka_areas,
            ["fukuoka_central", "fukuoka_west", "fukuoka_east", "chikushi", "chikugo", "kitakyushu", "all"],
        )
        halls = load_halls(include_disabled=True)
        self.assertGreaterEqual(len(halls), 47)
        for hall in halls:
            self.assertTrue(hall.prefecture, hall.name)
            self.assertTrue(hall.area, hall.name)
        locales = hall_locale_index(include_disabled=True)
        self.assertEqual(locales["GOGOアリーナ天神"], {"prefecture": "fukuoka", "area": "fukuoka_central"})
        self.assertEqual(locales["玉屋∞室見"]["area"], "fukuoka_west")
        self.assertEqual(locales["玉屋∞新宮"]["area"], "fukuoka_east")
        self.assertEqual(locales["玉屋GO＋太宰府"]["area"], "chikushi")
        self.assertEqual(locales["ワンダーランド柳川店"]["area"], "chikugo")
        shinshigai = locales["123＋N新市街店"]
        self.assertEqual(shinshigai["prefecture"], "kumamoto")
        self.assertNotEqual(shinshigai["prefecture"], "fukuoka")
        self.assertEqual(locales["玉屋666唐津"]["prefecture"], "saga")

    def test_halls_json_stays_valid(self) -> None:
        payload = json.loads((ROOT / "config" / "halls.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in payload["halls"]]
        self.assertEqual(len(ids), len(set(ids)))


class GoraggioCycleTest(unittest.TestCase):
    def test_cycle_keeps_fetched_halls_and_caps_new_ones(self) -> None:
        halls = [
            Hall(id="old", name="取得済み", aliases=("取得済み",)),
            Hall(id="a", name="新規A", aliases=("新規A",)),
            Hall(id="b", name="新規B", aliases=("新規B",)),
            Hall(id="c", name="新規C", aliases=("新規C",)),
        ]
        chosen = select_cycle_halls(halls, {"取得済み"}, new_limit=2)
        self.assertEqual([hall.name for hall in chosen], ["取得済み", "新規A", "新規B"])


class GoraggioParserTest(unittest.TestCase):
    def test_model_links_keep_halfwidth_names(self) -> None:
        links = parse_model_links(GORAGGIO_LIST, "https://daidata.goraggio.com/100969/list")
        self.assertEqual(links[0][0], "L/ﾐﾘｵﾝｺﾞｯﾄﾞ/CX")
        self.assertIn("unit_list", links[0][1])
        self.assertNotIn("&amp;", links[0][1])
        self.assertEqual(len(links), 2)

    def test_unit_table_uses_cumulative_starts(self) -> None:
        rows = parse_unit_table(GORAGGIO_UNITS, "L/ﾐﾘｵﾝｺﾞｯﾄﾞ/CX", "GOGOアリーナ天神")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["dai_no"], "376")
        self.assertEqual(row["total_games"], 3554)
        self.assertEqual(row["bb"], 0)
        self.assertEqual(row["rb"], 23)
        self.assertEqual(row["合成確率"], "1/154.5")
        self.assertIsNone(row["BB確率"])
        self.assertEqual(row["最大持ち玉"], 5313)
        self.assertEqual(row["hall_name"], "GOGOアリーナ天神")


class GoraggioModelFailureTest(unittest.TestCase):
    def test_one_model_error_keeps_the_rest(self) -> None:
        list_html = """
        <a href="https://daidata.goraggio.com/100969/unit_list?model=ok">マイジャグラーV</a>
        <a href="https://daidata.goraggio.com/100969/unit_list?model=boom">ミリオンゴッド</a>
        <a href="https://daidata.goraggio.com/100969/unit_list?model=later">ゴーゴージャグラー</a>
        """

        class _Client:
            def open(self, url, data=None, referer=None):
                if "unit_list" not in url:
                    return url, list_html
                if "model=boom" in url:
                    raise TimeoutError("同時接続が 12秒空かなかった")
                if "model=later" in url:
                    raise ValueError("parse broke")
                return url, GORAGGIO_UNITS

            def accept_if_needed(self, code, final, html, referer):
                return final, html

        hall = Hall(
            id="gogo-arena-tenjin",
            name="GOGOアリーナ天神",
            aliases=(),
            source="goraggio",
            source_url="https://daidata.goraggio.com/100969",
            source_config={"hall_code": "100969", "ps": "S"},
        )
        rows = fetch_goraggio_hall(hall, client=_Client())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["machine_name"], "マイジャグラーV")
        self.assertEqual(rows[0]["dai_no"], "376")


class PscubeParserTest(unittest.TestCase):
    def test_html_table_reads_normal_games_and_bonus_counts(self) -> None:
        rows = parse_pscube_tables(PSCUBE_HTML, "ゾーン姪浜")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["machine_name"], "Lミリオンゴッド")
        self.assertEqual(row["dai_no"], "101")
        self.assertEqual(row["total_games"], 3200)
        self.assertEqual(row["bb"], 2)
        self.assertEqual(row["rb"], 8)
        self.assertEqual(row["art"], 5)
        self.assertEqual(row["差玉"], 1200)

    def test_dmm_json_uses_latest_slot_row(self) -> None:
        listed = {
            "types": [
                {"type": 0, "type_name": "4円パチンコ", "group_name": "P"},
                {"type": 21, "type_name": "20円スロット", "group_name": "S"},
            ],
            "machines": [],
        }
        dai = {
            "mdata": {"1": {"mm_id": "1", "mname": "Lミリオンゴッド"}},
            "dai": {
                "12": {
                    "mm_id": "1",
                    "type": 21,
                    "rows": [
                        {"start": 819, "bb_num": 0, "reg_num": 1, "art_num": 4, "diff": -4365, "bonus_rate": "1/164"},
                        {"start": 100, "bb_num": 9, "reg_num": 9, "art_num": 9, "diff": 1, "bonus_rate": "1/9"},
                    ],
                },
                "13": {"mm_id": "1", "type": 0, "rows": [{"start": 50, "bb_num": 1, "reg_num": 0, "art_num": 0}]},
            },
        }
        rows = parse_assist_json(listed, dai, "メガフェイス博多")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dai_no"], "12")
        self.assertEqual(rows[0]["total_games"], 819)
        self.assertEqual(rows[0]["bb"], 0)
        self.assertEqual(rows[0]["rb"], 1)
        self.assertEqual(rows[0]["art"], 4)
        self.assertEqual(rows[0]["差玉"], -4365)
        self.assertEqual(rows[0]["合成確率"], "1/164")

    def test_netcube_unit_reads_games_big_and_reg(self) -> None:
        from src.scraper.pscube import netcube_slot_units, parse_netcube_unit

        listed = {
            "Ki": [
                {
                    "nmk_kisyu": "ｺﾞｰｺﾞｰｼﾞｬｸﾞﾗｰ3",
                    "cd_ps": 2,
                    "list": {"cd_dai": ["0397", "0398"]},
                },
                {"nmk_kisyu": "未監視機種", "cd_ps": 2, "list": {"cd_dai": ["0001"]}},
                {"nmk_kisyu": "海物語", "cd_ps": 1, "list": {"cd_dai": ["0100"]}},
            ]
        }
        units = netcube_slot_units(listed)
        self.assertEqual(units, [("ｺﾞｰｺﾞｰｼﾞｬｸﾞﾗｰ3", "0397"), ("ｺﾞｰｺﾞｰｼﾞｬｸﾞﾗｰ3", "0398")])
        row = parse_netcube_unit(
            {
                "Ki": {"name": "ｺﾞｰｺﾞｰｼﾞｬｸﾞﾗｰ3"},
                "Dai": {"cd_dai": "0397"},
                "Data": [
                    {"title": "BONUS", "item": [6]},
                    {"title": "BIG", "item": [5]},
                    {"title": "REG", "item": [1]},
                    {"title": "AT/ART", "item": [0]},
                    {"title": "累計ゲーム", "item": [1130]},
                ],
            },
            "ゾーン姪浜",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["dai_no"], "0397")
        self.assertEqual(row["total_games"], 1130)
        self.assertEqual(row["bb"], 5)
        self.assertEqual(row["rb"], 1)
        self.assertEqual(row["art"], 0)
        self.assertEqual(row["hall_name"], "ゾーン姪浜")


class TeramobaPayoutTest(unittest.TestCase):
    def test_unit_keeps_medal_diff(self) -> None:
        row = _normalize_unit(
            {
                "rack_no": 10,
                "machine_name": "Lミリオンゴッド",
                "all_game_count": 4000,
                "bonus_1": 1,
                "bonus_2": 8,
                "bonus_3": 3,
                "substraction": -2500,
            },
            "ブーム天神",
            None,
        )
        self.assertEqual(row["total_games"], 4000)
        self.assertEqual(row["差玉"], -2500)
        self.assertEqual(row["art"], 3)

    def test_judgment_keeps_medal_diff(self) -> None:
        result = judge_record(
            {
                "hall_name": "ブーム天神",
                "fields": {
                    "machine_name": "ＬミリオンゴッドＣＸ",
                    "total_games": 4000,
                    "bb_count": 1,
                    "rb_count": 8,
                    "art_count": 3,
                    "差玉": -2500,
                    "dai_no": "10",
                },
            },
            load_masters(),
        )
        self.assertEqual(result["diff_medals"], -2500)
        self.assertEqual(result["hall_name"], "ブーム天神")
        self.assertEqual(result["total_games"], 4000)


if __name__ == "__main__":
    unittest.main()
