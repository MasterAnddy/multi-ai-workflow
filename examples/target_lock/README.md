# target_lock の使用例

状態は `.workflow/`（`--root` か環境変数 `WORKFLOW_ROOT` で変更）に JSON で置かれる。

```
# 対象 T001 を取得し、節目で beat を打ち、完了で解除する
python tools/target_lock.py claim T001 --by worker-a --note "仕様の確認"
python tools/target_lock.py beat T001 --by worker-a
python tools/target_lock.py done T001 --by worker-a --note "完了"

# 台帳（ledger.csv）を書く間だけロックする。取得時にバックアップが取られる
python tools/target_lock.py ledger-lock --by worker-a --purpose "T001 の結果を記入"
python tools/target_lock.py ledger-unlock --by worker-a

# 保持者が止まって STALE になったときだけ引き継ぐ
python tools/target_lock.py status
python tools/target_lock.py takeover T001 --by reviewer-b --why "worker-a が停止"
```
