# Expected showcase output

Healthy-run transcript with volatile values elided (`<…>`). The script
exits non-zero on the first failing step, so reaching the end IS the check.

```
work dir: /tmp/hotmem-showcase.XXXXXX

== 1/5 prepare (resume) ==
handoff-prepare
  already_applied: False
  handoff_id: <uuid32>
  memories: 2
  mode: resume
  omissions: 7
  package_id: <sha256-12>
  redactions: 1
  entries: 11
  path: /tmp/hotmem-showcase.XXXXXX/pkg
  total_ms: <float>

== 2/5 verify ==
handoff-verify
  entries: 11
  memories: 2
  mode: resume
  package_id: <sha256-12>
  valid: True

== 3/5 inspect (coverage: omissions + redactions) ==
inspect: ok — 11 entries, 2 memories, 7 omissions, 1 redaction

== 4/5 hydrate (fresh target; repeat proves idempotence) ==
handoff-hydrate
  ... loaded: 3 ... already_applied: False ...
repeat hydrate: ok — already_applied=True, zero writes

== 5/5 retrieve through the normal search path ==
search: ok — brief retrieved with source ids and the no-re-run marker

== bonus: archive mode keeps the full ordered stream ==
archive: ok — 12 entries, no resume bounding

showcase complete: /tmp/hotmem-showcase.XXXXXX
```

Notes:

- The redaction count of 1 is the fixture's pasted `HOTMEM_API_KEY` — the
  value never appears in any package artifact; the name survives.
- The 7 omissions are 5 unsupported source fields, 1 policy-denied hidden
  prompt, and the 1 middle turn bounded by resume mode (recoverable from
  the source; archive mode keeps it).
- The hydrated target holds 3 records: 2 durable memories and the resume
  brief at `handoff/hotmem-showcase-planning/resume-brief`.