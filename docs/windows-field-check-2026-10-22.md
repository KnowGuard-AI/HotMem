# Windows field check — 2026-10-22

This was an independent, hands-on confidence check alongside the v0.2.7 epic.
It used a disposable workspace and synthetic data only; it is not a release gate.

## Environment

- Windows 10 Pro 26H2, build 26300.9711, AMD64
- Python 3.14.4
- HotMem 0.2.6 at commit `8f741243a0844a0673599481f8eb02bd210a44af`
- MCP 1.30.0
- MCP host: Python MCP SDK stdio client using the Claude Desktop-compatible
  absolute executable/argument shape. No preconfigured Claude Desktop, Cursor,
  or Windsurf GUI host was present on this workstation.

## Results

| Check | Result | Evidence |
|---|---|---|
| Fresh install and start | PASS | `pip install ".[dev,mcp,okf]"`; loopback server on `127.0.0.1:8711`; persistent mount path contained spaces and non-ASCII characters. |
| Local API round trip and restart | PASS | Synthetic marker `WFC-2026-CAFE-7F3A` was added, searched, and found after stopping and restarting HotMem. |
| File-backed Windows path | PASS | Percent-encoded local `file:///C:/...` URI resolved a 66-byte synthetic file whose path contained spaces and non-ASCII characters; hydration matched exactly. |
| Malformed local file URI | PASS | `file:///bad%FF.txt` returned HTTP 400 with `unsupported_scheme`; the server remained healthy. |
| MCP handoff prepare/verify | PASS | Bundled `examples/handoff_showcase` fixture produced 11 entries, 2 memories, 7 omissions, and 1 redaction. |
| Tamper rejection | PASS | A modified package was rejected with `size_mismatch` while the fresh target remained at 0 memories. |
| MCP hydrate/search/repeat | PASS | Valid hydrate loaded 3 records; the resume brief was searchable; repeat hydrate returned `already_applied=true`, `loaded=0`, with the target still at 3 records. |

## Windows friction

- The Bash `run_showcase.sh` requires PowerShell translation: quoted paths,
  `.`-prefixed virtual-environment executables, and PowerShell equivalents for
  `mktemp`, `grep`, and Unix line continuations.
- `Start-Process -ArgumentList` split an unquoted path containing spaces and
  non-ASCII characters. Structured Windows argument passing preserved it.
- A default CP1252 MCP host console could not print the fixture's Unicode arrow;
  UTF-8 output resolved the issue.
- Supplying a session id different from the fixture envelope produced a clear
  structured MCP validation error; omitting it allowed the fixture default.

No HotMem defect was found, so no linked bug was filed. The disposable workspace
and test processes were removed after the run, and no real memories or secrets
were used.
