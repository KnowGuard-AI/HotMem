#!/usr/bin/env bash
# Session handoff showcase — Codex → HotMem → Claude (#101).
#
# Clean-room: no cloud account, no network, no provider secret, no manual
# database editing. Exits non-zero the moment any step fails.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${1:-$(mktemp -d /tmp/hotmem-showcase.XXXXXX)}"
CONSENT="showcase demo consent: capture this session for the handoff walkthrough"

mkdir -p "$WORK"
echo "work dir: $WORK"

echo
echo "== 1/5 prepare (resume) =="
uv run --project "$HERE/../.." hotmem handoff prepare \
    --source "$HERE/codex_export" \
    --out "$WORK/pkg" \
    --mode resume \
    --consent "$CONSENT"

echo
echo "== 2/5 verify =="
uv run --project "$HERE/../.." hotmem handoff verify "$WORK/pkg"

echo
echo "== 3/5 inspect (coverage: omissions + redactions) =="
uv run --project "$HERE/../.." hotmem handoff inspect "$WORK/pkg" --json > "$WORK/inspect.json"
python3 - "$WORK/inspect.json" <<'PY'
import json, sys

report = json.load(open(sys.argv[1]))
assert report["valid"] is True, report
assert report["mode"] == "resume"
assert report["counts"]["entries"] == 11
assert report["counts"]["memories"] == 2
assert report["coverage"]["omitted_count"] == 7
assert report["coverage"]["redacted_count"] == 1
assert any("hidden prompts" in o["reason"] for o in report["coverage"]["omitted"])
print("inspect: ok — 11 entries, 2 memories, 7 omissions, 1 redaction")
PY

echo
echo "== 4/5 hydrate (fresh target; repeat proves idempotence) =="
uv run --project "$HERE/../.." hotmem handoff hydrate "$WORK/pkg" --db "$WORK/target.sqlite"
uv run --project "$HERE/../.." hotmem handoff hydrate "$WORK/pkg" --db "$WORK/target.sqlite" | tee "$WORK/hydrate2.txt"
python3 - "$WORK/hydrate2.txt" <<'PY'
import sys

out = sys.argv[1] and open(sys.argv[1]).read()
assert "already_applied" in out and "True" in out, "repeat hydrate not a no-op"
print("repeat hydrate: ok — already_applied=True, zero writes")
PY

echo
echo "== 5/5 retrieve through the normal search path =="
uv run --project "$HERE/../.." hotmem search --db "$WORK/target.sqlite" "resume brief handoff showcase" \
    | tee "$WORK/search.txt"
grep -q "handoff/hotmem-showcase-planning/resume-brief" "$WORK/search.txt"
grep -q "do not re-run" "$WORK/search.txt"
echo "search: ok — brief retrieved with source ids and the no-re-run marker"

echo
echo "== bonus: archive mode keeps the full ordered stream =="
uv run --project "$HERE/../.." hotmem handoff prepare --source "$HERE/codex_export" --out "$WORK/archive" \
    --mode archive --consent "$CONSENT"
uv run --project "$HERE/../.." hotmem handoff verify "$WORK/archive"
python3 - "$WORK/archive" <<'PY'
import json, sys

manifest = json.load(open(sys.argv[1] + "/manifest.json"))
assert manifest["counts"]["entries"] == 12, "archive must keep all 12 entries"
assert manifest["coverage"]["omitted_count"] == 6
print("archive: ok — 12 entries, no resume bounding")
PY

echo
echo "showcase complete: $WORK"