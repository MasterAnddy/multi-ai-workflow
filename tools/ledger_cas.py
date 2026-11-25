"""台帳（CSV）への書き込みを、比較して交換する方式（compare-and-swap）で守る。

目的：
    指示書を書いたあとに、別の担当が同じ行を書き換えていることがある。
    台帳ロックを持っていても、古い読みに基づく書き込みは防げない。
    このツールは、指示書の作成時に見た値（expect）が今も同じときだけ書く。
    一つでも食い違えば、何も書かずに止める。

patch.json の形：
    {"updates": [{"id": "T002",
                  "cells": {"status": "review", "updated": "{TODAY}"},
                  "expect": {"status": "doing", "updated": "2026-01-10"}}],
     "append": [{"ID": "T005", "title": "...", "status": "todo",
                 "next_step": "...", "updated": "{TODAY}"}]}

検査（すべて通ったときだけ書く）：
    - <root>/ledger.lock があり、解除されておらず、by が --by と一致する。
      host が記録されていれば、現在の host と一致する。
    - 各 update に空でない expect があり、expect が cells の全列を含む。
    - ID がちょうど1行に一致し、expect の列が存在し、現在値が一致する。
      None・欠落と空文字は同じ。文字列として比べる。
    - append の ID が既存でなく、列名がすべて台帳にある。
    {TODAY} は実行日（ローカル）の YYYY-MM-DD に置き換える。

使い方：
    python tools/ledger_cas.py patch.json --by worker-a
    python tools/ledger_cas.py patch.json --by worker-a --root .workflow --dry-run
    python tools/ledger_cas.py patch.json --by worker-a --ledger path/to/ledger.csv

    ルートは --root、環境変数 WORKFLOW_ROOT、./.workflow の順に決まる。
    台帳は --ledger、なければ <root>/ledger.csv。
    host は socket.gethostname()。環境変数 WORKFLOW_HOST があればそれを使う。

終了コード：
    0 = 書き込んだ（--dry-run では、書けると確認できた）
    2 = 検査で止めた（何も書いていない）
    1 = 入力や環境の誤り（patch.json・台帳が読めない、引数の誤り、など）
"""
import argparse
import csv
import datetime
import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def text_of(value):
    """None と空文字を同じ扱いにして、文字列で返す。"""
    return "" if value is None else str(value)


def current_host():
    return os.environ.get("WORKFLOW_HOST") or socket.gethostname()


def resolve_root(arg_root):
    return Path(arg_root or os.environ.get("WORKFLOW_ROOT") or ".workflow")


def check_lock(root, by):
    """台帳ロックの検査。問題の説明を返す（空なら通過）。"""
    path = root / "ledger.lock"
    if not path.is_file():
        return ["台帳ロックがない"]
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["台帳ロックを読めない"]
    if not isinstance(lock, dict):
        return ["台帳ロックの形式が違う"]
    if lock.get("released") is True:
        return ["台帳ロックは解除済み"]
    if lock.get("by") != by:
        return ["台帳ロックの担当が --by と違う"]
    host = lock.get("host")
    if host and host != current_host():
        return ["台帳ロックの host が現在の host と違う"]
    return []


def read_ledger(path):
    """(列名のリスト, 行のリスト) を返す。行は列数に揃えた文字列のリスト。"""
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError("台帳が空")
    head = rows[0]
    if "ID" not in head:
        raise ValueError("台帳に ID 列がない")
    body = []
    for r in rows[1:]:
        if not r:
            continue
        body.append((r + [""] * len(head))[: len(head)] if len(r) <= len(head) else r)
    return head, body


def fill_today(value, today):
    return value.replace("{TODAY}", today) if isinstance(value, str) else value


