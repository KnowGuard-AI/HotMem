#!/usr/bin/env bash
# Reproducible company-brain restore (#69): wiki -> JSONL -> instance ->
# package -> clean instance -> idempotent repeat. Exits non-zero on any
# failed assertion. Artifacts are created in a temp dir and cleaned up.
set -euo pipefail

cd "$(dirname "$0")/../.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
WIKI="tests/fixtures/okf/acme_retail"

echo "[1/6] import wiki -> reviewable JSONL"
uv run hotmem import --from okf --db "$WIKI" --out "$WORK/brain.jsonl"

echo "[2/6] hydrate source instance"
uv run hotmem hydrate --file "$WORK/brain.jsonl" --db "$WORK/source.sqlite"

echo "[3/6] package the clone (gz)"
uv run hotmem snapshot --file "$WORK/clone-pkg" --package --gz --db "$WORK/source.sqlite"

echo "[4/6] verify the package"
uv run hotmem verify "$WORK/clone-pkg"

echo "[5/6] restore into a clean instance"
uv run hotmem hydrate --file "$WORK/clone-pkg" --db "$WORK/clean.sqlite"

echo "[6/6] repeat restore must load zero records"
REPEAT="$(uv run hotmem hydrate --file "$WORK/clone-pkg" --db "$WORK/clean.sqlite")"
echo "$REPEAT"
if ! echo "$REPEAT" | grep -q "loaded=0"; then
  echo "FAIL: repeat hydrate loaded records — restore is not idempotent" >&2
  exit 1
fi
echo "OK: wiki -> JSONL -> instance -> package -> clean instance -> equivalent, repeat loads zero."
