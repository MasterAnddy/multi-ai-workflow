#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""対象ロックと台帳ロック。複数のAIエージェントが同じ作業を同時に触らないための機械チェック。

目的：
  対象（作業の単位）ごとのロックと、台帳（CSV）への書き込みロックを、コマンドで取得・更新・解除する。
  理由は3つ。
  1. セッションが落ちてもロックが永久に残らないよう、beat のない状態が一定時間続けば STALE にする。
  2. 他の担当のロックは解除できない。STALE でも takeover だけが引き継ぎ口で、監査記録が残る。
  3. 台帳への書き込みは、ロック取得時に必ずバックアップを取る。

使い方（状態の置き場は --root → 環境変数 WORKFLOW_ROOT → ./.workflow。--root/--ledger はコマンド名の前でも後でもよい）：
  python tools/target_lock.py status
  python tools/target_lock.py claim T001 --by worker-a --note "仕様の確認"
  python tools/target_lock.py beat T001 --by worker-a
  python tools/target_lock.py done T001 --by worker-a --note "完了"
  python tools/target_lock.py stop T001 --by worker-a --reason "入力待ち"
  python tools/target_lock.py takeover T001 --by reviewer-b --why "worker-a が停止"
  python tools/target_lock.py ledger-lock --by worker-a --purpose "T001 の結果を記入"
  python tools/target_lock.py ledger-beat --by worker-a
  python tools/target_lock.py ledger-unlock --by worker-a
  python tools/target_lock.py ledger-takeover --by reviewer-b --why "worker-a が停止"
  python tools/target_lock.py restore --list
  python tools/target_lock.py restore ledger_20260101_120000_ab12cd34.csv --by worker-a   # 台帳ロックを持つ担当だけ

タイムアウト：対象ロック 45 分、台帳ロック 10 分。最後の beat から超えたら STALE。
host は socket.gethostname()（環境変数 WORKFLOW_HOST があればそれ）。by と host が両方一致する者だけが保持者。

終了コード：0 = 成功、2 = ロックの規則で止めた、1 = 入力や環境の誤り（引数の誤りを含む）。
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sys

TARGET_TTL_MIN = 45
LEDGER_TTL_MIN = 10
KEEP_BACKUPS = 60
ID_PATTERN = re.compile(r"^T\d{3,4}$")


class InputError(Exception):
    pass


def host():
    return os.environ.get("WORKFLOW_HOST") or socket.gethostname()


def now():
    return datetime.datetime.now().astimezone()


def ts():
    return now().isoformat(timespec="seconds")


def stamp():
    return now().strftime("%Y%m%d_%H%M%S")


class Paths:
    def __init__(self, root, ledger):
        self.root = os.path.abspath(root or os.environ.get("WORKFLOW_ROOT") or ".workflow")
        self.ledger = ledger or os.path.join(self.root, "ledger.csv")
        self.active = os.path.join(self.root, "active")
        self.closed = os.path.join(self.root, "closed")
        self.backups = os.path.join(self.root, "backups")
        self.ledger_lock = os.path.join(self.root, "ledger.lock")

    def target(self, target_id):
        if not ID_PATTERN.match(target_id):
            raise InputError(f"対象 ID は T001 形式で指定する：{target_id}")
        return os.path.join(self.active, f"{target_id}.json")


def sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def create_exclusive(path, data):
    """O_EXCL でロックファイルを新規作成する。既にあれば False。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def create_lock(path, data):
    """ロックを原子的に作る。解除済みの印があれば先に消してから作る。
    他の担当が有効なロックを先に作っていれば False（上書きしない）。"""
    for _ in range(2):
        if create_exclusive(path, data):
            return True
        if read_lock(path) is not None:
            return False
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise InputError(f"解除済みの印を消せない：{path}（{e}）")
    return False


def read_lock(path):
    """ロックを読む。無い、または released=true なら None（ロックなし）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise InputError(f"ロックファイルを読めない：{path}（{e}）")
    if not isinstance(data, dict):
        raise InputError(f"ロックファイルの形式が違う：{path}")
    return None if data.get("released") else data


def age_min(lock):
    try:
        t = datetime.datetime.fromisoformat(lock.get("beat") or lock.get("started"))
        if t.tzinfo is None:
            t = t.astimezone()
        return (now() - t).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def state(lock, ttl):
    age = age_min(lock)
    return ("STALE" if age is None or age > ttl else "有効"), age


def age_text(age):
    return "不明" if age is None else f"{age:.0f}分前"


