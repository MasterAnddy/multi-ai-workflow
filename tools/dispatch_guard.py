#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch_guard: 指示書を担当へ渡す前の機械チェック。

渡した指示書は取り消せない。そこで、重複した指示と、他の担当が扱っている
対象への指示を、渡す前に止める。

止めるもの:
  重複        同じ目印、または空白を除いて同じ内容が、配送記録か受け渡しフォルダにある。
              上書きの手段はない。新しい版を、新しい目印で書き直す。
  対象の競合  指示書の中の対象 ID(T001 形式)に、有効または STALE のロック、
              24 時間以内の終了記録、DONE の記録がある。
              --seen ID=担当 で、見つかった記録の担当を全員読んだと示すと通す。
  台帳        「台帳は更新しない」と書いてある。--no-writeback "理由" で通せるが、
              取り消せない操作を含む指示書は通さない。
止めずに足すもの:
  取り消せない操作があり、重複確認の文言がなければ GUARD_LINE を足す。
  台帳の更新に触れていなければ WRITEBACK_LINE を足す。

状態ファイルは <root>/active/<ID>.json と <root>/closed/*.json を読む。
root は --root、環境変数 WORKFLOW_ROOT、./.workflow の順に決める。

使い方(既定は検査だけ。--send で受け渡しフォルダへ置く):
  python tools/dispatch_guard.py --file instruction.txt
  python tools/dispatch_guard.py --file instruction.txt --seen T001=worker-b
  python tools/dispatch_guard.py --file instruction.txt --no-writeback "状態は変わらない作業" --send

終了コード: 0 通過または送付済み、2 止めた、1 入力や環境の誤り(引数の誤りを含む)。
元の指示書は書き換えない。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from collections import namedtuple

LOCK_TIMEOUT = datetime.timedelta(minutes=45)
RECENT = datetime.timedelta(hours=24)

MARKER = re.compile(r"TASK-\d{8}-[A-Z0-9]{3,}")
TARGET = re.compile(r"(?<![A-Za-z0-9])T\d{3,4}(?![A-Za-z0-9])")
IRREVERSIBLE = re.compile(
    r"公開する|送信する|リリースする|push\s*する|デプロイする"
    r"|\b(?:publish|deploy|push|release)\b", re.I)
DUP_GUARD = re.compile(r"二重|重複|既に(?:公開|送信)|already", re.I)
WRITEBACK_FORBIDDEN = re.compile(r"台帳[^。\n]{0,10}(?:更新しない|書かない|不要|触らない)")
RECORD_NAME = re.compile(
    r"^(DONE|STOPPED|TAKEOVER)_(T\d{3,4})_(.+)_(\d{8})_(\d{6})\.json$")

GUARD_LINE = ("【共通】取り消せない操作の前に、同じ操作が既に済んでいないかを確かめる。"
              "済んでいれば実行せず報告する。実行は1回だけ。")
WRITEBACK_LINE = "【共通】対象の状態が変わったら、この作業の中で台帳を更新する。"

Finding = namedtuple("Finding", "label blocks text")


def now():
    return datetime.datetime.now().astimezone()


def compact(text):
    """目印と空白を除く。目印だけ変えた同じ内容を見つけるため。"""
    return re.sub(r"\s+", "", MARKER.sub("", text))


def digest(text):
    return hashlib.sha256(compact(text).encode("utf-8")).hexdigest()


def parse_time(value):
    try:
        stamp = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.astimezone()


def ago(delta):
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 1:
        return "たった今"
    if minutes < 60:
        return f"{minutes} 分前"
    if minutes < 48 * 60:
        return f"{minutes // 60} 時間前"
    return f"{minutes // (24 * 60)} 日前"


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_root(arg):
    return arg or os.environ.get("WORKFLOW_ROOT") or os.path.join(".", ".workflow")


def inbox_texts(inbox, skip):
    """受け渡しフォルダの (ファイル名, 本文) を返す。skip は検査対象そのもの。"""
    if not os.path.isdir(inbox):
        return
    for name in sorted(os.listdir(inbox)):
        path = os.path.join(inbox, name)
        if not os.path.isfile(path) or os.path.abspath(path) == skip:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield name, handle.read()
        except OSError:
            continue


def log_entries(log_path):
    if not os.path.isfile(log_path):
        return []
    entries = []
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def check_duplicate(marker, text, inbox, log_path, skip):
    found = []
    sha = digest(text)
    for entry in log_entries(log_path):
        if entry.get("marker") == marker:
            found.append(f"同じ目印が配送記録にある({entry.get('file')}、{entry.get('time')})")
        elif entry.get("sha256") == sha:
            found.append(f"同じ内容が配送記録にある({entry.get('marker')})")
    plain = compact(text)
    for name, body in inbox_texts(inbox, skip):
        if marker in body or marker in name:
            found.append(f"同じ目印が受け渡しフォルダの {name} にある")
        elif compact(body).startswith(plain):
            found.append(f"同じ内容が受け渡しフォルダの {name} にある")
    out = [Finding("重複", True, why) for why in dict.fromkeys(found)]
    if out:
        out.append(Finding("重複", True, "上書きはできない。新しい版を新しい目印で書き直す"))
    return out


def target_records(root, target_id, moment):
    """対象 ID のロックと終了記録を [(種類, 担当, 経過)] で返す。止める記録だけ。"""
    records = []
    lock = read_json(os.path.join(root, "active", target_id + ".json"))
    if lock and not lock.get("released"):
        beat = parse_time(lock.get("beat") or lock.get("started"))
        age = moment - beat if beat else None
        stale = age is None or age > LOCK_TIMEOUT
        records.append(("STALE ロック" if stale else "ロック", str(lock.get("by", "?")), age))
    closed = os.path.join(root, "closed")
    names = sorted(os.listdir(closed)) if os.path.isdir(closed) else []
    for name in names:
        match = RECORD_NAME.match(name)
        if not match or match.group(2) != target_id:
            continue
        kind, _, by, day, clock = match.groups()
        data = read_json(os.path.join(closed, name)) or {}
        when = parse_time(data.get("closed_at"))
        if when is None:
            try:
                when = datetime.datetime.strptime(day + clock, "%Y%m%d%H%M%S").astimezone()
            except ValueError:
                when = moment
        age = moment - when
        if kind == "DONE" or age <= RECENT:
            records.append((kind, str(data.get("by", by)), age))
    return records


def check_targets(text, root, seen, moment):
    out = []
    ids = list(dict.fromkeys(TARGET.findall(MARKER.sub("", text))))
    for target_id in ids:
        records = target_records(root, target_id, moment)
        if not records:
            continue
        names = {rec[1] for rec in records}
        detail = "、".join(f"{kind}(担当 {by}、{ago(age) if age is not None else '時刻不明'})"
                          for kind, by, age in records)
        if names <= seen.get(target_id, set()):
            out.append(Finding("対象の競合", False, f"{target_id}: {detail} を読んだ上で通す"))
        else:
            out.append(Finding("対象の競合", True,
                               f"{target_id}: {detail}。確認して --seen {target_id}=<担当> を付ける"))
    return out


def additions(text, no_writeback):
    out, lines = [], []
    irreversible = bool(IRREVERSIBLE.search(text))
    if irreversible and not DUP_GUARD.search(text):
        lines.append(GUARD_LINE)
        out.append(Finding("追加", False, "重複確認の一文を足す"))
    if WRITEBACK_FORBIDDEN.search(text):
        if no_writeback and not irreversible:
            out.append(Finding("台帳", False, f"台帳を更新しない指示を通す(理由: {no_writeback})"))
        elif no_writeback:
            out.append(Finding("台帳", True, "取り消せない操作を含むため --no-writeback では通せない"))
        else:
            out.append(Finding("台帳", True, "台帳を更新しないと書いてある。--no-writeback \"理由\" を付ける"))
    elif "台帳" not in text:
        lines.append(WRITEBACK_LINE)
        out.append(Finding("追加", False, "台帳更新の一文を足す"))
    return lines, out


def run_checks(args, text, marker):
    root = resolve_root(args.root)
    inbox = args.inbox or os.path.join(root, "inbox")
    log_path = os.path.join(root, "deliveries.jsonl")
    seen = {}
    for item in args.seen:
        target_id, _, by = item.partition("=")
        seen.setdefault(target_id.strip(), set()).add(by.strip())
    findings = check_duplicate(marker, text, inbox, log_path, os.path.abspath(args.file))
    findings += check_targets(text, root, seen, now())
    lines, extra = additions(text, args.no_writeback)
    return findings + extra, lines, inbox, log_path


def send(text, lines, marker, inbox, log_path):
    stamp = now()
    body = text.rstrip("\n") + "\n" + "".join(line + "\n" for line in lines)
    body += f"【チェック通過】{marker} {stamp.isoformat(timespec='seconds')}\n"
    os.makedirs(inbox, exist_ok=True)
    name = marker + ".txt"
    with open(os.path.join(inbox, name), "x", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    entry = {"marker": marker, "sha256": digest(text), "file": name,
             "time": stamp.isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return name


class _Parser(argparse.ArgumentParser):
    """引数の誤りは終了コード 1 にする(2 は「止めた」に使う)。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"使い方の誤り: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_parser():
    parser = _Parser(description="指示書を渡す前の重複・競合チェック")
    parser.add_argument("--file", required=True, help="指示書のテキストファイル")
    parser.add_argument("--root", help="状態ファイルのルート(既定: WORKFLOW_ROOT か ./.workflow)")
    parser.add_argument("--inbox", help="受け渡しフォルダ(既定: <root>/inbox)")
    parser.add_argument("--seen", action="append", default=[], metavar="ID=担当",
                        help="見つかった記録の担当を読んだ印(複数可)")
    parser.add_argument("--no-writeback", metavar="理由", help="台帳を更新しない指示を通す理由")
    parser.add_argument("--send", action="store_true", help="通過したら受け渡しフォルダへ置く")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"入力エラー: 指示書を読めない: {exc}", file=sys.stderr)
        return 1
    markers = set(MARKER.findall(text))
    if len(markers) != 1:
        print(f"入力エラー: 目印 TASK-YYYYMMDD-XXXX は1つだけ必要(見つかった数: {len(markers)})",
              file=sys.stderr)
        return 1
    marker = markers.pop()
    findings, lines, inbox, log_path = run_checks(args, text, marker)
    for item in findings:
        print(f"[{item.label}] {'止める' if item.blocks else '止めない'}: {item.text}")
    if any(item.blocks for item in findings):
        print(f"結果: 止めた({marker})")
        return 2
    if not args.send:
        print(f"結果: 通過({marker})。--send で受け渡しフォルダへ置く")
        return 0
    try:
        name = send(text, lines, marker, inbox, log_path)
    except FileExistsError:
        print(f"[重複] 止める: {marker}.txt が既にある")
        return 2
    except OSError as exc:
        print(f"入力エラー: 書き込めない: {exc}", file=sys.stderr)
        return 1
    print(f"結果: 送付済み {os.path.join(inbox, name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