def plan_changes(head, rows, patch, today):
    """検査して、(問題のリスト, 変更計画) を返す。問題には ID と列名だけを書く。"""
    problems = []
    plan = []  # ("update", 行番号, {列: 新値}) / ("append", None, [行])
    id_col = head.index("ID")
    updates = patch.get("updates", [])
    appends = patch.get("append", [])
    if not isinstance(updates, list) or not isinstance(appends, list):
        raise ValueError("updates と append は配列にする")

    seen = set()
    for n, upd in enumerate(updates, 1):
        if not isinstance(upd, dict) or not isinstance(upd.get("id"), str):
            problems.append(f"updates[{n}]: id がない")
            continue
        tid = upd["id"]
        cells = upd.get("cells")
        expect = upd.get("expect")
        if tid in seen:
            problems.append(f"{tid}: updates に同じ ID が複数ある")
            continue
        seen.add(tid)
        if not isinstance(cells, dict) or not cells:
            problems.append(f"{tid}: cells がない")
            continue
        if not isinstance(expect, dict) or not expect:
            problems.append(f"{tid}: expect がない")
            continue
        hits = [i for i, r in enumerate(rows) if r[id_col] == tid]
        if len(hits) != 1:
            problems.append(f"{tid}: ID に一致する行が {len(hits)} 件")
            continue
        row = rows[hits[0]]
        ok = True
        for col in cells:
            if col not in head:
                problems.append(f"{tid} {col}: 台帳にない列")
                ok = False
            elif col == "ID":
                problems.append(f"{tid} {col}: ID 列は書き換えない")
                ok = False
            if col not in expect:
                problems.append(f"{tid} {col}: cells の列に expect がない")
                ok = False
        for col, want in expect.items():
            if col not in head:
                problems.append(f"{tid} {col}: 台帳にない列")
                ok = False
            elif text_of(row[head.index(col)]) != text_of(want):
                problems.append(f"{tid} {col}: 指示を書いたあとに変わっている")
                ok = False
        if ok:
            new = {c: text_of(fill_today(v, today)) for c, v in cells.items()}
            plan.append(("update", hits[0], new))

    existing = {r[id_col] for r in rows}
    for n, rec in enumerate(appends, 1):
        if not isinstance(rec, dict) or not isinstance(rec.get("ID"), str):
            problems.append(f"append[{n}]: ID がない")
            continue
        tid = rec["ID"]
        if tid in existing:
            problems.append(f"{tid}: 追加しようとした ID が既にある")
            continue
        unknown = [c for c in rec if c not in head]
        for col in unknown:
            problems.append(f"{tid} {col}: 台帳にない列")
        if unknown:
            continue
        existing.add(tid)
        plan.append(("append", None, [text_of(fill_today(rec.get(c), today)) for c in head]))
    return problems, plan


def apply_plan(rows, head, plan):
    out = [list(r) for r in rows]
    for kind, idx, data in plan:
        if kind == "update":
            for col, val in data.items():
                out[idx][head.index(col)] = val
        else:
            out.append(list(data))
    return out


def write_atomic(path, head, rows):
    """一時ファイルへ書いてから os.replace で置き換える。LF・UTF-8。"""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(head)
            writer.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def report_plan(head, plan, rows):
    id_col = head.index("ID")
    for kind, idx, data in plan:
        if kind == "update":
            print(f"  更新 {rows[idx][id_col]}: {', '.join(data)}")
        else:
            print(f"  追加 {data[id_col]}")


class _Parser(argparse.ArgumentParser):
    """引数の誤りは終了コード 1 にする（2 は「検査で止めた」に使う）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"使い方の誤り: {message}", file=sys.stderr)
        raise SystemExit(1)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    ap = _Parser(description="台帳を比較して交換する方式で更新する")
    ap.add_argument("patch", help="patch.json のパス")
    ap.add_argument("--by", required=True, help="台帳ロックを持つ担当のラベル")
    ap.add_argument("--root", help="状態ファイルのルート")
    ap.add_argument("--ledger", help="台帳 CSV のパス（省略時は <root>/ledger.csv）")
    ap.add_argument("--dry-run", action="store_true", help="検査と計画の表示だけで書かない")
    args = ap.parse_args(argv)

    root = resolve_root(args.root)
    ledger = Path(args.ledger) if args.ledger else root / "ledger.csv"
    try:
        patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
        if not isinstance(patch, dict):
            raise ValueError("patch.json の最上位はオブジェクトにする")
        head, rows = read_ledger(ledger)
    except (OSError, ValueError) as exc:
        print(f"入力エラー: {exc}")
        return 1

    lock_problems = check_lock(root, args.by)
    problems, plan = [], []
    if not lock_problems:
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        try:
            problems, plan = plan_changes(head, rows, patch, today)
        except ValueError as exc:
            print(f"入力エラー: {exc}")
            return 1

    # 問題の表示には ID と列名だけを出す。セルの値は出さない。
    if lock_problems:
        print("書き込まなかった: 台帳ロックを持っていない。")
        for p in lock_problems:
            print(f" - {p}")
        return 2
    if problems:
        print("書き込まなかった: 台帳が、この指示を書いた時の状態と違う。指示を作り直す。")
        for p in problems:
            print(f" - {p}")
        return 2

    if args.dry_run:
        print("dry-run: 検査は通った。書き込まない。計画:")
        report_plan(head, plan, rows)
        return 0

    write_atomic(ledger, head, apply_plan(rows, head, plan))
    print("書き込んだ:")
    report_plan(head, plan, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
