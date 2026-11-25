"""tools/ledger_cas.py のテスト。一時ディレクトリだけを使う。"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "ledger_cas.py"
HEAD = "ID,title,status,next_step,updated\n"
LEDGER = (
    HEAD
    + "T001,README更新,todo,節を書く,2026-01-08\n"
    + "T002,ログ解析の修正,doing,SECRET-NEXT,2026-01-10\n"
    + "T003,テスト追加,todo,,2026-01-09\n"
)


class LedgerCasTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.csv"
        self.ledger.write_bytes(LEDGER.encode("utf-8"))
        self.set_lock(by="worker-a", host="host-x")

    def set_lock(self, **fields):
        self.lock_path = self.root / "ledger.lock"
        self.lock_path.write_text(json.dumps(fields), encoding="utf-8")

    def patch(self, data):
        p = self.root / "patch.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def run_tool(self, data, by="worker-a", extra=()):
        env = dict(os.environ, WORKFLOW_HOST="host-x", PYTHONIOENCODING="utf-8")
        r = subprocess.run(
            [sys.executable, str(TOOL), str(self.patch(data)), "--by", by,
             "--root", str(self.root), *extra],
            capture_output=True, env=env, encoding="utf-8")
        return r.returncode, r.stdout + r.stderr

    def good(self):
        return {"updates": [{"id": "T002",
                             "cells": {"status": "review", "updated": "{TODAY}"},
                             "expect": {"status": "doing", "updated": "2026-01-10"}}]}

    def assert_refused(self, data, **kw):
        before = self.ledger.read_bytes()
        code, out = self.run_tool(data, **kw)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertNotIn("SECRET-NEXT", out)
        return out

    def test_passes_and_writes(self):
        code, out = self.run_tool(self.good())
        self.assertEqual(code, 0, out)
        text = self.ledger.read_text(encoding="utf-8")
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        self.assertIn(f"T002,ログ解析の修正,review,SECRET-NEXT,{today}\n", text)
        self.assertTrue(text.startswith(HEAD))
        self.assertNotIn("\r", text)
        self.assertEqual(text.splitlines()[1].split(",")[0], "T001")

    def test_empty_equals_missing(self):
        data = {"updates": [{"id": "T003", "cells": {"next_step": "x"},
                             "expect": {"next_step": None}}]}
        code, out = self.run_tool(data)
        self.assertEqual(code, 0, out)

    def test_mismatch_writes_nothing_and_hides_values(self):
        data = self.good()
        data["updates"][0]["expect"]["status"] = "todo"
        out = self.assert_refused(data)
        self.assertIn("T002 status", out)
        self.assertNotIn("doing", out)
        self.assertNotIn("review", out)

    def test_expect_missing_for_cells_column(self):
        data = self.good()
        del data["updates"][0]["expect"]["updated"]
        self.assert_refused(data)

    def test_empty_expect(self):
        data = self.good()
        data["updates"][0]["expect"] = {}
        self.assert_refused(data)

    def test_missing_id(self):
        data = self.good()
        data["updates"][0]["id"] = "T999"
        self.assert_refused(data)

    def test_duplicate_id_rows(self):
        self.ledger.write_bytes((LEDGER + "T002,重複,todo,,2026-01-01\n").encode("utf-8"))
        self.assert_refused(self.good())

    def test_unknown_column(self):
        data = self.good()
        data["updates"][0]["cells"]["owner"] = "a"
        data["updates"][0]["expect"]["owner"] = ""
        self.assert_refused(data)

    def test_append_existing_id(self):
        data = {"append": [{"ID": "T001", "title": "x"}]}
        self.assert_refused(data)

    def test_append_unknown_column(self):
        self.assert_refused({"append": [{"ID": "T010", "owner": "x"}]})

    def test_append_adds_row_with_today(self):
        data = {"append": [{"ID": "T005", "title": "CI設定", "status": "todo",
                            "next_step": "読む", "updated": "{TODAY}"}]}
        code, out = self.run_tool(data)
        self.assertEqual(code, 0, out)
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        self.assertTrue(self.ledger.read_text(encoding="utf-8")
                        .endswith(f"T005,CI設定,todo,読む,{today}\n"))

    def test_one_bad_update_blocks_all(self):
        data = self.good()
        data["updates"].append({"id": "T001", "cells": {"status": "done"},
                                "expect": {"status": "doing"}})
        self.assert_refused(data)

    def test_no_lock(self):
        self.lock_path.unlink()
        self.assert_refused(self.good())

    def test_lock_of_another_by(self):
        self.assert_refused(self.good(), by="worker-b")

    def test_lock_of_another_host(self):
        self.set_lock(by="worker-a", host="host-y")
        self.assert_refused(self.good())

    def test_released_lock(self):
        self.set_lock(by="worker-a", host="host-x", released=True)
        self.assert_refused(self.good())

    def test_dry_run_writes_nothing(self):
        before = self.ledger.read_bytes()
        code, out = self.run_tool(self.good(), extra=["--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertIn("T002", out)
        self.assertIn("status", out)
        self.assertNotIn("SECRET-NEXT", out)

    def test_dry_run_still_refuses(self):
        data = self.good()
        data["updates"][0]["expect"]["status"] = "todo"
        before = self.ledger.read_bytes()
        code, _ = self.run_tool(data, extra=["--dry-run"])
        self.assertEqual(code, 2)
        self.assertEqual(self.ledger.read_bytes(), before)


    def test_no_lock_message_says_so(self):
        self.lock_path.unlink()
        out = self.assert_refused(self.good())
        self.assertIn("台帳ロックがない", out)
        self.assertNotIn("指示を作り直す", out)

    def test_other_holder_message_says_so(self):
        out = self.assert_refused(self.good(), by="worker-b")
        self.assertIn("台帳ロックの担当が --by と違う", out)
        self.assertNotIn("指示を作り直す", out)

    def test_usage_error_exits_one(self):
        for args in ([], [str(self.root / "patch.json")], ["--no-such-option"]):
            r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True,
                               encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("使い方の誤り", r.stderr)

    def test_input_error_prints_reason(self):
        (self.root / "bad.json").write_text("{bad", encoding="utf-8")
        r = subprocess.run([sys.executable, str(TOOL), str(self.root / "bad.json"), "--by", "worker-a",
                            "--root", str(self.root)], capture_output=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("入力エラー", r.stdout)
        self.assertNotIn("JSONDecodeError", r.stdout)
        self.assertIn("line 1", r.stdout)


if __name__ == "__main__":
    unittest.main()