def is_holder(lock, by):
    if not by or by != lock.get("by"):
        return False
    held_host = lock.get("host")
    return not held_host or held_host == host()


def drop(path, lock, why):
    """ロックを解除する。削除できなければ released=true を書き戻す（読む側はロックなしと扱う）。"""
    try:
        os.remove(path)
        return "削除"
    except OSError:
        marked = dict(lock, released=True, released_at=ts(), released_why=why)
        write_json(path, marked)
        return "released 印"


def refuse_not_holder(lock, by, ttl, takeover_cmd):
    st, age = state(lock, ttl)
    print(f"✗ 拒否：現在の保持者 by={lock.get('by', '?')} host={lock.get('host', '?')}"
          f"（最後の beat {age_text(age)}、状態={st}）。")
    print(f"  今回の by={by or '(未指定)'} host={host()} は保持者と一致しない。他の担当のロックは解除も更新もできない。")
    if st == "STALE":
        print(f"  STALE。相手の停止を確認したら：{takeover_cmd}")
    else:
        print("  保持者が自分で解除するのを待つ。")
    return 2


def write_closed(paths, kind, lock, by, note, extra=None):
    record = dict(lock, kind=kind, closed_at=ts(), note=note)
    record.update(extra or {})
    name = f"{kind}_{lock.get('id', 'LEDGER')}_{by}_{stamp()}.json"
    write_json(os.path.join(paths.closed, name), record)
    return name


# ---------------------------------------------------------------- 台帳バックアップ

