from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dashboard.members import grant_checkout
from src.dashboard.members import open_member_session


class MemberStoreTest(unittest.TestCase):
    def test_sqlite_url_replaces_the_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "members.sqlite"
            json_path = root / "members.json"
            with mock.patch.dict(
                os.environ,
                {
                    "DATABASE_URL": f"sqlite:///{database}",
                    "MEMBER_STORE_PATH": str(json_path),
                },
            ):
                saved = grant_checkout(
                    when=1_700_000_000,
                    session_id="cs_sqlite_abcdefgh",
                    customer_id="cus_sqlite",
                    subscription_id="sub_sqlite",
                    email="sqlite@example.com",
                )
                self.assertTrue(database.is_file())
                self.assertFalse(json_path.exists())
                member_id, session_id, error = open_member_session(
                    saved["license_key"],
                    "127.0.0.1",
                    "fp-sqlite",
                    now=1_700_000_100,
                )
            self.assertEqual(error, "")
            self.assertTrue(member_id.startswith("m_"))
            self.assertTrue(session_id)

    def test_unreachable_postgres_falls_back_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / "fallback.sqlite"
            with mock.patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgresql://127.0.0.1:1/suro_goku_missing",
                    "MEMBER_SQLITE_PATH": str(fallback),
                },
            ):
                from src.dashboard import member_db

                member_db._FAILED_POSTGRES.discard("postgresql://127.0.0.1:1/suro_goku_missing")
                saved = grant_checkout(
                    when=1_700_000_000,
                    session_id="cs_fallback_abcdefgh",
                    customer_id="cus_fallback",
                    email="fallback@example.com",
                )
                self.assertTrue(fallback.is_file())
                self.assertIn("sqlite", member_db.describe_backend())
                member_id, _session_id, error = open_member_session(
                    saved["license_key"],
                    "127.0.0.1",
                    "fp-fallback",
                    now=1_700_000_100,
                )
            self.assertEqual(error, "")
            self.assertTrue(member_id.startswith("m_"))

    def test_existing_sqlite_gains_account_columns(self) -> None:
        import sqlite3

        from src.dashboard.member_db import load_members

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "old.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE members (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL DEFAULT '',
                  license_key TEXT NOT NULL DEFAULT '',
                  stripe_customer_id TEXT NOT NULL DEFAULT '',
                  stripe_subscription_id TEXT NOT NULL DEFAULT '',
                  checkout_session_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'incomplete',
                  status_source TEXT NOT NULL DEFAULT '',
                  status_event_at BIGINT NOT NULL DEFAULT 0,
                  trial_end BIGINT,
                  current_period_end BIGINT,
                  active_session_id TEXT NOT NULL DEFAULT '',
                  session_seen BIGINT NOT NULL DEFAULT 0,
                  session_ip TEXT NOT NULL DEFAULT '',
                  session_fp TEXT NOT NULL DEFAULT '',
                  created_at BIGINT NOT NULL DEFAULT 0,
                  updated_at BIGINT NOT NULL DEFAULT 0
                );
                CREATE TABLE stripe_events (event_id TEXT PRIMARY KEY);
                """
            )
            connection.execute(
                "INSERT INTO members (id, email, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("m_0123456789abcdef", "old@example.com", "active", 10, 10),
            )
            connection.commit()
            connection.close()
            with mock.patch.dict(os.environ, {"DATABASE_URL": f"sqlite:///{database}"}):
                from src.dashboard import member_db

                member_db._READY_SQLITE.discard(str(database.resolve()))
                data = load_members()
            member = data["members"][0]
            self.assertEqual(member["email"], "old@example.com")
            self.assertEqual(member["status"], "active")
            self.assertTrue(member["is_active"])
            self.assertEqual(member["password_hash"], "")


class ViewerSeedTest(unittest.TestCase):
    def test_existing_email_gets_a_new_password_and_stays_active(self) -> None:
        from src.dashboard.members import ensure_viewer_account
        from src.dashboard.members import open_password_session
        from src.dashboard.members import present_member

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"DATABASE_URL": "", "MEMBER_STORE_PATH": str(Path(tmp) / "members.json")},
            ):
                ensure_viewer_account("viewer@example.com", "first-pass-1")
                second = ensure_viewer_account("viewer@example.com", "second-pass-2")
                member_id, _session_id, error = open_password_session(
                    "viewer@example.com",
                    "second-pass-2",
                    "127.0.0.1",
                    "fp-seed",
                )
                view = present_member(member_id)
                stored = (Path(tmp) / "members.json").read_text(encoding="utf-8")
        self.assertEqual(second, "updated")
        self.assertEqual(error, "")
        self.assertTrue(view["is_active"])
        self.assertTrue(view["entitled"])
        self.assertNotIn("second-pass-2", stored)
        self.assertIn("pbkdf2$", stored)


if __name__ == "__main__":
    unittest.main()
