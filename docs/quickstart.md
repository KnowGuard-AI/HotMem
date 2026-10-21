# Quickstart

## 1. Purpose

This quickstart gets a local HotMem sidecar running, stores one memory, searches
it, and shows how to move memory with JSONL snapshots.

## 2. Install

```bash
pip install hotmem
# or
uv pip install hotmem
```

Supports Python 3.11, 3.12, 3.13, and 3.14.

For native Windows setup, MCP configuration, verification, and the measured
support boundary, see [Windows qualification](#windows-qualification).

## 3. Start the Server

```bash
# Start with a mount directory (portable memory)
hotmem serve --mount ./hotmem

# Or just start (uses temp DB)
hotmem serve
```

## 4. Add and Search Memories

```bash
# Add a memory
curl -X POST http://127.0.0.1:8711/v1/add \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "project", "fact": "uses FastAPI and SQLite"}'

# Search
curl -X POST http://127.0.0.1:8711/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "what stack does the project use"}'
```

## 5. Portable Memory

```bash
# Export to a swap file
hotmem snapshot --file swap.jsonl --db ./hotmem/hotmem.sqlite

# Hydrate on another machine
hotmem hydrate --file swap.jsonl --db ./my.sqlite

# Or export/restore a verified Snapshot v2 directory (macOS / Linux / Windows PowerShell)
hotmem snapshot --file ./snapshot-v2 --db ./hotmem/hotmem.sqlite
hotmem verify ./snapshot-v2
hotmem hydrate --file ./snapshot-v2 --db ./my.sqlite
```

JSONL remains a stable compatibility format. Snapshot v2 directories are
available when a manifest and integrity checks are useful.

## 6. Use the Python Client

```python
from hotmem.client import HotMemClient

client = HotMemClient("http://127.0.0.1:8711")
client.add(identifier="user", fact="likes Rust")
results = client.search(query="programming preferences")
```

## 7. Docker

```bash
docker run -p 8711:8711 -v ./data:/data knowguard/hotmem
```

See [CLI](cli.md) for the full command reference and [API Reference](api.md) for endpoints.

## Compatibility rules

- `/v1/add` accepts `identifier` and `fact`.
- `/v1/search` returns LLM-ready message objects by default.
- JSONL hydrate/snapshot remains supported.
- File-native features must be additive.

## Windows qualification

The Windows release scope is the existing hash-only core: installed CLI,
Python/HTTP client, MCP stdio, persistent SQLite mounts, Snapshot v2,
legacy JSONL/JSONL.GZ, file references, and verified handoff/interchange.
The broader daemon and relay feature track is separate.

The published `hotmem==0.2.6` includes the original Snapshot v2 byte-integrity
correction. The additional file-URI and MCP Snapshot v2 changes described
here belong to [PR #121](https://github.com/KnowGuard-AI/HotMem/pull/121).
Installing the existing PyPI package does not install those PR changes.

### Install and reproduce the candidate on PowerShell

Use an x64 CPython installation and a writable local filesystem. Git is
required for the candidate checkout; `uv` is only needed for the optional
locked developer-install check. No activation script or execution-policy
change is required. From an empty working directory:

```powershell
git clone https://github.com/KnowGuard-AI/HotMem.git
Set-Location HotMem
git fetch origin pull/121/head
git switch --detach FETCH_HEAD
py -3.12 -m venv .venv-check
.\.venv-check\Scripts\python.exe -m pip install ".[dev,mcp,okf]"
.\.venv-check\Scripts\hotmem.exe --version
.\.venv-check\Scripts\python.exe -m pytest -q -ra
```

Use `-3.11`, `-3.13`, or `-3.14` with a separate environment to check another
supported interpreter. CI builds a wheel and installs it in an otherwise
clean environment before running this suite; it does not use editable source.

### One persistent server and a clean restore

In the first PowerShell terminal:

```powershell
.\.venv-check\Scripts\hotmem.exe serve --host 127.0.0.1 --port 8711 --mount ".\Windows test mount" --embedder hash --reranker none
```

In a second terminal in the same checkout:

```powershell
@'
from pathlib import Path
from hotmem.client import HotMemClient
with HotMemClient("http://127.0.0.1:8711") as client:
    assert client.health()["status"] == "ok"
    client.add("windows-check", "Synthetic invoice approval note", source="manual-check")
    assert client.search("invoice approval", top_k=1)
    client.snapshot(str(Path("Windows verified snapshot").resolve()))
'@ | .\.venv-check\Scripts\python.exe -
```

Stop the server with Ctrl+C in its terminal before running offline commands:

```powershell
.\.venv-check\Scripts\hotmem.exe verify ".\Windows verified snapshot"
.\.venv-check\Scripts\hotmem.exe hydrate --file ".\Windows verified snapshot" --db ".\clean-restored.sqlite"
.\.venv-check\Scripts\hotmem.exe hydrate --file ".\Windows verified snapshot" --db ".\clean-restored.sqlite"
.\.venv-check\Scripts\hotmem.exe search "invoice approval" --db ".\clean-restored.sqlite" --json
```

The first hydrate loads one record and reuses its stored embedding. The
second loads zero and reports one skipped duplicate. Reopening the original
mount retains its acknowledged record. The automated process test also
abruptly kills and restarts the source server, checks unchanged retrieval,
rejects a corrupt snapshot without mutation, and removes all temporary files.

For only the process checks:

```powershell
.\.venv-check\Scripts\python.exe -m pytest tests/test_cli.py tests/test_mcp.py -q
```

### Paths, provenance, and diagnostics

- Quote paths containing spaces. JSON MCP arguments are an array; each path
  is one argument. Use absolute executable and mount paths in GUI host configs;
  see the [Windows MCP example](https://github.com/KnowGuard-AI/HotMem/tree/main/examples/mcp_claude_desktop#windows).
- UTF-8 snapshots have LF record terminators on every OS. Do not edit or
  normalize an exported payload, bypass checksum verification, or rebuild its
  manifest to conceal changed bytes. Git attributes preserve checked-in
  checksummed fixtures even with Windows `core.autocrlf=true`.
- Bare paths and local `file:///C:/...` URIs are supported. Standard
  percent-encoded spaces/Unicode and `file://localhost/...` resolve locally.
  Remote file authorities/UNC shares are outside the qualified boundary.
- File-backed hydration verifies the referenced byte range. Snapshot v2
  preserves references; it does not automatically relocate backing files.
  Copy required sources explicitly and preserve relative layout in the new
  mount. A missing source or changed checksum produces HTTP 409 with a
  provenance reason. Optional copied attachments do not imply automatic
  reference rewriting.
- `hotmem mcp` reserves stdout for MCP messages; diagnostics use stderr.
  When launching HTTP sidecars, inherit stderr or write it to a file. A
  subprocess launcher that captures a pipe must drain it and must always
  terminate/wait on success and failure. The tests use bounded startup and
  cleanup deadlines and check that Windows releases SQLite/file handles.
  Windows console entry points can have a Python child: killing only
  `hotmem.exe` can leave the server running. Use Ctrl+C for an interactive
  server, manage the full process tree (`taskkill /PID <pid> /T /F`) for a
  disposable background server, or launch the owning Python process directly
  with `python -c "from hotmem.cli import main; main()" serve ...`.
- If a port is occupied, stop the owning process or choose a different port.
  If MCP support is missing, install the `[mcp]` extra into the same environment
  as the executable referenced by the host. A locked checkout must use the
  root-package correction in #121; `uv sync --locked` is the developer check.
- Delete only disposable test mounts, snapshots, and restored databases after
  stopping their processes. Keep your persistent mount when restarting.

### Evidence boundary

Native results and exact revisions are recorded in PR #121 and in the
`Windows core` jobs of [CI](https://github.com/KnowGuard-AI/HotMem/actions/workflows/ci.yml).
Tests include the full core suite and actual installed CLI/HTTP/MCP processes.
Optional semantic models, Chroma, framework adapters, GUI-host interaction,
other Windows OS builds, ARM64, network/removable filesystems, and service-manager
installation need separate qualification. No performance or broad platform
certification follows from these correctness checks.
