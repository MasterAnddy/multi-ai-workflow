import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "release_gate.py"

spec = importlib.util.spec_from_file_location("release_gate", TOOL)
rg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rg)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.files = {"CHANGELOG.md": b"# changes\n- one\n", "docs/usage.md": b"# usage\n"}
        for rel, data in self.files.items():
            self.write(rel, data)
        self.manifest = None
        self.build()

    def write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def build(self, excluded=None, reviewed_at="2026-01-12T10:00:00+09:00"):
        hashes = {rel: sha(data) for rel, data in self.files.items()}
        ev = "target T003\n" + "\n".join(hashes.values()) + "\n"
        self.write("review.md", ev)
        self.manifest = {
            "schema_version": 1,
            "target_id": "T003",
            "artifacts": [{"path": r, "sha256": h} for r, h in hashes.items()],
            "excluded": excluded or [],
            "review": {
                "reviewer": "reviewer-b",
                "reviewed_at": reviewed_at,
                "artifact_hashes": dict(hashes),
                "evidence": {"path": "review.md", "sha256": sha(ev.encode("utf-8"))},
            },
        }
        self.save()

    def save(self):
        (self.root / "release_review.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")

    def codes(self):
        return [e["code"] for e in rg.evaluate(self.root)["errors"]]

    def test_pass(self):
        self.assertEqual(rg.evaluate(self.root), {"ok": True, "errors": []})

    def test_edited_after_review(self):
        self.write("CHANGELOG.md", b"# changes\n- one\n- sneaked in\n")
        codes = self.codes()
        self.assertIn("artifact_invalid", codes)
        self.assertIn("review_invalid", codes)

    def test_unlisted_file(self):
        self.write("extra.md", "forgotten\n")
        errs = rg.evaluate(self.root)["errors"]
        self.assertEqual([(e["code"], e["where"]) for e in errs],
                         [("unlisted_file", "extra.md")])

    def test_skipped_dirs_and_other_suffixes_ignored(self):
        self.write("_internal/x.md", "x")
        self.write(".git/y.md", "y")
        self.write("__pycache__/z.json", "{}")
        self.write("logo.png", b"\x89PNG")
        self.assertTrue(rg.evaluate(self.root)["ok"])

    def test_excluded_file_passes(self):
        data = b"old notes\n"
        self.write("notes/draft_v1.md", data)
        self.build(excluded=[{"path": "notes/draft_v1.md", "sha256": sha(data),
                              "role": "superseded", "reason": "v2 に置き換え済み"}])
        self.assertTrue(rg.evaluate(self.root)["ok"])

    def test_excluded_needs_role_and_reason(self):
        data = b"old\n"
        self.write("notes/a.md", data)
        self.build(excluded=[{"path": "notes/a.md", "sha256": sha(data),
                              "role": "misc", "reason": ""}])
        self.assertIn("exclusion_invalid", self.codes())

    def test_path_traversal_rejected(self):
        outside = self.root.parent / "x.md"
        outside.write_text("x", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink())
        self.manifest["artifacts"].append({"path": "../x.md", "sha256": sha(b"x")})
        self.save()
        errs = rg.evaluate(self.root)["errors"]
        self.assertTrue(any(e["code"] == "artifact_invalid" and e["where"] == "../x.md"
                            for e in errs))

    def test_absolute_path_rejected(self):
        self.manifest["artifacts"].append({"path": str(self.root / "CHANGELOG.md"),
                                           "sha256": sha(b"")})
        self.save()
        self.assertIn("artifact_invalid", self.codes())

    def test_duplicate_artifact(self):
        self.manifest["artifacts"].append(dict(self.manifest["artifacts"][0]))
        self.save()
        self.assertIn("artifact_invalid", self.codes())

    def test_reviewed_at_without_timezone(self):
        self.build(reviewed_at="2026-01-12T10:00:00")
        self.assertEqual(self.codes(), ["review_invalid"])

    def test_evidence_missing_hash(self):
        ev = "target T003\n" + sha(self.files["CHANGELOG.md"]) + "\n"
        self.write("review.md", ev)
        self.manifest["review"]["evidence"]["sha256"] = sha(ev.encode("utf-8"))
        self.save()
        errs = rg.evaluate(self.root)["errors"]
        self.assertEqual([e["code"] for e in errs], ["evidence_invalid"])
        self.assertIn("docs/usage.md", errs[0]["message"])

    def test_manifest_problems(self):
        (self.root / "release_review.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(self.codes(), ["manifest_invalid"])
        self.manifest["schema_version"] = 2
        self.save()
        self.assertEqual(self.codes(), ["manifest_invalid"])
        self.manifest["schema_version"] = 1
        self.manifest["artifacts"] = []
        self.save()
        self.assertEqual(self.codes(), ["manifest_invalid"])
        (self.root / "release_review.json").unlink()
        self.assertEqual(self.codes(), ["manifest_invalid"])

    def test_placeholder_with_line_number(self):
        self.files["CHANGELOG.md"] = b"# changes\n\nTODO: write\n"
        self.write("CHANGELOG.md", self.files["CHANGELOG.md"])
        self.build()
        errs = rg.evaluate(self.root)["errors"]
        self.assertEqual([(e["code"], e["where"]) for e in errs],
                         [("placeholder_left", "CHANGELOG.md:3")])

    def test_hash_subcommand(self):
        p = self.root / "CHANGELOG.md"
        out = subprocess.run([sys.executable, str(TOOL), "hash", str(p)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.split()[0], sha(self.files["CHANGELOG.md"]))

    def test_cli_exit_codes(self):
        run = lambda *a: subprocess.run([sys.executable, str(TOOL), *a],
                                        capture_output=True).returncode
        self.assertEqual(run("check", str(self.root)), 0)
        self.write("extra.md", "x")
        self.assertEqual(run("check", str(self.root)), 2)
        self.assertEqual(run("nonsense"), 1)
        self.assertEqual(run(), 1)

    def test_example_folder_passes(self):
        out = subprocess.run([sys.executable, str(TOOL), "check",
                              str(ROOT / "examples" / "release_gate")],
                             capture_output=True)
        self.assertEqual(out.returncode, 0, out.stdout)


if __name__ == "__main__":
    unittest.main()
