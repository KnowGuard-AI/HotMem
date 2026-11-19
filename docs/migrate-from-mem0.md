# Move from mem0 in five minutes

This guide imports the current state recorded in a local mem0 SQLite history
database. It does not read mem0's vector-store files or call a hosted API.

## 1. Find the history database

Use the `history.db` path from your mem0 configuration. The common local
default is `~/.mem0/history.db`, but custom stores can live elsewhere. Make a
copy first if you want to preserve an untouched source artifact.

The importer has been checked against the current upstream
[`SQLiteManager` schema](https://github.com/mem0ai/mem0/blob/main/mem0/memory/storage.py)
and its [storage tests](https://github.com/mem0ai/mem0/blob/main/tests/memory/test_storage.py).

## 2. Import into a new HotMem database

```bash
hotmem import --from mem0 \
  --db ~/.mem0/history.db \
  --target ./hotmem.sqlite \
  --out ./mem0-review.jsonl
```

The importer replays `ADD`, `UPDATE`, and `DELETE` history rows and writes one
record for each live mem0 memory. The latest text and timestamp are imported;
`actor_id` becomes the HotMem identifier. HotMem generates new memory IDs and
embeddings, then deduplicates repeat imports by content.

## 3. Check the imported rows

Open `mem0-review.jsonl` to review exactly what HotMem will load. Then run a
local search against the new database:

```bash
hotmem search "a fact you remember" --db ./hotmem.sqlite --json
```

If your HotMem runtime uses the local semantic embedder, pass the same
`--embedder local-semantic` option to `hotmem import`; otherwise the import
uses the dependency-free hash embedder.

## Scope

`--from mem0` accepts mem0's SQLite history database. It does not read the
separate Qdrant, Chroma, or other vector-store backend, and it does not accept
Mem0 Platform's structured API export. Source embeddings are not reused.
