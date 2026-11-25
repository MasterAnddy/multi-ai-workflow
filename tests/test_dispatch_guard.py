# -*- coding: utf-8 -*-
"""dispatch_guard のテスト。一時ディレクトリだけを使う。"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "tools", "dispatch_guard.py")
MARK = "TASK-20261201-AAA"


def iso(delta=datetime.timedelta(0)):
    return (datetime.datetime.now().astimezone() - delta).isoformat(timespec="seconds")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "wf")
        os.makedirs(os.path.join(self.root, "active"))
        os.makedirs(os.path.join(self.root, "closed"))
        self.n = 0

    def instruction(self, body, marker=MARK):
        self.n += 1
        path = os.path.join(self._tmp.name, f"i{self.n}.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{marker}\n{body}\n")
        return path

    def run_guard(self, path, *extra):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("WORKFLOW_ROOT", None)
        proc = subprocess.run([sys.executable, TOOL, "--file", path, "--root", self.root, *extra],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        return proc.returncode, proc.stdout + proc.stderr

    def lock(self, target, by, beat_ago=datetime.timedelta(minutes=1), released=False):
        data = {"id": target, "by": by, "host": "h", "note": "", "started": iso(beat_ago),
                "beat": iso(beat_ago)}
        if released:
            data["released"] = True
        with open(os.path.join(self.root, "active", target + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def closed(self, kind, target, by, ago):
        when = datetime.datetime.now().astimezone() - ago
        name = f"{kind}_{target}_{by}_{when:%Y%m%d_%H%M%S}.json"
        data = {"id": target, "by": by, "kind": kind, "closed_at": when.isoformat(), "note": ""}
        with open(os.path.join(self.root, "closed", name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def sent_text(self, marker=MARK):
        with open(os.path.join(self.root, "inbox", marker + ".txt"), encoding="utf-8") as f:
            return f.read()

    def test_clean_pass(self):
        code, out = self.run_guard(self.instruction("T001 の表示を直す。台帳を更新する。"))
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "inbox")))

    def test_send_writes_inbox_and_log_source_unchanged(self):
        path = self.instruction("T001 の表示を直す。台帳を更新する。")
        with open(path, "rb") as f:
            before = f.read()
        code, out = self.run_guard(path, "--send")
        self.assertEqual(code, 0, out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        sent = self.sent_text()
        self.assertTrue(sent.startswith(before.decode("utf-8")))
        self.assertIn(f"【チェック通過】{MARK} ", sent.splitlines()[-1])
        with open(os.path.join(self.root, "deliveries.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["marker"], MARK)
        self.assertEqual(entry["file"], MARK + ".txt")

    def test_resend_same_marker_blocked(self):
        path = self.instruction("T001 を直す。台帳を更新する。")
        self.assertEqual(self.run_guard(path, "--send")[0], 0)
        code, out = self.run_guard(path, "--send")
        self.assertEqual(code, 2, out)
        self.assertIn("重複", out)

    def test_same_content_new_marker_blocked(self):
        self.assertEqual(self.run_guard(self.instruction("T001 を直す。\n台帳を更新する。"), "--send")[0], 0)
        other = self.instruction("T001 を直す。  台帳を\n更新する。", marker="TASK-20261201-BBB")
        code, out = self.run_guard(other)
        self.assertEqual(code, 2, out)
        self.assertIn("同じ内容", out)

    def test_live_lock_blocks_and_seen(self):
        self.lock("T001", "worker-b")
        path = self.instruction("T001 を直す。台帳を更新する。")
        code, out = self.run_guard(path)
        self.assertEqual(code, 2, out)
        self.assertIn("対象の競合", out)
        self.assertIn("worker-b", out)
        self.assertEqual(self.run_guard(path, "--seen", "T001=worker-b")[0], 0)
        self.assertEqual(self.run_guard(path, "--seen", "T001=worker-c")[0], 2)

    def test_stale_lock_blocks_and_released_does_not(self):
        self.lock("T001", "worker-b", beat_ago=datetime.timedelta(hours=3))
        path = self.instruction("T001 を直す。台帳を更新する。")
        code, out = self.run_guard(path)
        self.assertEqual(code, 2, out)
        self.assertIn("STALE", out)
        self.lock("T001", "worker-b", released=True)
        self.assertEqual(self.run_guard(path)[0], 0)

    def test_done_record_blocks(self):
        self.closed("DONE", "T001", "worker-a", datetime.timedelta(days=10))
        path = self.instruction("T001 を直す。台帳を更新する。")
        self.assertEqual(self.run_guard(path)[0], 2)
        self.assertEqual(self.run_guard(path, "--seen", "T001=worker-a")[0], 0)

    def test_closed_within_24h_blocks_older_stopped_does_not(self):
        self.closed("STOPPED", "T001", "worker-a", datetime.timedelta(hours=2))
        self.closed("STOPPED", "T002", "worker-a", datetime.timedelta(hours=30))
        self.assertEqual(self.run_guard(self.instruction("T001 を直す。台帳を更新する。"))[0], 2)
        self.assertEqual(self.run_guard(self.instruction("T002 を直す。台帳を更新する。"))[0], 0)

    def test_usage_error_exits_one(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for args in ([], ["--no-such-option"], ["--file"]):
            proc = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True,
                                  encoding="utf-8", env=env)
            self.assertEqual(proc.returncode, 1, args)
            self.assertIn("使い方の誤り", proc.stderr)

    def test_missing_marker_exit_1(self):
        path = os.path.join(self._tmp.name, "nomarker.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("目印なし。台帳を更新する。\n")
        self.assertEqual(self.run_guard(path)[0], 1)

    def test_two_markers_exit_1(self):
        path = self.instruction("TASK-20261201-ZZZ も書いてある。台帳を更新する。")
        self.assertEqual(self.run_guard(path)[0], 1)

    def test_lines_appended_only_when_missing(self):
        path = self.instruction("本番へデプロイする。")
        self.assertEqual(self.run_guard(path, "--send")[0], 0)
        sent = self.sent_text()
        self.assertIn("取り消せない操作の前に", sent)
        self.assertIn("台帳を更新する。", sent)
        done = self.instruction("本番へデプロイする。二重に実行しない。台帳を更新する。",
                                marker="TASK-20261201-CCC")
        self.assertEqual(self.run_guard(done, "--send")[0], 0)
        sent = self.sent_text("TASK-20261201-CCC")
        self.assertNotIn("取り消せない操作の前に", sent)
        self.assertNotIn("【共通】対象の状態が変わったら", sent)

    def test_writeback_forbidden(self):
        plain = self.instruction("文言を直す。台帳は更新しない。")
        code, out = self.run_guard(plain)
        self.assertEqual(code, 2, out)
        self.assertIn("台帳", out)
        self.assertEqual(self.run_guard(plain, "--no-writeback", "状態は変わらない")[0], 0)
        risky = self.instruction("本番へデプロイする。台帳は更新しない。", marker="TASK-20261201-DDD")
        self.assertEqual(self.run_guard(risky, "--no-writeback", "状態は変わらない")[0], 2)


if __name__ == "__main__":
    unittest.main()
