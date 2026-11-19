# Move from Cognee in five minutes

HotMem reads Cognee's COGX export format. Cognee's current COGX writer emits a
directory with `manifest.json` and JSONL files for each record kind; its
optional `.cogx.tar.gz` archive is also accepted. See Cognee's
[COGX record definitions](https://github.com/topoteretes/cognee/blob/main/cognee/modules/migration/cogx.py)
and [export implementation](https://github.com/topoteretes/cognee/blob/main/cognee/modules/migration/export.py).

## 1. Export your Cognee dataset

Run this against the dataset in your existing Cognee environment:

```python
import asyncio
import cognee


async def main():
    await cognee.export(
        dataset="my_dataset",
        format="cogx",
        destination="./cognee-export",
    )


asyncio.run(main())
```

Do not pass `include_permissions=True`. Cognee's optional `permissions.json`
contains account details and password hashes. HotMem ignores that file.

## 2. Import into a new HotMem database

```bash
hotmem import --from cognee \
  --db ./cognee-export \
  --target ./hotmem.sqlite \
  --out ./cognee-review.jsonl
```

You can pass the `.cogx.tar.gz` archive instead of the export directory.
HotMem checks the manifest, record counts, archive paths, and each record
checksum when one is present.

## 3. Review and search

Review `cognee-review.jsonl`, then try a local query:

```bash
hotmem search "a fact from the Cognee dataset" --db ./hotmem.sqlite --json
```

HotMem maps typed documents, episodes, entities, facts, memories, memory
blocks, and text-bearing raw graph nodes into textual memories. It preserves
source IDs, scope, tags, and supported provenance in metadata. It does not
rebuild Cognee's graph links as a graph. Source embeddings are not reused; to
use the local semantic embedder, pass `--embedder local-semantic` during
import after provisioning the model.

The import reads local files and does not call an LLM or hosted provider.
