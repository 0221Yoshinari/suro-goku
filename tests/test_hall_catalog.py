import json
import unittest
from pathlib import Path

from datetime import date

from src.dashboard.hall_admin import list_hall_rows, set_hall_enabled, user_data_state
from src.scraper.area_map import infer_area
from src.scraper.hall_catalog import (
    apply_catalog,
    archive_hall,
    archive_reason,
    brand_for_name,
    daidata_publication_state,
    extract_teramoba_urls,
    load_targets,
    matches_target,
    names_match,
    parse_daidata_store_list,
    parse_hackertarget_hosts,
    parse_teramoba_hall_page,
    reassign_hall_source,
)
from src.scraper.hall_rollout import rollout_candidates
from src.scraper.halls import area_presets
from src.scraper.halls import HALLS_CONFIG_PATH, load_halls_config

ROOT = Path(__file__).resolve().parents[1]

DAIDATA_HTML = """
<div id="contractStores">
  <table>
    <tbody>
      <tr>
        <td>
          <div class="store-wrapper">
            <a href="https://daidata.goraggio.com/100969">
              <h3>GOGOアリーナ天神</h3>
              <address>
                福岡県
                福岡市中央区天神2丁目6番37号
              </address>
            </a>
          </div>
        </td>
      </tr>
      <tr>
        <td>
          <a href="https://daidata.goraggio.com/200001">
            <h3>マルハン新宿東宝ビル店</h3>
            <address>
              東京都
              新宿区歌舞伎町1-1-1
            </address>
          </a>
        </td>
      </tr>
    </tbody>
  </table>
</div>
<div id="noContractStores">
  <a href="https://daidata.goraggio.com/999999">
    <h3>未公開ワンダーランド</h3>
    <address>東京都 どこか</address>
  </a>
</div>
"""

TERAMOBA_HTML = """
<div id="app" data-page="{&quot;component&quot;:&quot;rack_info_kt&quot;,&quot;props&quot;:{&quot;hall_informations&quot;:{&quot;hall_id&quot;:202,&quot;hall_code&quot;:&quot;honten&quot;,&quot;hall_name&quot;:&quot;プラザ本店&quot;,&quot;prefecture_cd&quot;:40,&quot;address&quot;:&quot;福岡市博多区西月隈3-6-17&quot;,&quot;domain&quot;:&quot;plaza-grp.pt.teramoba2.com&quot;}}}"></div>
"""


class HallCatalogParseTest(unittest.TestCase):
    def test_daidata_list_keeps_public_stores_only(self) -> None:
        rows = parse_daidata_store_list(DAIDATA_HTML, prefecture_name="福岡県")
        self.assertEqual([row["store_id"] for row in rows], ["100969", "200001", "999999"])
        self.assertEqual(rows[0]["source"], "daidata")
        self.assertTrue(rows[0]["data_public"])
        self.assertFalse(rows[2]["data_public"])
        self.assertEqual(rows[0]["prefecture"], "fukuoka")
        self.assertEqual(rows[0]["address"], "福岡市中央区天神2丁目6番37号")
        self.assertEqual(rows[1]["prefecture"], "tokyo")
        self.assertEqual(rows[1]["name"], "マルハン新宿東宝ビル店")

    def test_teramoba_page_reads_hall_informations(self) -> None:
        row = parse_teramoba_hall_page(
            TERAMOBA_HTML,
            "https://plaza-grp.pt.teramoba2.com/honten/rack_info_kt?kind_code=Y",
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["source"], "teramoba")
        self.assertEqual(row["name"], "プラザ本店")
        self.assertEqual(row["store_id"], "honten")
        self.assertEqual(row["hall_id"], 202)
        self.assertEqual(row["prefecture"], "fukuoka")
        self.assertEqual(row["kind_code"], "Y")
        self.assertEqual(row["host"], "plaza-grp.pt.teramoba2.com")

    def test_brand_filter_uses_prefecture_and_keyword(self) -> None:
        targets = load_targets(ROOT / "config" / "hall_targets.json")
        tokyo = {
            "name": "マルハン新宿東宝ビル店",
            "prefecture": "tokyo",
            "source": "daidata",
        }
        rural = {
            "name": "マルハン地方店",
            "prefecture": "aomori",
            "source": "daidata",
        }
        other = {"name": "地元ホール", "prefecture": "tokyo", "source": "daidata"}
        public = {"name": "メッセ新宿", "prefecture": "tokyo", "source": "daidata", "data_public": True}
        hidden = {"name": "地元ホール", "prefecture": "tokyo", "source": "daidata", "data_public": False}
        self.assertTrue(matches_target(tokyo, targets))
        limited = {**targets, "prefectures": ["tokyo"]}
        self.assertFalse(matches_target(rural, limited))
        self.assertFalse(matches_target(other, targets))
        self.assertFalse(matches_target(hidden, targets))
        self.assertTrue(matches_target(public, targets))
        self.assertEqual(brand_for_name("１２３博多店", targets), "123")
        self.assertEqual(brand_for_name("ISLAND 秋葉原店", targets), "island")
        self.assertEqual(brand_for_name("メガガイア蒲田店", targets), "maruhan")
        self.assertEqual(brand_for_name("ミクちゃんガイア三宮店", targets), "gaia")
        teramoba = {
            "name": "グランドシップ",
            "prefecture": "tokyo",
            "source": "teramoba",
        }
        self.assertTrue(matches_target(teramoba, targets))


