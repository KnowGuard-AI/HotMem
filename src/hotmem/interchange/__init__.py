"""HotMem interchange — canonical record, digest, and compatibility primitives.

Purpose:
     Shared normalization, validation, canonical serialization, and embedding
     compatibility for every HotMem record stream format (legacy JSONL with
     ``embedding_b64``, Snapshot v2 with ``embedding``, and the
     ``hotmem-interchange-v1`` contract). One code path so the readers cannot
     drift apart (contract: ``docs/okf/interchange-v1.md``, issue #67).

Interface:
      canonical.canonical_dumps(record) -> str
      canonical.compute_content_hash(identifier, fact_text) -> str
      canonical.logical_id(content_hashes) -> str
      record.normalize_record(raw) -> dict
      record.validate_record(record) -> list[str]
      compat.compatible_embedding_blob(record) -> bytes | None

Deps: stdlib + hotmem.embed constants only (no db/snapshot imports — the
      package must stay importable from both readers without cycles).
Extension: package publishing/verification live in interchange.package and
      interchange.hydrate (#69).
"""

from hotmem.interchange import canonical, compat, record
from hotmem.interchange.canonical import compute_content_hash, logical_id
from hotmem.interchange.compat import compatible_embedding_blob
from hotmem.interchange.record import normalize_record, validate_record

__all__ = [
    "canonical",
    "compat",
    "compatible_embedding_blob",
    "compute_content_hash",
    "logical_id",
    "normalize_record",
    "record",
    "validate_record",
]
