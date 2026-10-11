import unittest

from src.notify.discord import build_success_content


class DiscordSuccessContentTest(unittest.TestCase):
    def test_completion_is_compact_counts(self) -> None:
        judgment = {
            "judged_at": "2026-09-26T09:11:00+00:00",
            "target_halls": ["玉屋∞本店", "玉屋666唐津"],
            "failed_halls": [],
        }
        text = build_success_content(judgment, elapsed_sec=90, target_halls=["玉屋∞本店", "玉屋666唐津"])
        self.assertEqual(
            text,
            "\n".join(
                [
                    "📊 **【台データ取得完了】**",
                    "・日時: 2026-09-26 18:11",
                    "・成功: **2** 店舗",
                    "・失敗: **0** 店舗",
                ]
            ),
        )
        self.assertNotIn("全6数値", text)
        self.assertNotIn("ダッシュボード", text)

    def test_failures_list_short_reasons_only(self) -> None:
        judgment = {
            "judged_at": "2026-09-26T09:11:00+00:00",
            "failed_halls": ["店舗名A", "店舗名B"],
            "failure_details": [
                {"hall": "店舗名A", "reason": "TimeoutError: timed out after 90s\nTraceback (most recent call last):\n  File x"},
                {"hall": "店舗名B", "reason": "HTTP 404 Not Found"},
            ],
        }
        text = build_success_content(judgment, target_halls=["玉屋∞本店", "店舗名A", "店舗名B"])
        self.assertIn("・成功: **1** 店舗", text)
        self.assertIn("・失敗: **2** 店舗", text)
        self.assertIn("⚠️ **失敗内訳**", text)
        self.assertIn("・店舗名A: [理由] タイムアウト（応答なし）", text)
        self.assertIn("・店舗名B: [理由] ページ構造変更 / 404エラー", text)
        self.assertNotIn("Traceback", text)