class HallCatalogApplyTest(unittest.TestCase):
    def test_apply_adds_matching_hall_without_touching_existing(self) -> None:
        targets = {
            "register_enabled": False,
            "prefectures": ["tokyo"],
            "brands": [{"id": "maruhan", "keywords": ["マルハン"]}],
        }
        master = {
            "halls": [
                {
                    "name": "マルハン新宿東宝ビル店",
                    "prefecture": "tokyo",
                    "address": "新宿区",
                    "store_id": "200001",
                    "hall_id": None,
                    "url": "https://daidata.goraggio.com/200001",
                    "source": "daidata",
                    "host": "daidata.goraggio.com",
                },
                {
                    "name": "GOGOアリーナ天神",
                    "prefecture": "fukuoka",
                    "store_id": "100969",
                    "url": "https://daidata.goraggio.com/100969",
                    "source": "daidata",
                    "host": "daidata.goraggio.com",
                },
            ]
        }
        halls = {
            "halls": [
                {
                    "id": "gogo-arena-tenjin",
                    "name": "GOGOアリーナ天神",
                    "enabled": True,
                    "prefecture": "fukuoka",
                    "area": "fukuoka_central",
                    "source": "goraggio",
                    "source_url": "https://daidata.goraggio.com/100969",
                    "goraggio": {"hall_code": "100969"},
                }
            ]
        }
        summary = apply_catalog(master, halls, targets)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["unchanged"], 0)
        added = halls["halls"][1]
        self.assertEqual(added["id"], "daidata-200001")
        self.assertFalse(added["enabled"])
        self.assertEqual(added["source"], "goraggio")
        self.assertEqual(added["group"], "maruhan")
        self.assertEqual(added["prefecture"], "tokyo")
        self.assertEqual(added["area"], "tokyo_shinjuku")
        self.assertEqual(added["goraggio"]["hall_code"], "200001")
        self.assertTrue(halls["halls"][0]["enabled"])
        self.assertEqual(halls["halls"][0]["area"], "fukuoka_central")

    def test_public_hall_outside_brand_is_added_enabled(self) -> None:
        targets = {
            "register_enabled": False,
            "prefectures": ["tokyo", "chiba"],
            "brands": [
                {"id": "maruhan", "keywords": ["マルハン", "メガガイア"]},
                {"id": "island", "keywords": ["ISLAND"]},
            ],
        }
        master = {
            "halls": [
                {
                    "name": "メッセ新宿",
                    "prefecture": "tokyo",
                    "address": "新宿区歌舞伎町1-1-1",
                    "store_id": "100001",
                    "url": "https://daidata.goraggio.com/100001",
                    "source": "daidata",
                    "data_public": True,
                },
                {
                    "name": "ISLAND 秋葉原店",
                    "prefecture": "tokyo",
                    "address": "千代田区外神田1-1-1",
                    "store_id": "100002",
                    "url": "https://daidata.goraggio.com/100002",
                    "source": "daidata",
                    "data_public": False,
                },
                {
                    "name": "近所のホール",
                    "prefecture": "chiba",
                    "store_id": "100003",
                    "url": "https://daidata.goraggio.com/100003",
                    "source": "daidata",
                    "data_public": False,
                },
            ]
        }
        halls = {"halls": []}
        summary = apply_catalog(master, halls, targets)
        by_name = {item["name"]: item for item in halls["halls"]}
        self.assertEqual(summary["added"], 2)
        self.assertTrue(by_name["メッセ新宿"]["enabled"])
        self.assertEqual(by_name["メッセ新宿"]["group"], "other")
        self.assertEqual(by_name["メッセ新宿"]["area"], "tokyo_shinjuku")
        self.assertFalse(by_name["ISLAND 秋葉原店"]["enabled"])
        self.assertEqual(by_name["ISLAND 秋葉原店"]["group"], "island")
        self.assertTrue(by_name["ISLAND 秋葉原店"]["data_public"] is False)
        self.assertNotIn("近所のホール", by_name)


