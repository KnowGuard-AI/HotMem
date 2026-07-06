# Plan: File Inspectors (#53) + Compatibility Golden Tests (#54)

Branch: `feat/file-inspectors-golden-tests-53-54` (off `main` @ 0f0b8da)
Milestone target: M1 (#54, P0) + M5 (#53, P1/P2). Recommended order in
`docs/okf/file-native-memory-practices.md` §13 is: golden tests first, then
inspectors — so #54 lands first to lock the contract, then #53 adds behind it.

## 0. Design philosophy (the 10x posture)

- **Compatibility is the product.** Every new line is additive. The non-breaking
  contract from `file-aware-architecture.md` §5 must become *executable*, not
  aspirational — that is the entire point of #54.
- **Filesystem first, never a query engine.** #53 inspects *about* files, not
  *into* them analytically. No DuckDB/Polars/Arrow runtime dependency. Parquet
  support is **metadata-only via a dependency-free Thrift Compact footer
  reader** — this is the disruptive-in-scope bet: production-grade Parquet
  provenance with **zero** new deps, staying inside the "HotMem owns vs EMOS
  owns" boundary (`file-aware-architecture.md` §4).
- **Provenance over copy.** Inspectors never pull large file contents into
  SQLite. They return URI + size + checksum + byte ranges + light summary so
  memories can *reference* data (#38 enabler) without inflating the hot store.
- **Streaming + seek, no full loads.** CSV uses the stdlib `csv` reader with a
  bounded sniff; JSONL counts newlines via a buffered byte scan and samples by
  `seek`; Parquet reads only the footer tail. Memory footprint is O(1) for the
  metadata path regardless of file size — this is what makes it safe on
  production workloads.
- **Fail loudly, fail structured.** Unsupported formats raise
  `UnsupportedFormatError` with an actionable message, mirroring the existing
  `UnsupportedSchemeError` in `storage/__init__.py`.
- **Local-only surface, for now.** Inspectors ship as a Python API + a
  `hotmem inspect` CLI subcommand. No `/v1` or MCP endpoints — those belong to
  #43 (API extensions) and would widen the compatibility surface #54 is locking.

## 1. Issue #54 — Compatibility golden tests (lands first)

Goal: make the non-breaking contract executable *before* more vNext code lands.

### Layout
```
tests/golden/
  __init__.py
  conftest.py            # shared golden fixtures (stable server + deterministic clock)
  fixtures/
    add_minimal.jsonl        # canonical {identifier, fact} add payload
    add_extended.jsonl       # {identifier, fact, source, importance, metadata, ttl_seconds}
    search_expected.json     # locked /v1/search message-object shape
    memories_expected.json   # locked /v1/memories row shape
  test_golden_api.py
  test_golden_swap.py
  test_golden_client.py
  test_golden_mcp.py
  test_golden_additive.py
```

### What gets locked
1. **API shapes** (`test_golden_api.py`): exact top-level key sets and value
   *types* for `/v1/health`, `/v1/add`, `/v1/search`, `/v1/memories`,
   `/v1/hydrate`, `/v1/snapshot`. Volatile fields (`memory_id`, `content_hash`,
   `created_at`, `trace_ms`, `uptime_s`, `db_path`) are masked to a stable
   sentinel so snapshots are deterministic. Search message-object shape
   (`role`/`content`/`memory_id`/`identifier`/`score`/`created_at`) is locked
   against `fixtures/search_expected.json`.
2. **Swap round trips** (`test_golden_swap.py`): JSONL and JSONL.GZ
   snapshot→hydrate→snapshot are byte-shape stable (key set + ordering of the
   first record), and a v0.1-style minimal record hydrates identically to a
   v2-extended record. Locks the existing compatibility promise in
   `file-native-memory-practices.md` §10.
3. **Python client** (`test_golden_client.py`): `HotMemClient` and
   `AsyncHotMemClient` method names, request payloads (vs a `MockTransport`),
   and return shapes are locked. This is the surface `test_client.py` already
   exercises informally — golden tests make it a *contract*.
4. **MCP** (`test_golden_mcp.py`): tool-name set is an exact frozen set;
   each tool's `inputSchema` top-level keys and `required` arrays are locked.
   Backed by `test_mcp.py`'s existing wiring but asserting schema stability
   specifically (the part that breaks clients when it drifts).
5. **Additive proof** (`test_golden_additive.py`): a `POST /v1/add` with *only*
   the legacy `{identifier, fact}` payload produces a memory whose
   `/v1/search` and `/v1/memories` shapes are *identical* to one created with
   the full extended payload minus the new fields. This is the executable
   form of "new optional fields do not change default behavior" (#54 scope).

### Determinism strategy
- Use `TestClient` against a temp DB (existing pattern).
- Mask volatile keys into typed sentinels (`"<uuid>"`, `"<hash>"`,
  `"<ts>"`, `"<float>"`) before comparing, so snapshots are stable across
  runs but still type-locked.

## 2. Issue #53 — Lightweight file inspectors

### Layout
```
src/hotmem/inspectors/
  __init__.py            # registry: get_inspector(uri), UnsupportedFormatError, inspect_file()
  base.py                # FileInspector Protocol, FileInspection dataclass
  csv_inspector.py
  jsonl_inspector.py
  _thrift.py             # minimal Thrift Compact Protocol reader (production)
  parquet_inspector.py   # footer-only metadata reader, no data-page decode
```

### `FileInspection` contract (`base.py`)
A frozen dataclass that pairs storage provenance with format-specific metadata,
designed so a future #38 file-backed memory can store it directly:

```python
@dataclass(frozen=True)
class FileInspection:
    uri: str
    format: str                       # csv | jsonl | parquet | ...
    size: int
    mtime: float
    checksum: str                     # sha256 from the storage adapter
    columns: list[str] | None         # CSV headers / Parquet schema names
    row_count: int | None             # None when not cheap to compute
    delimiter: str | None              # CSV only
    has_header: bool | None            # CSV only
    num_row_groups: int | None         # Parquet only
    schema_types: list[str] | None     # Parquet column types
    sample: list[dict] | None          # bounded JSONL/CSV preview
    byte_ranges: list[tuple[int, int]] | None  # provenance offsets for sample
    metadata: dict[str, Any]          # format-specific extras
    unsupported_reason: str | None    # set when format is recognized but limited
```

### Inspectors
- **CSV** (`csv_inspector.py`): `csv.Sniffer` over a bounded head buffer for
  delimiter + header detection; columns from the header row; `row_count`
  computed only when `count_rows=True` via a streaming `\n` scan (cheap,
  optional — matches issue's "optional row count when cheap"). Never loads the
  whole file into memory. Sample = first N rows via the reader.
- **JSONL** (`jsonl_inspector.py`): line count via buffered newline scan (O(file
  size) byte read, O(1) memory). Range sampling via `seek(offset)` +
  `readline()` to fetch selected records without parsing the whole file.
  Validates each sampled line is JSON; reports first malformed line offset in
  `unsupported_reason` rather than crashing.
- **Parquet** (`parquet_inspector.py` + `_thrift.py`): validate `PAR1` magic
  at head and tail; read last 8 bytes → footer length (LE uint32); read footer;
  parse Thrift Compact `FileMetaData` → `version`, `num_rows`, `schema`
  (column names + converted_type/physical type), `row_groups` count. **No data
  page decoding, no query engine.** If `pyarrow` is installed at runtime it is
  *not* used (deterministic, zero-dep path is the contract). Malformed/legacy
  footers set `unsupported_reason` instead of raising, so a bad file becomes
  provenance, not an outage.

### Registry (`__init__.py`)
```python
def get_inspector(uri: str) -> FileInspector: ...   # by format from storage.metadata()
def inspect_file(uri: str, *, count_rows=False, sample_size=5) -> FileInspection: ...
```
Unsupported formats → `UnsupportedFormatError` (mirrors `storage`'s
`UnsupportedSchemeError`). Scheme resolution reuses `storage.get_adapter` so
remote schemes fail fast with the existing EMOS-boundary error.

### CLI
`hotmem inspect <uri> [--count-rows] [--sample N] [--json]` — additive, uses
`get_renderer()` for human output and `--json` for scripting (matches the
`search` command's `--json` convention in `cli.py`).

### Tests (`tests/test_inspectors.py`)
- Fixtures generated in `tmp_path` (deterministic, no committed binaries):
  - CSV with header + 3 rows, comma and `;` delimiters.
  - JSONL with 5 records incl. one malformed line.
  - Parquet via a small Thrift-Compact footer encoder in
    `tests/_parquet_fixtures.py` producing a valid footer (num_rows, schema,
    1 row group) — exercises the real parser on a real footer shape.
- Assertions: stable metadata, no full-file copy into any DB, malformed JSONL
  reported not raised, unsupported format raises `UnsupportedFormatError`,
  Parquet returns metadata-only (no row data), existing hydrate/search tests
  unchanged.

## 3. Production hardening notes

- All inspectors are read-only and side-effect free; safe to call concurrently.
- Parquet footer read is bounded (footer length capped at a sane max to reject
  hostile files: `len(file) - 8` sanity-checked).
- `lru_cache` on checksum is already provided by the storage adapter; the
  inspector reuses `storage.get_adapter(...).checksum(uri)` rather than
  re-hashing.
- No new runtime dependencies; `[dev]` unchanged. CI matrix (py3.11–3.14)
  unaffected — pure stdlib.
- Ruff: `target-version = "py311"`, line-length 100, existing rule set
  (`E,F,I,UP,B,SIM`). All new code conforms.

## 4. Out of scope (deferred to owning issues)
- HTTP/MCP endpoints for inspection → #43.
- File-backed memory hydration from inspection → #38.
- Bundle readers, directory snapshots, event log, promotion → #39–#42.
- Parquet data-page decoding, partitioning, query → EMOS.

## 5. Verification
- `uv run pytest -q` — all existing 150 + new tests green.
- `uv run ruff check src tests` — clean.
- Spot-check `hotmem inspect --json <fixture>` output shape.
