# dispatch_guard の例

指示書を担当へ渡す前に、重複と競合を機械で止めるツールの例である。
リポジトリ直下で実行する。状態は `--root` で指したフォルダ（省略時は `./.workflow`）に作られる。

## 通る指示書

```
python tools/dispatch_guard.py --file examples/dispatch_guard/instruction_pass.txt --root demo_wf
python tools/dispatch_guard.py --file examples/dispatch_guard/instruction_pass.txt --root demo_wf --send
```

1回目は検査だけで、終了コードは 0。2回目で `demo_wf/inbox/TASK-20261201-AB1.txt` を作り、`deliveries.jsonl` に1行足す。
台帳の更新に触れていないので、台帳更新の一文が末尾に足される。同じ指示書をもう一度 `--send` すると、重複で止まる（終了コード 2）。

## 止まる指示書

```
python tools/dispatch_guard.py --file examples/dispatch_guard/instruction_blocked.txt --root demo_wf
```

`instruction_blocked.txt` を止めるのは台帳のチェックだけで、終了コードは 2 である。
「台帳は更新しない」と書いてあるため止まる。「公開する」「デプロイする」という取り消せない操作も含むので、`--no-writeback "理由"` を付けても通らない（メッセージは「取り消せない操作を含むため --no-writeback では通せない」）。
`--no-writeback` で通せるのは、取り消せない操作を含まない指示書だけである。

通すには、指示書から「台帳は更新しない」の行を消す。台帳更新の一文が足されて、チェックを通過する（終了コード 0）。

対象 `T102` のロックは、この例では作っていないので止める理由にならない。`demo_wf` に `T102` のロックがあると、台帳の問題がなくても止まる。
その場合は、記録の担当を読んだうえで `--seen T102=worker-a` のように指定すると通る。

終了コードは 0 が通過、2 が停止、1 が入力の誤りである。