def backup_ledger(paths):
    if not os.path.exists(paths.ledger):
        return None
    os.makedirs(paths.backups, exist_ok=True)
    name = f"ledger_{stamp()}_{sha256(paths.ledger)[:8]}.csv"
    dst = os.path.join(paths.backups, name)
    if not os.path.exists(dst):
        shutil.copy2(paths.ledger, dst)
    for old in sorted(glob.glob(os.path.join(paths.backups, "ledger_*.csv")))[:-KEEP_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass
    return name


# ---------------------------------------------------------------- 対象ロック

def cmd_status(a, paths):
    print(f"現在：{ts()}（host={host()}）\n── 対象ロック ──")
    found = False
    for p in sorted(glob.glob(os.path.join(paths.active, "*.json"))):
        lock = read_lock(p)
        if lock is None:
            continue
        found = True
        st, age = state(lock, TARGET_TTL_MIN)
        mark = "  ⚠ takeover 可" if st == "STALE" else ""
        print(f"  {lock.get('id', os.path.basename(p))}  by={lock.get('by')}  host={lock.get('host')}"
              f"  beat={age_text(age)}  {st}{mark}")
        if lock.get("note"):
            print(f"      {lock['note']}")
    if not found:
        print("  （なし）")
    print("── 台帳ロック ──")
    lock = read_lock(paths.ledger_lock)
    if lock is None:
        print("  （なし）")
        return 0
    st, age = state(lock, LEDGER_TTL_MIN)
    print(f"  by={lock.get('by')}  host={lock.get('host')}  beat={age_text(age)}  {st}")
    print(f"  目的：{lock.get('purpose', '')}")
    if st == "STALE":
        print('  ⚠ ledger-takeover 可：python tools/target_lock.py ledger-takeover --by <担当> --why "..."')
    return 0


def cmd_claim(a, paths):
    path = paths.target(a.target_id)
    lock = read_lock(path)
    if lock is not None:
        st, age = state(lock, TARGET_TTL_MIN)
        print(f"✗ 拒否：{a.target_id} は by={lock.get('by')} host={lock.get('host')} が保持中"
              f"（最後の beat {age_text(age)}、状態={st}）。")
        if st == "STALE":
            print(f'  相手の停止を確認したら：python tools/target_lock.py takeover {a.target_id} '
                  f'--by {a.by} --why "..."')
        else:
            print("  この対象は触らない。保持者の done / stop を待つ。")
        return 2
    others = []
    for p in sorted(glob.glob(os.path.join(paths.active, "*.json"))):
        other = read_lock(p)
        if other is not None and other.get("by") == a.by:
            others.append(other)
    if others:
        print(f"⚠ 警告：by={a.by} は既に別の対象を保持している：")
        for other in others:
            print(f"    {other.get('id')}  host={other.get('host')}")
        print(f"  別セッションなら by を分ける（例：{a.by}-2）。")
    t = ts()
    if not create_lock(path, {"id": a.target_id, "by": a.by, "host": host(), "note": a.note or "",
                              "started": t, "beat": t}):
        print(f"✗ 拒否：{a.target_id} は直前に別の担当が取得した。status で確認する。")
        return 2
    print(f"✓ {a.target_id} のロックを取得（by={a.by}）。作業の節目で beat を打つ。")
    return 0


def holder_lock(a, paths, command):
    """保持者として操作できるロックを返す。(lock, path, 終了コード) の形。"""
    path = paths.target(a.target_id)
    lock = read_lock(path)
    if lock is None:
        print(f"✗ {a.target_id} のロックがない（解除済み、または未取得）。ロックは作り直さない。")
        return None, path, 1
    if not is_holder(lock, a.by):
        cmd = f'python tools/target_lock.py takeover {a.target_id} --by {a.by or "<担当>"} --why "..."'
        return None, path, refuse_not_holder(lock, a.by, TARGET_TTL_MIN, cmd)
    return lock, path, 0


def cmd_beat(a, paths):
    lock, path, rc = holder_lock(a, paths, "beat")
    if lock is None:
        return rc
    lock["beat"] = ts()
    write_json(path, lock)
    print(f"✓ beat {a.target_id} @ {lock['beat']}")
    return 0


def close_target(a, paths, kind, note):
    lock, path, rc = holder_lock(a, paths, kind)
    if lock is None:
        return rc
    name = write_closed(paths, kind, lock, lock["by"], note)
    how = drop(path, lock, f"{kind}: {note}")
    print(f"✓ {kind} → closed/{name}（ロック解除：{how}）")
    return 0


def cmd_done(a, paths):
    return close_target(a, paths, "DONE", a.note)


def cmd_stop(a, paths):
    return close_target(a, paths, "STOPPED", a.reason)


def cmd_takeover(a, paths):
    path = paths.target(a.target_id)
    lock = read_lock(path)
    if lock is None:
        print(f"✗ {a.target_id} のロックがない。takeover は不要で、claim できる。")
        return 1
    st, age = state(lock, TARGET_TTL_MIN)
    if st != "STALE":
        print(f"✗ 拒否：{a.target_id} のロックは有効（最後の beat {age_text(age)}、{TARGET_TTL_MIN} 分以内）。")
        return 2
    name = write_closed(paths, "TAKEOVER", lock, a.by, a.why, {"taken_over_by": a.by, "taken_over_host": host()})
    how = drop(path, lock, f"takeover by {a.by}: {a.why}")
    print(f"✓ 引き継ぎ：監査記録 closed/{name}（ロック解除：{how}）")
    print(f"  続けて：python tools/target_lock.py claim {a.target_id} --by {a.by}")
    return 0


# ---------------------------------------------------------------- 台帳ロック

def ledger_holder(a, paths):
    lock = read_lock(paths.ledger_lock)
    if lock is None:
        print("✗ 台帳ロックがない。")
        return None, 1
    if not is_holder(lock, a.by):
        cmd = f'python tools/target_lock.py ledger-takeover --by {a.by or "<担当>"} --why "..."'
        return None, refuse_not_holder(lock, a.by, LEDGER_TTL_MIN, cmd)
    return lock, 0


def cmd_ledger_lock(a, paths):
    lock = read_lock(paths.ledger_lock)
    if lock is not None:
        st, age = state(lock, LEDGER_TTL_MIN)
        print(f"✗ 拒否：台帳は by={lock.get('by')} host={lock.get('host')} がロック中"
              f"（最後の beat {age_text(age)}、状態={st}）。")
        if st == "STALE":
            print(f'  相手の停止を確認したら：python tools/target_lock.py ledger-takeover --by {a.by} --why "..."')
        else:
            print("  待つか、保持者に確認する。")
        return 2
    backup = backup_ledger(paths)
    t = ts()
    if not create_lock(paths.ledger_lock, {"by": a.by, "host": host(), "purpose": a.purpose, "started": t,
                                           "beat": t, "ledger_sha256_before": sha256(paths.ledger),
                                           "backup": backup}):
        print("✗ 拒否：台帳は直前に別の担当がロックした。status で確認する。")
        return 2
    print(f"✓ 台帳ロックを取得（by={a.by}）。バックアップ：{backup or '（台帳なし）'}")
    return 0


def cmd_ledger_beat(a, paths):
    lock, rc = ledger_holder(a, paths)
    if lock is None:
        return rc
    lock["beat"] = ts()
    write_json(paths.ledger_lock, lock)
    print(f"✓ 台帳ロック beat @ {lock['beat']}")
    return 0


def cmd_ledger_unlock(a, paths):
    lock, rc = ledger_holder(a, paths)
    if lock is None:
        return rc
    before = lock.get("ledger_sha256_before")
    after = sha256(paths.ledger)
    backup_ledger(paths)
    how = drop(paths.ledger_lock, lock, f"{a.by} unlock")
    print(f"✓ 台帳ロックを解除（{how}）")
    print(f"  書き込み前 sha256 = {before}")
    print(f"  書き込み後 sha256 = {after}")
    if before == after:
        print("  ⚠ 前後が同じ。書き込まれていない可能性がある。")
    return 0


def cmd_ledger_takeover(a, paths):
    lock = read_lock(paths.ledger_lock)
    if lock is None:
        print("✗ 台帳ロックがない。ledger-lock できる。")
        return 1
    st, age = state(lock, LEDGER_TTL_MIN)
    if st != "STALE":
        print(f"✗ 拒否：台帳ロックは有効（最後の beat {age_text(age)}、{LEDGER_TTL_MIN} 分以内）。")
        return 2
    backup_ledger(paths)
    record = dict(lock, id="LEDGER")
    name = write_closed(paths, "TAKEOVER", record, a.by, a.why,
                        {"taken_over_by": a.by, "taken_over_host": host(), "ledger_sha256_at_takeover": sha256(paths.ledger)})
    how = drop(paths.ledger_lock, lock, f"takeover by {a.by}: {a.why}")
    print(f"✓ 台帳ロックを引き継ぎ：監査記録 closed/{name}（ロック解除：{how}）")
    return 0


def cmd_restore(a, paths):
    files = sorted(glob.glob(os.path.join(paths.backups, "ledger_*.csv")))
    if a.list or not a.name:
        if not files:
            print("（バックアップなし。ledger-lock で作られる）")
            return 0
        print(f"バックアップ {len(files)} 個（新しい順に最大20）：")
        for p in reversed(files[-20:]):
            print(f"  {os.path.basename(p)}  {os.path.getsize(p):,} bytes")
        return 0
    if os.path.basename(a.name) != a.name or not os.path.exists(os.path.join(paths.backups, a.name)):
        print(f"✗ バックアップが見つからない：{a.name}")
        return 1
    lock, rc = ledger_holder(a, paths)
    if lock is None:
        print("  restore は台帳ロックを持つ担当だけが実行できる（ledger-lock --by <担当>）。")
        return rc
    backup_ledger(paths)
    tmp = paths.ledger + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(paths.ledger)), exist_ok=True)
    shutil.copy2(os.path.join(paths.backups, a.name), tmp)
    os.replace(tmp, paths.ledger)
    print(f"✓ {a.name} で台帳を戻した。sha256 = {sha256(paths.ledger)}")
    return 0


