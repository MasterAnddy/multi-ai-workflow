# ledger_cas の例
台帳 `ledger.csv` へ `patch.json` を、書いた時点の値（expect）が今も同じときだけ反映する。
先に `<root>/ledger.lock`（`by` が `worker-a`）を置いてから、次を実行する。
`python tools/ledger_cas.py examples/ledger_cas/patch.json --by worker-a --root <root> --ledger examples/ledger_cas/ledger.csv --dry-run`
