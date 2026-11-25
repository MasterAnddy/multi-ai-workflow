# -*- coding: utf-8 -*-
"""tools/target_lock.py のテスト。一時ディレクトリだけを使い、サブプロセスで実行する。"""

import datetime
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "target_lock.py")


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def iso(minutes_ago=0):
    t = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat(timespec="seconds")


class TargetLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ledger = os.path.join(self.root, "ledger.csv")

    def run_tool(self, *args, host="host-a", encoding=None):
        env = dict(os.environ, WORKFLOW_ROOT=self.root, WORKFLOW_HOST=host, PYTHONIOENCODING="utf-8")
        if encoding:
            env["PYTHONIOENCODING"] = encoding
        return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def lock_path(self, target_id="T001"):
        return os.path.join(self.root, "active", f"{target_id}.json")

    def write_lock(self, target_id="T001", by="worker-a", host="host-a", minutes_ago=0, **extra):
        data = {"id": target_id, "by": by, "host": host, "note": "", "started": iso(minutes_ago),
                "beat": iso(minutes_ago)}
        data.update(extra)
        os.makedirs(os.path.join(self.root, "active"), exist_ok=True)
        with open(self.lock_path(target_id), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def write_ledger_lock(self, by="worker-a", host="host-a", minutes_ago=0):
        data = {"by": by, "host": host, "purpose": "テスト", "started": iso(minutes_ago),
                "beat": iso(minutes_ago), "ledger_sha256_before": None, "backup": None}
        with open(os.path.join(self.root, "ledger.lock"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def write_ledger(self, text="ID,status\nT001,open\n"):
        with open(self.ledger, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def closed(self, kind):
        return glob.glob(os.path.join(self.root, "closed", f"{kind}_*.json"))

    # ---- 対象ロック

    def test_claim_creates_lock(self):
        r = self.run_tool("claim", "T001", "--by", "worker-a", "--note", "確認")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.lock_path(), encoding="utf-8") as f:
            lock = json.load(f)
        self.assertEqual((lock["by"], lock["host"], lock["note"]), ("worker-a", "host-a", "確認"))

    def test_claim_refused_when_live(self):
        self.write_lock(by="worker-a")
        r = self.run_tool("claim", "T001", "--by", "reviewer-b")
        self.assertEqual(r.returncode, 2)
        self.assertIn("worker-a", r.stdout)

    def test_claim_refused_when_stale_and_prints_takeover(self):
        self.write_lock(minutes_ago=60)
        r = self.run_tool("claim", "T001", "--by", "reviewer-b")
        self.assertEqual(r.returncode, 2)
        self.assertIn("takeover T001", r.stdout)

    def test_claim_warns_same_label_on_other_target(self):
        self.write_lock("T001", by="worker-a")
        r = self.run_tool("claim", "T002", "--by", "worker-a")
        self.assertEqual(r.returncode, 0)
        self.assertIn("警告", r.stdout)
        self.assertTrue(os.path.exists(self.lock_path("T002")))

    def test_claim_rejects_bad_id(self):
        self.assertEqual(self.run_tool("claim", "X1", "--by", "worker-a").returncode, 1)

    def test_beat_by_holder_updates(self):
        self.write_lock(minutes_ago=30)
        r = self.run_tool("beat", "T001", "--by", "worker-a")
        self.assertEqual(r.returncode, 0)
        with open(self.lock_path(), encoding="utf-8") as f:
            beat = datetime.datetime.fromisoformat(json.load(f)["beat"])
        self.assertLess((datetime.datetime.now().astimezone() - beat).total_seconds(), 120)

    def test_beat_by_non_holder_refused(self):
        self.write_lock(by="worker-a")
        r = self.run_tool("beat", "T001", "--by", "reviewer-b")
        self.assertEqual(r.returncode, 2)

    def test_same_label_on_other_host_refused(self):
        self.write_lock(by="worker-a", host="host-a")
        for cmd in (["beat", "T001"], ["done", "T001"], ["stop", "T001"]):
            r = self.run_tool(*cmd, "--by", "worker-a", host="host-b")
            self.assertEqual(r.returncode, 2, cmd)
        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertEqual(self.closed("DONE") + self.closed("STOPPED"), [])

    def test_beat_does_not_recreate_released_lock(self):
        self.write_lock(released=True, released_at=iso(), released_why="テスト")
        before = read_text(self.lock_path())
        r = self.run_tool("beat", "T001", "--by", "worker-a")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(read_text(self.lock_path()), before)
        self.assertEqual(self.run_tool("claim", "T001", "--by", "reviewer-b").returncode, 0)

    def test_beat_without_lock_does_not_create(self):
        self.assertEqual(self.run_tool("beat", "T001", "--by", "worker-a").returncode, 1)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_done_by_holder_writes_closed_and_releases(self):
        self.write_lock()
        r = self.run_tool("done", "T001", "--by", "worker-a", "--note", "完了")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.lock_path()))
        (rec,) = self.closed("DONE")
        with open(rec, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["kind"], data["note"], data["by"]), ("DONE", "完了", "worker-a"))
        self.assertIn("closed_at", data)

    def test_stop_by_holder_writes_closed_and_releases(self):
        self.write_lock()
        r = self.run_tool("stop", "T001", "--by", "worker-a", "--reason", "入力待ち")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.lock_path()))
        self.assertEqual(len(self.closed("STOPPED")), 1)

    def test_done_stop_by_non_holder_refused_even_when_stale(self):
        for minutes in (0, 90):
            self.write_lock(minutes_ago=minutes)
            for cmd in (["done", "T001", "--note", "x"], ["stop", "T001", "--reason", "x"]):
                r = self.run_tool(*cmd, "--by", "reviewer-b")
                self.assertEqual(r.returncode, 2, (minutes, cmd))
                self.assertTrue(os.path.exists(self.lock_path()))
        self.assertEqual(self.closed("DONE") + self.closed("STOPPED"), [])

    def test_takeover_stale_writes_audit_and_releases(self):
        self.write_lock(minutes_ago=60, note="元のメモ")
        r = self.run_tool("takeover", "T001", "--by", "reviewer-b", "--why", "停止を確認")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(self.lock_path()))
        (rec,) = self.closed("TAKEOVER")
        with open(rec, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["kind"], data["by"], data["note"], data["taken_over_by"]),
                         ("TAKEOVER", "worker-a", "停止を確認", "reviewer-b"))
        self.assertEqual(data["id"], "T001")
        self.assertEqual(self.run_tool("claim", "T001", "--by", "reviewer-b").returncode, 0)

    def test_takeover_live_refused(self):
        self.write_lock(minutes_ago=5)
        r = self.run_tool("takeover", "T001", "--by", "reviewer-b", "--why", "x")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertEqual(self.closed("TAKEOVER"), [])

    def test_status_lists_live_and_stale(self):
        self.write_lock("T001", minutes_ago=1)
        self.write_lock("T002", by="reviewer-b", minutes_ago=90)
        self.write_ledger_lock()
        out = self.run_tool("status").stdout
        self.assertIn("T001", out)
        self.assertIn("有効", out)
        self.assertIn("STALE", out)
        self.assertIn("台帳ロック", out)

    # ---- 台帳ロック

    def test_ledger_lock_creates_backup_and_records_sha(self):
        self.write_ledger()
        r = self.run_tool("ledger-lock", "--by", "worker-a", "--purpose", "記入")
        self.assertEqual(r.returncode, 0, r.stdout)
        backups = glob.glob(os.path.join(self.root, "backups", "ledger_*.csv"))
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.root, "ledger.lock"), encoding="utf-8") as f:
            lock = json.load(f)
        self.assertEqual(lock["backup"], os.path.basename(backups[0]))
        self.assertEqual(len(lock["ledger_sha256_before"]), 64)
        self.assertEqual(read_text(backups[0]), read_text(self.ledger))

    def test_ledger_lock_refused_when_live_or_stale(self):
        self.write_ledger_lock(minutes_ago=1)
        self.assertEqual(self.run_tool("ledger-lock", "--by", "reviewer-b").returncode, 2)
        self.write_ledger_lock(minutes_ago=30)
        r = self.run_tool("ledger-lock", "--by", "reviewer-b")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ledger-takeover", r.stdout)

    def test_ledger_unlock_by_holder_shows_hashes(self):
        self.write_ledger()
        self.run_tool("ledger-lock", "--by", "worker-a", "--purpose", "記入")
        self.write_ledger("ID,status\nT001,done\n")
        r = self.run_tool("ledger-unlock", "--by", "worker-a")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, "ledger.lock")))
        self.assertIn("書き込み前", r.stdout)
        self.assertNotIn("前後が同じ", r.stdout)
        self.assertEqual(len(glob.glob(os.path.join(self.root, "backups", "ledger_*.csv"))), 2)

    def test_ledger_unlock_warns_when_unchanged(self):
        self.write_ledger()
        self.run_tool("ledger-lock", "--by", "worker-a")
        r = self.run_tool("ledger-unlock", "--by", "worker-a")
        self.assertEqual(r.returncode, 0)
        self.assertIn("前後が同じ", r.stdout)

    def test_ledger_unlock_beat_by_non_holder_refused_and_lock_kept(self):
        self.write_ledger_lock(by="worker-a")
        for cmd in ("ledger-unlock", "ledger-beat"):
            r = self.run_tool(cmd, "--by", "reviewer-b")
            self.assertEqual(r.returncode, 2, cmd)
        self.assertTrue(os.path.exists(os.path.join(self.root, "ledger.lock")))

    def test_ledger_unlock_other_host_refused_even_when_stale(self):
        self.write_ledger_lock(by="worker-a", host="host-a", minutes_ago=60)
        r = self.run_tool("ledger-unlock", "--by", "worker-a", host="host-b")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ledger-takeover", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.root, "ledger.lock")))

    def test_ledger_takeover_stale_and_live(self):
        self.write_ledger()
        self.write_ledger_lock(minutes_ago=1)
        self.assertEqual(self.run_tool("ledger-takeover", "--by", "reviewer-b", "--why", "x").returncode, 2)
        self.write_ledger_lock(minutes_ago=30)
        r = self.run_tool("ledger-takeover", "--by", "reviewer-b", "--why", "停止を確認")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, "ledger.lock")))
        (rec,) = self.closed("TAKEOVER")
        with open(rec, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual((data["kind"], data["id"], data["by"]), ("TAKEOVER", "LEDGER", "worker-a"))

    def test_restore_list_and_restore(self):
        self.write_ledger("ID,status\nT001,open\n")
        self.run_tool("ledger-lock", "--by", "worker-a")
        self.write_ledger("ID,status\nT001,broken\n")
        self.run_tool("ledger-unlock", "--by", "worker-a")
        self.assertEqual(self.run_tool("ledger-lock", "--by", "worker-a").returncode, 0)
        listing = self.run_tool("restore", "--list")
        self.assertEqual(listing.returncode, 0)
        name = os.path.basename(sorted(glob.glob(os.path.join(self.root, "backups", "ledger_*.csv")))[0])
        self.assertIn(name, listing.stdout)
        self.assertEqual(self.run_tool("restore", name, "--by", "worker-a").returncode, 0)
        self.assertIn("T001,open", read_text(self.ledger))
        self.assertEqual(self.run_tool("restore", "..\\x.csv").returncode, 1)

    # ---- 引数の誤りと --root の位置

    def test_usage_errors_exit_one(self):
        cases = [[], ["claim", "T001"], ["claim"], ["status", "--no-such-option"], ["no-such-command"],
                 ["ledger-lock"]]
        for args in cases:
            with self.subTest(args=args):
                r = self.run_tool(*args)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("使い方の誤り", r.stderr)

    def test_root_and_ledger_accepted_after_subcommand(self):
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        env = dict(os.environ, WORKFLOW_HOST="host-a", PYTHONIOENCODING="utf-8")
        env.pop("WORKFLOW_ROOT", None)

        def run(*args):
            return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True,
                                  text=True, encoding="utf-8")

        r = run("claim", "T001", "--by", "worker-a", "--root", other)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(other, "active", "T001.json")))
        self.assertFalse(os.path.exists(self.lock_path()))
        csv_path = os.path.join(other, "x.csv")
        with open(csv_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("ID,status\nT001,open\n")
        r = run("ledger-lock", "--by", "worker-a", "--root", other, "--ledger", csv_path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lock = json.loads(read_text(os.path.join(other, "ledger.lock")))
        self.assertEqual(len(lock["ledger_sha256_before"]), 64)

    # ---- 原子的な作成と restore の保持者検査

    def test_create_lock_does_not_overwrite_existing_file(self):
        spec = importlib.util.spec_from_file_location("target_lock", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.write_lock(by="worker-a")
        before = read_text(self.lock_path())
        self.assertFalse(mod.create_exclusive(self.lock_path(), {"id": "T001", "by": "reviewer-b"}))
        self.assertFalse(mod.create_lock(self.lock_path(), {"id": "T001", "by": "reviewer-b"}))
        self.assertEqual(read_text(self.lock_path()), before)

    def test_claim_replaces_released_marker(self):
        self.write_lock(released=True, released_at=iso(), released_why="テスト")
        self.assertEqual(self.run_tool("claim", "T001", "--by", "reviewer-b").returncode, 0)
        lock = json.loads(read_text(self.lock_path()))
        self.assertEqual(lock["by"], "reviewer-b")
        self.assertNotIn("released", lock)

    def test_ledger_lock_replaces_released_marker(self):
        path = os.path.join(self.root, "ledger.lock")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"by": "worker-a", "host": "host-a", "released": True}, f)
        self.assertEqual(self.run_tool("ledger-lock", "--by", "reviewer-b").returncode, 0)
        lock = json.loads(read_text(path))
        self.assertEqual(lock["by"], "reviewer-b")
        self.assertNotIn("released", lock)

    def test_restore_requires_ledger_lock_holder(self):
        self.write_ledger("ID,status\nT001,open\n")
        self.run_tool("ledger-lock", "--by", "worker-a")
        self.run_tool("ledger-unlock", "--by", "worker-a")
        name = os.path.basename(sorted(glob.glob(os.path.join(self.root, "backups", "ledger_*.csv")))[0])
        self.write_ledger("ID,status\nT001,changed\n")
        # ロックなし
        self.assertEqual(self.run_tool("restore", name, "--by", "worker-a").returncode, 1)
        self.assertEqual(self.run_tool("restore", name).returncode, 1)
        # 他の担当が保持
        self.write_ledger_lock(by="worker-a")
        self.assertEqual(self.run_tool("restore", name, "--by", "reviewer-b").returncode, 2)
        self.assertEqual(self.run_tool("restore", name).returncode, 2)
        self.assertIn("T001,changed", read_text(self.ledger))
        # 保持者
        self.assertEqual(self.run_tool("restore", name, "--by", "worker-a").returncode, 0)
        self.assertIn("T001,open", read_text(self.ledger))

    # ---- コンソールの文字コード

    def test_ledger_lock_unlock_exit_zero_under_legacy_encodings(self):
        for enc in ("cp932", "gbk"):
            with self.subTest(encoding=enc):
                self.write_ledger()
                r1 = self.run_tool("ledger-lock", "--by", "worker-a", "--purpose", "記入", encoding=enc)
                self.assertEqual(r1.returncode, 0, r1.stderr)
                r2 = self.run_tool("ledger-unlock", "--by", "worker-a", encoding=enc)
                self.assertEqual(r2.returncode, 0, r2.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.root, "ledger.lock")))

    def test_every_command_exit_codes_under_cp932(self):
        self.write_ledger()
        steps = [
            (["status"], 0),
            (["claim", "T001", "--by", "worker-a", "--note", "確認"], 0),
            (["claim", "T001", "--by", "reviewer-b"], 2),
            (["beat", "T001", "--by", "worker-a"], 0),
            (["beat", "T001", "--by", "reviewer-b"], 2),
            (["takeover", "T001", "--by", "reviewer-b", "--why", "x"], 2),
            (["done", "T001", "--by", "worker-a", "--note", "完了"], 0),
            (["claim", "T002", "--by", "worker-a"], 0),
            (["stop", "T002", "--by", "worker-a", "--reason", "待ち"], 0),
            (["ledger-lock", "--by", "worker-a", "--purpose", "記入"], 0),
            (["ledger-lock", "--by", "reviewer-b"], 2),
            (["ledger-beat", "--by", "worker-a"], 0),
            (["ledger-takeover", "--by", "reviewer-b", "--why", "x"], 2),
            (["ledger-unlock", "--by", "worker-a"], 0),
            (["restore", "--list"], 0),
            (["beat", "T009", "--by", "worker-a"], 1),
        ]
        for args, expected in steps:
            r = self.run_tool(*args, encoding="cp932")
            self.assertEqual(r.returncode, expected, f"{args}: {r.stdout}{r.stderr}")
        self.write_lock("T003", minutes_ago=60)
        r = self.run_tool("takeover", "T003", "--by", "reviewer-b", "--why", "停止", encoding="cp932")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