def safe_stdio():
    """出力の文字がコンソールの文字コードに無くても、印字の失敗で終了コード1にならないようにする。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


class _Parser(argparse.ArgumentParser):
    """引数の誤りは終了コード 1 にする（2 は「ロックの規則で止めた」に使う）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"使い方の誤り: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_parser():
    ap = _Parser(description="対象ロックと台帳ロック")
    ap.add_argument("--root", help="状態の置き場（既定：WORKFLOW_ROOT、なければ ./.workflow）")
    ap.add_argument("--ledger", help="台帳 CSV（既定：<root>/ledger.csv）")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    def add(name, fn, target=False, **opts):
        p = sub.add_parser(name)
        p.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        p.add_argument("--ledger", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if target:
            p.add_argument("target_id")
        if name != "status":
            p.add_argument("--by", required=name in ("claim", "takeover", "ledger-lock", "ledger-beat",
                                                     "ledger-unlock", "ledger-takeover"), default=None)
        for opt, required in opts.items():
            p.add_argument("--" + opt, required=required, default="")
        p.set_defaults(fn=fn)
        return p

    add("status", cmd_status)
    add("claim", cmd_claim, target=True, note=False)
    add("beat", cmd_beat, target=True)
    add("done", cmd_done, target=True, note=False)
    add("stop", cmd_stop, target=True, reason=False)
    add("takeover", cmd_takeover, target=True, why=True)
    add("ledger-lock", cmd_ledger_lock, purpose=False)
    add("ledger-beat", cmd_ledger_beat)
    add("ledger-unlock", cmd_ledger_unlock)
    add("ledger-takeover", cmd_ledger_takeover, why=True)
    p = add("restore", cmd_restore)
    p.add_argument("name", nargs="?")
    p.add_argument("--list", action="store_true")
    return ap


def main(argv=None):
    safe_stdio()
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a, Paths(a.root, a.ledger))
    except InputError as e:
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
