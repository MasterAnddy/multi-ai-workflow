# リリースゲートの例（対象 T003）

架空のリリースフォルダ。`CHANGELOG.md` と `docs/usage.md` をレビューし、
そのハッシュをマニフェスト `release_review.json` に結びつけてある。
レビューの証拠は `review.md`（リリースレビュー記録）で、対象 ID と成果物のハッシュを書いてある。

```
python tools/release_gate.py check examples/release_gate
python tools/release_gate.py hash examples/release_gate/CHANGELOG.md examples/release_gate/docs/usage.md
```

そのまま実行すると合格する（終了コード 0）。

成果物（`CHANGELOG.md` か `docs/usage.md`）を1文字でも編集して `check` を実行すると、
次の3つのエラーが出て不合格になる（終了コード 2）。編集したファイルごとに1組ずつ出る。

- `artifact_invalid`：成果物の sha256 がマニフェストの値と一致しない。
- `review_invalid`：レビュー時のハッシュと現物が一致しない。
- `evidence_invalid`：`review.md` に、編集後のファイルの sha256 が書かれていない。

通すには、レビューをやり直して `review.md` とマニフェストのハッシュを更新する。
この `README.md` は成果物ではないため、マニフェストで `internal` として除外している。
