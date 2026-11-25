#!/usr/bin/env python3
"""リリースゲート：レビューを、レビューしたそのバイト列に結びつける。

目的：
    レビューが有効なのは、レビューしたそのバイト列に対してだけである。
    レビュー後の編集、一覧に載せ忘れたファイル、すり替えたファイルは、
    ハッシュ（sha256）の不一致か未分類として検出し、リリースを止める。

使い方：
    python tools/release_gate.py check <フォルダ>
    python tools/release_gate.py hash <ファイル>...

    check はフォルダ直下のマニフェスト release_review.json を読み、全項目を検査する。
    hash はファイルごとの sha256 を表示する（マニフェストを書くときの補助）。

終了コード：
    0 = 合格
    2 = 検査で止めた（不合格）
    1 = 使い方や入力の誤り（引数の誤りを含む）
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

MANIFEST = "release_review.json"
MAX_BYTES = 16 * 1024 * 1024
EXCLUDE_ROLES = {"internal", "superseded", "source"}
EVIDENCE_SUFFIXES = {".md", ".txt", ".json"}
CANDIDATE_SUFFIXES = {".md", ".txt", ".json", ".csv", ".py", ".html", ".js",
                      ".css", ".yml", ".yaml", ".toml"}
TEXT_SUFFIXES = {".md", ".txt", ".py", ".html", ".js", ".css", ".json", ".csv",
                 ".yml", ".yaml", ".toml"}
SKIP_DIRS = {".git", ".workflow", "__pycache__", "_internal"}
PLACEHOLDERS = ("{{", "TODO:", "FIXME", "XXX:")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def resolve_inside(root, rel):
    """相対パス rel をフォルダ内の実ファイルへ解決する。外へ出る指定は ValueError。"""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path が空か文字列ではない")
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute() \
            or rel.startswith(("/", "\\")):
        raise ValueError("絶対パスは使えない")
    parts = rel.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("'..' を含むパスは使えない")
    path = (root / rel).resolve()
    try:
        key = path.relative_to(root).as_posix()
    except ValueError:
        raise ValueError("フォルダの外を指している") from None
    return key, path


def check_file_entry(root, entry):
    """{path, sha256} の1件を検査する。(key, 実際のsha256 または None) を返す。"""
    if not isinstance(entry, dict):
        raise ValueError("項目がオブジェクトではない")
    key, path = resolve_inside(root, entry.get("path"))
    if not path.is_file():
        raise ValueError("ファイルが存在しない")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("16 MiB を超えている")
    actual = sha256_file(path)
    declared = entry.get("sha256")
    if not isinstance(declared, str) or declared.lower() != actual:
        raise ValueError("sha256 が一致しない（マニフェスト %s / 実際 %s）" % (declared, actual))
    return key, actual


def load_manifest(root, fail):
    mf = root / MANIFEST
    if not mf.is_file():
        fail("manifest_invalid", MANIFEST, "マニフェストが無い")
        return None
    try:
        data = json.loads(mf.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as e:
        fail("manifest_invalid", MANIFEST, "JSON として読めない：%s" % e)
        return None
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int \
            or data["schema_version"] != 1:
        fail("manifest_invalid", MANIFEST, "schema_version が 1 のオブジェクトが必要")
        return None
    tid = data.get("target_id")
    if not isinstance(tid, str) or not tid.strip():
        fail("manifest_invalid", MANIFEST, "target_id が空")
        return None
    if not isinstance(data.get("artifacts"), list) or not data["artifacts"]:
        fail("manifest_invalid", MANIFEST, "artifacts が空か配列ではない")
        return None
    if not isinstance(data.get("excluded", []), list):
        fail("manifest_invalid", MANIFEST, "excluded は配列が必要")
        return None
    return data


def find_candidates(root, skip):
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in CANDIDATE_SUFFIXES:
                rel = p.relative_to(root).as_posix()
                if rel not in skip:
                    found.add(rel)
    return found


def find_placeholders(path):
    hits = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for no, line in enumerate(text.splitlines(), 1):
        for token in PLACEHOLDERS:
            if token in line:
                hits.append((no, token))
    return hits


def evaluate(folder):
    """検査して {"ok": bool, "errors": [{code, where, message}]} を返す。"""
    root = Path(folder).resolve()
    errors = []

    def fail(code, where, message):
        errors.append({"code": code, "where": where, "message": message})

    if not root.is_dir():
        fail("manifest_invalid", str(folder), "フォルダが存在しない")
        return {"ok": False, "errors": errors}
    m = load_manifest(root, fail)
    if m is None:
        return {"ok": False, "errors": errors}

    current = {}      # 成果物 path -> 実際の sha256（読めたもの）
    declared = {}     # 成果物 path -> マニフェストの sha256
    for entry in m["artifacts"]:
        where = entry.get("path", "?") if isinstance(entry, dict) else "?"
        try:
            key, actual = check_file_entry(root, entry)
            if key in declared:
                raise ValueError("成果物が重複している")
            current[key] = actual
            declared[key] = actual
        except (ValueError, OSError) as e:
            fail("artifact_invalid", str(where), str(e))
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                try:
                    key, path = resolve_inside(root, entry["path"])
                    declared.setdefault(key, entry.get("sha256"))
                    if path.is_file():
                        current[key] = sha256_file(path)
                except (ValueError, OSError):
                    pass

    excluded = set()
    for entry in m.get("excluded", []):
        where = entry.get("path", "?") if isinstance(entry, dict) else "?"
        try:
            key, _ = check_file_entry(root, entry)
            if entry.get("role") not in EXCLUDE_ROLES:
                raise ValueError("role は internal / superseded / source のどれか")
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("reason が空")
            if key in declared:
                raise ValueError("成果物にも載っている")
            if key in excluded:
                raise ValueError("除外が重複している")
            excluded.add(key)
        except (ValueError, OSError) as e:
            fail("exclusion_invalid", str(where), str(e))

    review = m.get("review")
    evidence_rel = None
    if isinstance(review, dict) and isinstance(review.get("evidence"), dict):
        ep = review["evidence"].get("path")
        if isinstance(ep, str):
            try:
                evidence_rel = resolve_inside(root, ep)[0]
            except ValueError:
                evidence_rel = None

    skip = {MANIFEST}
    if evidence_rel:
        skip.add(evidence_rel)
    for rel in sorted(find_candidates(root, skip) - set(declared) - excluded):
        fail("unlisted_file", rel, "成果物にも除外にも載っていない")

    def hashes_for(key):
        return current.get(key, declared.get(key))

    # --- review ---
    if not isinstance(review, dict):
        fail("review_invalid", "review", "review オブジェクトが無い")
        review = {}
    for k in ("reviewer", "reviewed_at"):
        v = review.get(k)
        if not isinstance(v, str) or not v.strip():
            fail("review_invalid", "review." + k, "空")
    when = review.get("reviewed_at")
    if isinstance(when, str) and when.strip():
        try:
            dt = datetime.datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                fail("review_invalid", "review.reviewed_at", "タイムゾーンのオフセットが無い")
        except ValueError:
            fail("review_invalid", "review.reviewed_at", "ISO 8601 として読めない")
    expected = {k: hashes_for(k) for k in declared}
    got = review.get("artifact_hashes")
    if not isinstance(got, dict):
        fail("review_invalid", "review.artifact_hashes", "オブジェクトが必要")
    elif got != expected:
        for k in sorted(set(got) | set(expected)):
            if got.get(k) != expected.get(k):
                fail("review_invalid", "review.artifact_hashes",
                     "%s：レビュー時のハッシュと現物が一致しない" % k)

    # --- evidence ---
    ev = review.get("evidence")
    ev_text = None
    try:
        if not isinstance(ev, dict):
            raise ValueError("evidence オブジェクトが無い")
        key, actual = check_file_entry(root, ev)
        if Path(key).suffix.lower() not in EVIDENCE_SUFFIXES:
            raise ValueError("証拠ファイルは .md / .txt / .json")
        ev_text = (root / key).read_text(encoding="utf-8", errors="replace")
    except (ValueError, OSError) as e:
        fail("evidence_invalid", str(ev.get("path", "evidence")) if isinstance(ev, dict)
             else "evidence", str(e))
    if ev_text is not None:
        if m["target_id"].strip() not in ev_text:
            fail("evidence_invalid", ev["path"], "対象ID %s が書かれていない" % m["target_id"])
        for key in sorted(declared):
            h = hashes_for(key)
            if isinstance(h, str) and h and h not in ev_text:
                fail("evidence_invalid", ev["path"], "%s の sha256 が書かれていない" % key)

    # --- placeholders ---
    for key in sorted(current):
        if Path(key).suffix.lower() in TEXT_SUFFIXES:
            try:
                for no, token in find_placeholders(root / key):
                    fail("placeholder_left", "%s:%d" % (key, no), "残っている目印 %s" % token)
            except OSError as e:
                fail("artifact_invalid", key, str(e))

    return {"ok": not errors, "errors": errors}


def cmd_check(args):
    result = evaluate(args.folder)
    for e in result["errors"]:
        print("[%s] %s: %s" % (e["code"], e["where"], e["message"]))
    print("判定 " + ("合格" if result["ok"] else "不合格"))
    return 0 if result["ok"] else 2


def cmd_hash(args):
    status = 0
    for name in args.files:
        try:
            print("%s  %s" % (sha256_file(name), name))
        except OSError as e:
            print("読めない: %s (%s)" % (name, e), file=sys.stderr)
            status = 1
    return status


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print("使い方の誤り: %s" % message, file=sys.stderr)
        raise SystemExit(1)


def build_parser():
    p = _Parser(description="レビュー済みのバイト列だけをリリースさせるゲート")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="フォルダのマニフェスト release_review.json を検査する")
    c.add_argument("folder")
    c.set_defaults(func=cmd_check)
    h = sub.add_parser("hash", help="ファイルの sha256 を表示する")
    h.add_argument("files", nargs="+")
    h.set_defaults(func=cmd_hash)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