class AreaAndRolloutTest(unittest.TestCase):
    def test_tokyo_areas_are_japanese_and_selectable(self) -> None:
        load_halls_config.cache_clear()
        tokyo = [item for item in area_presets() if item["prefecture"] == "tokyo"]
        labels = [item["label"] for item in tokyo]
        self.assertIn("新宿・渋谷・中野", labels)
        self.assertIn("池袋・豊島・板橋・練馬", labels)
        self.assertTrue(any(item["id"] == "all" and "東京都" in item["label"] for item in tokyo))
        self.assertNotIn("tokyo", labels)
        self.assertEqual(infer_area("tokyo", "新宿区歌舞伎町1-23-3", "エスパス日拓新宿歌舞伎町店"), "tokyo_shinjuku")
        self.assertEqual(infer_area("fukuoka", "福岡市博多区西月隈3-6-17", "プラザ本店"), "fukuoka_central")

    def test_rollout_skips_unpublished_and_enabled(self) -> None:
        rows = rollout_candidates(
            {
                "halls": [
                    {"id": "a", "name": "公開店", "enabled": False, "source": "goraggio", "data_public": True, "prefecture": "tokyo"},
                    {"id": "b", "name": "未公開店", "enabled": False, "source": "goraggio", "data_public": False, "prefecture": "osaka"},
                    {"id": "c", "name": "取得中", "enabled": True, "source": "goraggio", "data_public": True, "prefecture": "fukuoka"},
                    {"id": "d", "name": "閉店店", "enabled": False, "archived": True, "source": "goraggio", "data_public": True, "prefecture": "tokyo"},
                ]
            }
        )
        self.assertEqual([item["id"] for item in rows], ["a"])


