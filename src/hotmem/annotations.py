"""Lossless annotation envelope — reserved ``metadata.annotations`` (#79).

Purpose:
    HotMem is the lossless transport for enrichment: external tools may
    attach namespaced annotations (entities, aliases, typed relationships,
    classifications, provenance) to canonical records, and those
    annotations survive every movement path — JSONL, JSONL.GZ, Snapshot v2,
    interchange packages, repeat hydration, one-way delta replay — without
    HotMem interpreting, ranking on, or verifying them. Preservation,
    provenance, deterministic merge, lossless movement: that is the whole
    contract.

Envelope (schema 1), carried inside ``metadata_json`` under the reserved
``annotations`` key — no new columns, no new record keys, and fingerprinted
through the existing metadata channel so annotation changes are
integrity-protected and conflict-visible for free:

    {
      "schema_version": 1,
      "namespaces": {
        "org.example.entities": [
          {"id": "vendor-x", "type": "Organization", "confidence": 0.92,
           "evidence": [{"memory_id": "..."}], ...supplied fields preserved...}
        ]
      },
      "producers": {"org.example.enricher": {"version": "1.2", ...}}
    }

Rules — validation is STRUCTURAL, never semantic:
    - The envelope must be an object with ``schema_version`` 1; ``namespaces``
      maps reverse-DNS-style namespace names to LISTS of item objects.
    - Every item must be a dict with a non-empty string ``id``; ids are
      unique within a namespace. ``confidence``, when supplied, is a finite
      number in [0, 1]. ``evidence``, when supplied, is a list whose entries
      are ``{"memory_id": ...}`` local references (resolved against the
      incoming package and the existing target when a resolver is given;
      dangling local references are actionable errors) or
      ``{"uri": ...}`` / plain strings (external URIs preserved verbatim,
      never fetched, never executed).
    - Unknown namespaces and unknown keys are preserved as JSON values —
      canonicalized object key order, preserved array order. Malformed KNOWN
      structure fails validation with actionable errors; unknown content
      never does.
    - Enterprise provenance fields (scope, authority, valid_time,
      recorded_time, classification) are preserved as supplied — their
      semantics stay with the producer.
    - Limits: 128 KiB serialized envelope, 1000 items total, JSON depth 8.

Merge (deterministic, by namespace + item id):
    - same id + same content -> no-op (idempotent replay)
    - disjoint ids -> combined
    - same id + different content -> an explicit conflict: the target
      version is retained, the incoming version is preserved in the merge
      outcome's conflict record, and neither is silently dropped.
      Conflicts never advance to last-write-wins.

Interface:
    AnnotationValidationError — actionable structural failure
    validate_annotations(annotations, known_ids?) — validation
    validate_metadata(metadata, known_ids?) — envelope hook for record paths
    merge_annotations(target, incoming) -> AnnotationMergeOutcome

Deps: none beyond the standard library (JSON-only contract).
Extension: schema 2+ is a new envelope version here, never a rewrite of
    stored data (schema_version gates validation).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from hotmem.trace import get_tracer

_trace = get_tracer("annotations")

SCHEMA_VERSION = 1
MAX_ENVELOPE_BYTES = 128 * 1024
MAX_ITEMS = 1000
MAX_DEPTH = 8

_NAMESPACE_MIN_LABELS = 2


class AnnotationValidationError(ValueError):
    """Structural envelope violation with an actionable message (#79)."""


def _fail(path: str, message: str) -> None:
    raise AnnotationValidationError(f"metadata.annotations{path}: {message}")


def _check_depth(value: Any, depth: int = 0, path: str = "") -> None:
    if depth > MAX_DEPTH:
        _fail(path, f"exceeds maximum JSON depth {MAX_DEPTH}")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_depth(item, depth + 1, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _check_depth(item, depth + 1, f"{path}[{idx}]")


def _validate_namespace_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        _fail(".namespaces", f"namespace key must be a non-empty string (got {name!r})")
    labels = name.split(".")
    if len(labels) < _NAMESPACE_MIN_LABELS:
        _fail(
            ".namespaces",
            f"namespace {name!r} must be reverse-DNS style with at least "
            f"{_NAMESPACE_MIN_LABELS} labels (e.g. org.example.entities)",
        )
    import re

    for label in labels:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", label):
            _fail(
                ".namespaces",
                f"namespace {name!r} label {label!r} must be lowercase "
                "alphanumerics/hyphens starting and ending alphanumerically",
            )


def _validate_item(item: Any, namespace: str, index: int, known_ids: set[str] | None) -> None:
    path = f".namespaces.{namespace}[{index}]"
    if not isinstance(item, dict):
        _fail(path, "annotation items must be objects")
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        _fail(path, "every annotation item requires a non-empty string 'id'")
    confidence = item.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            _fail(f"{path}.confidence", "must be a number when supplied")
        if not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
            _fail(f"{path}.confidence", f"must be a finite number in [0, 1] (got {confidence!r})")
    evidence = item.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            _fail(f"{path}.evidence", "must be a list when supplied")
        for i, entry in enumerate(evidence):
            _validate_evidence_entry(entry, f"{path}.evidence[{i}]", known_ids)


def _validate_evidence_entry(entry: Any, path: str, known_ids: set[str] | None) -> None:
    if isinstance(entry, str):
        if not entry.strip():
            _fail(path, "external evidence strings must be non-empty")
        return  # external URI, preserved verbatim, never fetched
    if not isinstance(entry, dict):
        _fail(path, "evidence entries must be objects or external URI strings")
    if "memory_id" in entry:
        memory_id = entry["memory_id"]
        if not isinstance(memory_id, str) or not memory_id.strip():
            _fail(path, "local evidence requires a non-empty 'memory_id'")
        if known_ids is not None and memory_id not in known_ids:
            _fail(
                path,
                f"local evidence references unknown memory {memory_id!r}; local "
                "references must resolve against the incoming package or the "
                "existing target (forward references included)",
            )
        unknown = set(entry) - {"memory_id"}
        if unknown:
            # Unknown keys on a local reference are preserved, not an error.
            return
        return
    if "uri" in entry:
        uri = entry["uri"]
        if not isinstance(uri, str) or not uri.strip():
            _fail(path, "external evidence requires a non-empty 'uri'")
        return  # external URI, preserved verbatim
    _fail(path, "evidence entries must carry 'memory_id' (local) or 'uri' (external)")


def envelope_exceeds_limits(annotations: Any) -> bool:
    """True when the serialized envelope breaches the documented limits.

    Used both by validation and by the merge path (#79): merged output is
    re-checked before it is persisted, so repeated hydration of colliding
    records can never grow a row's envelope past MAX_ENVELOPE_BYTES /
    MAX_ITEMS — the merge is refused instead (target retained, incoming
    preserved via the conflict channel semantics).
    """
    if not isinstance(annotations, dict):
        return False
    serialized = json.dumps(annotations, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        return True
    namespaces = annotations.get("namespaces") or {}
    return (
        isinstance(namespaces, dict)
        and sum(len(items) for items in namespaces.values() if isinstance(items, list)) > MAX_ITEMS
    )


def _check_limits(annotations: Any) -> None:
    if not isinstance(annotations, dict):
        return
    serialized = json.dumps(annotations, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        _fail(
            "",
            f"serialized envelope exceeds {MAX_ENVELOPE_BYTES} bytes "
            f"(got {len(serialized.encode('utf-8'))})",
        )


def validate_annotations(
    annotations: Any,
    *,
    known_ids: set[str] | None = None,
) -> None:
    """Validate the envelope structurally; raise AnnotationValidationError.

    ``known_ids`` — when provided — enables local evidence resolution: every
    ``{"memory_id": ...}`` reference must appear in it (the incoming package's
    ids plus the existing target's ids; forward references included). Without
    a resolver, local references are checked structurally only.
    """
    if not isinstance(annotations, dict):
        _fail("", "the envelope must be a JSON object")
    _check_depth(annotations)

    version = annotations.get("schema_version")
    if version != SCHEMA_VERSION:
        _fail(
            ".schema_version",
            f"must be {SCHEMA_VERSION} (got {version!r}); future envelope "
            "versions are not silently reinterpreted",
        )

    _check_limits(annotations)

    namespaces = annotations.get("namespaces")
    if namespaces is None:
        namespaces = {}
    if not isinstance(namespaces, dict):
        _fail(".namespaces", "must be an object mapping namespaces to item lists")

    total_items = 0
    for name, items in namespaces.items():
        _validate_namespace_name(name)
        if not isinstance(items, list):
            _fail(f".namespaces.{name}", "namespace value must be a list of item objects")
        seen_ids: set[str] = set()
        for index, item in enumerate(items):
            _validate_item(item, name, index, known_ids)
            if item["id"] in seen_ids:
                _fail(
                    f".namespaces.{name}[{index}]",
                    f"duplicate item id {item['id']!r} within namespace {name!r}",
                )
            seen_ids.add(item["id"])
        total_items += len(items)
        if total_items > MAX_ITEMS:
            _fail(
                ".namespaces",
                f"envelope exceeds {MAX_ITEMS} total items (limit reached at {name!r})",
            )

    producers = annotations.get("producers")
    if producers is not None and not isinstance(producers, dict):
        _fail(".producers", "must be an object when supplied")


def validate_metadata(
    metadata: dict[str, Any] | None,
    *,
    known_ids: set[str] | None = None,
) -> None:
    """Validate the reserved ``annotations`` envelope inside record metadata.

    Metadata without an ``annotations`` key is untouched (zero overhead for
    annotation-less records — one dict membership check).
    """
    if not isinstance(metadata, dict) or "annotations" not in metadata:
        return
    validate_annotations(metadata.get("annotations"), known_ids=known_ids)


# ── deterministic merge (#79) ────────────────────────────────────────────────


@dataclass
class AnnotationConflict:
    """A same-id/different-content collision — neither version is lost."""

    namespace: str
    item_id: str
    target: dict[str, Any]
    incoming: dict[str, Any]


@dataclass
class AnnotationMergeOutcome:
    """Result of merging an incoming envelope into a target's."""

    merged: dict[str, Any] | None  # None when nothing changed
    merged_items: int = 0
    conflicts: list[AnnotationConflict] = field(default_factory=list)


def _items_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Canonical equality: object key order never matters, array order does."""
    return json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(
        b, sort_keys=True, separators=(",", ":")
    )


def merge_annotations(
    target: Any,
    incoming: Any,
) -> AnnotationMergeOutcome:
    """Merge ``incoming`` into ``target`` deterministically (id-level rules).

    Both arguments are envelope objects (``metadata["annotations"]``); either
    may be ``None``. The merged envelope is a NEW dict (inputs untouched);
    ``merged`` is ``None`` when the merge is a pure no-op (idempotent
    replay). Conflicts retain the target's version and record the incoming
    version — never last-write-wins, never a silent drop.
    """
    if not incoming:
        return AnnotationMergeOutcome(merged=None)
    if not target:
        # Nothing to merge into: adopt the incoming envelope wholesale.
        return AnnotationMergeOutcome(merged=dict(incoming), merged_items=_count_items(incoming))

    incoming_namespaces = incoming.get("namespaces") or {}
    target_namespaces = dict(target.get("namespaces") or {})
    result_namespaces = {name: list(items) for name, items in target_namespaces.items()}
    conflicts: list[AnnotationConflict] = []
    merged_items = 0
    changed = False

    for name in sorted(incoming_namespaces):
        incoming_items = incoming_namespaces[name]
        target_items = result_namespaces.setdefault(name, [])
        by_id = {item["id"]: item for item in target_items}
        for item in incoming_items:
            existing = by_id.get(item["id"])
            if existing is None:
                result_namespaces[name].append(item)
                by_id[item["id"]] = item
                merged_items += 1
                changed = True
            elif not _items_equal(existing, item):
                conflicts.append(
                    AnnotationConflict(
                        namespace=name, item_id=item["id"], target=existing, incoming=item
                    )
                )
                # Target version retained; incoming preserved in the conflict.

    producers = dict(target.get("producers") or {})
    producers.update(incoming.get("producers") or {})
    producers_changed = producers != (target.get("producers") or {})

    if not changed and not producers_changed:
        return AnnotationMergeOutcome(merged=None, conflicts=conflicts)

    merged = {
        "schema_version": target.get("schema_version", SCHEMA_VERSION),
        "namespaces": result_namespaces,
    }
    if producers:
        merged["producers"] = producers
    # Unknown top-level keys from either side are preserved verbatim.
    for source in (target, incoming):
        for key, value in source.items():
            if key not in merged and key not in ("namespaces", "producers", "schema_version"):
                merged.setdefault(key, value)
    return AnnotationMergeOutcome(merged=merged, merged_items=merged_items, conflicts=conflicts)


def _count_items(envelope: dict[str, Any]) -> int:
    return sum(len(items) for items in (envelope.get("namespaces") or {}).values())


def merge_duplicate_annotations(
    db: Any,
    incoming_records: list[dict[str, Any]],
    counters: dict[str, int],
    *,
    commit: bool = False,
) -> None:
    """Merge incoming envelopes into content-hash-duplicate target rows (#79).

    Called by every hydrate flush path with the records whose canonical
    content already exists (``skipped`` rows): same text but a different
    annotation envelope merges by namespace + item id — disjoint items are
    added (in-transaction, deterministic serialized form), same-id/same-
    content items are no-ops, and same-id/different-content items are
    explicit conflicts: the target version is retained, the incoming
    version preserved in the outcome, never last-write-wins, never a silent
    content-hash dedup drop.

    Merged output is re-checked against the documented limits before it is
    persisted: a merge that would breach MAX_ENVELOPE_BYTES / MAX_ITEMS is
    refused (target retained) with a trace diagnostic, so repeated
    hydration of colliding records can never grow a row's envelope
    unboundedly.

    ``commit``: package hydration passes ``False`` inside its explicit
    all-or-nothing transaction; the v2 and legacy flush paths pass ``True``
    because their per-batch inserts auto-commit and no later commit exists
    to carry an all-duplicate batch's merge.

    ``counters["annotations_merged"]`` counts merged items;
    ``counters["annotation_conflicts"]`` counts conflicting items. Rows
    without annotations on either side cost one dict check — zero work.
    """
    if not incoming_records:
        return
    incoming_with_annotations = [
        r
        for r in incoming_records
        if isinstance(r.get("metadata"), dict) and "annotations" in (r["metadata"] or {})
    ]
    if not incoming_with_annotations:
        return

    hashes = [r["content_hash"] for r in incoming_with_annotations]
    existing_rows = db.fetch_metadata_by_hashes(hashes)
    for rec in incoming_with_annotations:
        target_row = existing_rows.get(rec["content_hash"])
        if not target_row:
            continue
        target_meta = target_row.get("metadata")
        if not isinstance(target_meta, dict):
            continue
        incoming_meta = rec["metadata"]
        outcome = merge_annotations(
            target_meta.get("annotations"), incoming_meta.get("annotations")
        )
        counters["annotation_conflicts"] += len(outcome.conflicts)
        if outcome.merged is None:
            continue
        if envelope_exceeds_limits(outcome.merged):
            # Refuse the merge: the target stays intact and the limit
            # breach is observable rather than an unbounded write.
            _trace.warn(
                "annotations",
                "refusing merge that would exceed envelope limits",
                detail={"id": target_row["id"]},
            )
            continue
        merged_meta = {**target_meta, "annotations": outcome.merged}
        # Deterministic serialized form: canonical object key order, so
        # metadata identity never depends on map insertion order (#79).
        db.update_metadata_json(
            target_row["id"], json.dumps(merged_meta, sort_keys=True), commit=commit
        )
        counters["annotations_merged"] += outcome.merged_items