class SourceRepairTest(unittest.TestCase):
    def test_publication_state_prefers_machine_links(self) -> None:
        self.assertEqual(daidata_publication_state('<a href="/100949/unit_list?x=1">機種</a>台データは更新されていません'), "live")
        self.assertEqual(daidata_publication_state("<dd>台データは更新されていません</dd>"), "stale")
        self.assertEqual(daidata_publication_state("<form>利用規約に同意する</form>"), "live")
        self.assertEqual(daidata_publication_state("<p>導入リクエスト</p>"), "unpublished")

    def test_names_and_search_links(self) -> None:
        self.assertTrue(names_match("グランドシップ", "グランド シップ"))
        self.assertFalse(names_match("グランドホール", "グランドホール港南店"))
        urls = extract_teramoba_urls(
            'uddg=https%3A%2F%2Fgramoba.pt.teramoba2.com%2Fship%2F&amp;'
            '<a href="https://gramoba.pt.teramoba2.com/ship/rack_info_kt?kind_code=I">x</a>'
        )
        self.assertEqual(urls, ["https://gramoba.pt.teramoba2.com/ship/rack_info_kt?kind_code=21", "https://gramoba.pt.teramoba2.com/ship/rack_info_kt?kind_code=I"])
        self.assertEqual(parse_hackertarget_hosts("example.com,1.2.3.4\ngramoba.pt.teramoba2.com,9.9.9.9\n"), ["gramoba.pt.teramoba2.com"])

    def test_reassign_keeps_id_and_archive_blocks_reenable(self) -> None:
        targets = {"brands": [], "prefectures": ["tokyo"]}
        hall = {
            "id": "daidata-100673",
            "name": "グランドシップ",
            "aliases": ["グランドシップ"],
            "enabled": True,
            "group": "other",
            "prefecture": "tokyo",
            "area": "tokyo_east",
            "source": "goraggio",
            "source_url": "https://daidata.goraggio.com/100673",
            "goraggio": {"hall_code": "100673"},
            "data_public": True,
        }
        record = {
            "name": "グランドシップ",
            "prefecture": "tokyo",
            "address": "江東区東雲1-10-22",
            "store_id": "ship",
            "hall_id": 9,
            "url": "https://gramoba.pt.teramoba2.com/ship/rack_info_kt?kind_code=21",
            "source": "teramoba",
            "host": "gramoba.pt.teramoba2.com",
            "kind_code": "21",
        }
        self.assertTrue(reassign_hall_source(hall, record, targets))
        self.assertEqual(hall["id"], "daidata-100673")
        self.assertEqual(hall["source"], "teramoba")
        self.assertEqual(hall["teramoba"]["department"], "ship")
        self.assertNotIn("goraggio", hall)
        self.assertTrue(hall["enabled"])

        closed = {
            "id": "daidata-203350",
            "name": "ガイア瑞江店",
            "enabled": False,
            "group": "gaia",
            "prefecture": "tokyo",
            "source": "goraggio",
            "data_public": False,
            "goraggio": {"hall_code": "203350"},
        }
        reason = archive_reason(closed)
        self.assertIsNotNone(reason)
        assert reason is not None
        archive_hall(closed, reason)
        master = {
            "halls": [
                {
                    "name": "ガイア瑞江店",
                    "prefecture": "tokyo",
                    "store_id": "203350",
                    "url": "https://daidata.goraggio.com/203350",
                    "source": "daidata",
                    "data_public": True,
                }
            ]
        }
        summary = apply_catalog(master, {"halls": [closed]}, targets)
        self.assertEqual(summary["added"], 0)
        self.assertTrue(closed["archived"])
        self.assertFalse(closed["enabled"])


class HallAdminTest(unittest.TestCase):
    def test_toggle_writes_enabled_flag(self) -> None:
        path = ROOT / "data" / "masters" / "halls-admin-test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "halls": [
                        {
                            "id": "sample-hall",
                            "name": "サンプルホール",
                            "enabled": False,
                            "prefecture": "tokyo",
                            "area": "tokyo",
                            "group": "maruhan",
                            "source": "goraggio",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            saved = set_hall_enabled("sample-hall", True, path=path)
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["prefecture_label"], "東京都")
            self.assertEqual(saved["source_label"], "台データオンライン")
            self.assertEqual(saved["status"], "pending")
            again = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(again["halls"][0]["enabled"])
        finally:
            path.unlink(missing_ok=True)
            load_halls_config.cache_clear()

    def test_user_status_labels_hide_internal_errors(self) -> None:
        day = date(2026, 10, 6)
        self.assertEqual(user_data_state("ok", "2026-10-06T01:00:00+00:00", today=day)["label"], "データ更新済み")
        self.assertEqual(user_data_state("ok", "2026-10-05T01:00:00+00:00", today=day)["label"], "次回更新待ち")
        self.assertEqual(user_data_state("pending", None, today=day)["label"], "順次データ反映中")
        preparing = user_data_state("error", "2026-10-06T01:00:00+00:00", today=day)
        self.assertEqual(preparing["label"], "更新準備中")
        self.assertNotIn("エラー", preparing["label"])

    def test_live_rows_include_registered_halls(self) -> None:
        rows = list_hall_rows()
        ids = {item["id"] for item in rows["halls"]}
        self.assertIn("gogo-arena-tenjin", ids)
        self.assertGreaterEqual(rows["count"], 1)
        self.assertTrue(HALLS_CONFIG_PATH.is_file())
