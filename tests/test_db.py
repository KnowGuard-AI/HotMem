"""Tests for hotmem.db — SQLite storage and cosine UDF."""

from __future__ import annotations

import sqlite3

import pytest

from hotmem.db import MemoryDB, MemoryRecord
from hotmem.embed import embed_text, pack_embedding


def test_insert_and_count(tmp_db: MemoryDB):
    vec = embed_text("test fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="abc123", identifier="test", fact_text="test fact", embedding=blob)
    assert tmp_db.count() == 1


def test_insert_replace(tmp_db: MemoryDB):
    vec = embed_text("fact one")
    blob = pack_embedding(vec)
    tmp_db.insert(id="same_id", identifier="x", fact_text="fact one", embedding=blob)
    tmp_db.insert(id="same_id", identifier="x", fact_text="fact two", embedding=blob)
    assert tmp_db.count() == 1


def test_exists(tmp_db: MemoryDB):
    vec = embed_text("fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="a", identifier="x", fact_text="fact", embedding=blob, content_hash="hash1")
    assert tmp_db.exists("hash1")
    assert not tmp_db.exists("hash2")


def test_insert_many_ignore_deduplicates_content_hash(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("same fact"))
    inserted = tmp_db.insert_many_ignore(
        [
            MemoryRecord(
                id="bulk1",
                identifier="x",
                fact_text="same fact",
                embedding=blob,
                content_hash="same-hash",
            ),
            MemoryRecord(
                id="bulk2",
                identifier="x",
                fact_text="same fact",
                embedding=blob,
                content_hash="same-hash",
            ),
        ]
    )

    assert inserted == 1
    assert tmp_db.count() == 1


def test_cosine_search(tmp_db: MemoryDB):
    for text in ["the quick brown fox", "hello world", "machine learning is great"]:
        vec = embed_text(text)
        blob = pack_embedding(vec)
        tmp_db.insert(id=text[:5], identifier="test", fact_text=text, embedding=blob)

    query_vec = embed_text("quick fox")
    query_blob = pack_embedding(query_vec)
    results = tmp_db.search_with_cosine(query_blob)

    assert len(results) == 3
    # The fox sentence should rank highest
    assert results[0]["fact_text"] == "the quick brown fox"


def test_fts_search(tmp_db: MemoryDB):
    vec = embed_text("invoice validation required")
    blob = pack_embedding(vec)
    tmp_db.insert(
        id="fts1",
        identifier="test",
        fact_text="invoice validation required",
        embedding=blob,
    )

    results = tmp_db.fts_search("invoice valid")

    assert len(results) == 1
    assert results[0]["id"] == "fts1"
    assert "bm25_score" in results[0]


def test_fts_search_updates_on_replace(tmp_db: MemoryDB):
    vec = embed_text("old invoice text")
    blob = pack_embedding(vec)
    tmp_db.insert(id="same", identifier="test", fact_text="old invoice text", embedding=blob)
    tmp_db.insert(id="same", identifier="test", fact_text="new contract text", embedding=blob)

    assert tmp_db.fts_search("old invoice") == []
    results = tmp_db.fts_search("new contract")
    assert [r["id"] for r in results] == ["same"]


def test_all_rows(tmp_db: MemoryDB):
    vec = embed_text("fact")
    blob = pack_embedding(vec)
    tmp_db.insert(id="r1", identifier="x", fact_text="fact", embedding=blob)
    rows = tmp_db.all_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "r1"
    assert rows[0]["ttl_seconds"] is None


def test_all_rows_can_include_embedding(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(id="r1", identifier="x", fact_text="fact", embedding=blob)

    rows = tmp_db.all_rows(include_embedding=True)

    assert rows[0]["embedding"] == blob


def test_insert_with_ttl(tmp_db: MemoryDB):
    vec = embed_text("temporary fact")
    blob = pack_embedding(vec)
    tmp_db.insert(
        id="ttl1",
        identifier="x",
        fact_text="temporary fact",
        embedding=blob,
        ttl_seconds=3600,
    )

    row = tmp_db.all_rows()[0]
    assert row["ttl_seconds"] == 3600


def test_existing_db_gets_ttl_column(tmp_path):
    db_path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(memories)")}
        assert "ttl_seconds" in columns
    finally:
        db.close()


def test_v2_migration_opens_v01_db(tmp_path):
    db_path = tmp_path / "v01.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            ttl_seconds INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )"""
    )
    blob = pack_embedding(embed_text("legacy fact"))
    conn.execute(
        """INSERT INTO memories (id, identifier, fact_text, embedding)
           VALUES ('legacy1', 'x', 'legacy fact', ?)""",
        (blob,),
    )
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        columns = {row["name"] for row in db._conn.execute("PRAGMA table_info(memories)")}
        for col in (
            "namespace",
            "tier",
            "memory_type",
            "source_uri",
            "source_format",
            "source_checksum",
            "byte_offset",
            "byte_length",
            "updated_at",
            "snapshot_id",
            "promotion_state",
            "promotion_candidate",
            "parent_memory",
            "related_memories",
            "tags",
            "schema_version",
        ):
            assert col in columns, f"missing v2 column: {col}"

        version = db._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 6  # 3 events; 4 checkpoints; 5 handoff ledger; 6 namespace identity

        row = db.all_rows()[0]
        assert row["id"] == "legacy1"
        assert row["promotion_state"] == "HOT"
        assert row["schema_version"] == 1
        assert row["tags"] == "[]"
        assert db.count() == 1
    finally:
        db.close()


def test_v2_fields_roundtrip(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("provenance fact"))
    tmp_db.insert(
        id="v2-1",
        identifier="ns-a",
        fact_text="provenance fact",
        embedding=blob,
        namespace="contracts",
        tier="hot",
        memory_type="fact",
        source_uri="file:///data/contract.pdf",
        source_format="pdf",
        source_checksum="sha256:abc123",
        byte_offset=4096,
        byte_length=128,
        updated_at="2026-07-01T00:00:00Z",
        snapshot_id="snap-1",
        promotion_state="READY",
        promotion_candidate=1,
        parent_memory="parent-1",
        related_memories='["m1","m2"]',
        tags='["legal","contract"]',
        schema_version=2,
    )

    row = tmp_db.all_rows()[0]
    assert row["namespace"] == "contracts"
    assert row["tier"] == "hot"
    assert row["memory_type"] == "fact"
    assert row["source_uri"] == "file:///data/contract.pdf"
    assert row["source_format"] == "pdf"
    assert row["source_checksum"] == "sha256:abc123"
    assert row["byte_offset"] == 4096
    assert row["byte_length"] == 128
    assert row["updated_at"] == "2026-07-01T00:00:00Z"
    assert row["snapshot_id"] == "snap-1"
    assert row["promotion_state"] == "READY"
    assert row["promotion_candidate"] == 1
    assert row["parent_memory"] == "parent-1"
    assert row["related_memories"] == '["m1","m2"]'
    assert row["tags"] == '["legal","contract"]'
    assert row["schema_version"] == 2


def test_v2_fields_default_when_unset(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("plain fact"))
    tmp_db.insert(id="d1", identifier="x", fact_text="plain fact", embedding=blob)

    row = tmp_db.all_rows()[0]
    assert row["namespace"] == ""
    assert row["tier"] == "hot"
    assert row["memory_type"] == "fact"
    assert row["source_uri"] == ""
    assert row["promotion_state"] == "HOT"
    assert row["promotion_candidate"] == 0
    assert row["tags"] == "[]"
    assert row["schema_version"] == 1
    assert row["byte_offset"] is None
    assert row["byte_length"] is None


def test_list_by_identifier_chronological(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(
        id="first",
        identifier="chat",
        fact_text="hello",
        embedding=blob,
        created_at="2026-01-01T00:00:00Z",
    )
    tmp_db.insert(
        id="second",
        identifier="chat",
        fact_text="world",
        embedding=blob,
        created_at="2026-06-01T00:00:00Z",
    )
    tmp_db.insert(
        id="other",
        identifier="other",
        fact_text="noise",
        embedding=blob,
        created_at="2026-03-01T00:00:00Z",
    )

    asc = tmp_db.list_by_identifier("chat", order="asc")
    assert [r["id"] for r in asc] == ["first", "second"]
    assert all("created_at" in r for r in asc)

    desc = tmp_db.list_by_identifier("chat", order="desc")
    assert [r["id"] for r in desc] == ["second", "first"]


def test_list_by_identifier_same_timestamp_uses_insertion_order(tmp_db: MemoryDB):
    for memory_id in ("first", "second", "third"):
        tmp_db.insert(
            id=memory_id,
            identifier="chat",
            fact_text=f"state {memory_id}",
            embedding=pack_embedding(embed_text(f"state {memory_id}")),
            created_at="2026-01-01T00:00:00Z",
        )

    assert [r["id"] for r in tmp_db.list_by_identifier("chat", order="asc")] == [
        "first",
        "second",
        "third",
    ]
    assert [r["id"] for r in tmp_db.list_by_identifier("chat", order="desc")] == [
        "third",
        "second",
        "first",
    ]
    filtered = tmp_db.list_by_identifier("chat", order="desc", row_filter=lambda row: True)
    assert [r["id"] for r in filtered] == ["third", "second", "first"]


def test_list_by_identifier_limit(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    for i in range(5):
        tmp_db.insert(
            id=f"m{i}",
            identifier="chat",
            fact_text=f"msg {i}",
            embedding=blob,
            created_at=f"2026-01-0{i + 1}T00:00:00Z",
        )
    rows = tmp_db.list_by_identifier("chat", order="asc", limit=2)
    assert len(rows) == 2
    assert [r["id"] for r in rows] == ["m0", "m1"]


def test_list_by_identifier_excludes_expired(tmp_db: MemoryDB):
    blob = pack_embedding(embed_text("fact"))
    tmp_db.insert(
        id="live",
        identifier="chat",
        fact_text="alive",
        embedding=blob,
        created_at="2026-01-01T00:00:00Z",
    )
    tmp_db.insert(
        id="dead",
        identifier="chat",
        fact_text="expired",
        embedding=blob,
        ttl_seconds=1,
        created_at="2000-01-01T00:00:00Z",
    )
    rows = tmp_db.list_by_identifier("chat", order="asc")
    assert [r["id"] for r in rows] == ["live"]


def test_namespace_scoped_identity_coexists_and_same_scope_skips(tmp_db: MemoryDB):
    """#100: content identity is (namespace, content_hash)."""
    records = [
        MemoryRecord(
            id="a-1",
            identifier="doc",
            fact_text="same fact",
            embedding=b"",
            content_hash="shared-hash",
            namespace="ns-a",
        ),
        MemoryRecord(
            id="b-1",
            identifier="doc",
            fact_text="same fact",
            embedding=b"",
            content_hash="shared-hash",
            namespace="ns-b",
        ),
        MemoryRecord(
            id="a-2",
            identifier="doc",
            fact_text="same fact",
            embedding=b"",
            content_hash="shared-hash",
            namespace="ns-a",
        ),
    ]

    assert tmp_db.insert_many_ignore(records) == 2
    assert tmp_db.count() == 2
    assert tmp_db.get_memory("a-1") is not None
    assert tmp_db.get_memory("b-1") is not None
    assert tmp_db.get_memory("a-2") is None
    assert tmp_db.exists("shared-hash")
    assert tmp_db.exists("shared-hash", namespace="ns-a")
    assert not tmp_db.exists("shared-hash", namespace="ns-c")


def test_empty_content_hash_is_not_an_identity_key(tmp_db: MemoryDB):
    """#100: empty hashes never match each other or an `exists` lookup."""
    tmp_db.insert_many_ignore(
        [
            MemoryRecord(
                id="empty-1",
                identifier="a",
                fact_text="fact a",
                embedding=b"",
                content_hash="",
                namespace="ns",
            ),
            MemoryRecord(
                id="empty-2",
                identifier="b",
                fact_text="fact b",
                embedding=b"",
                content_hash="",
                namespace="ns",
            ),
        ]
    )

    assert tmp_db.count() == 2
    assert not tmp_db.exists("")
    assert not tmp_db.exists("", namespace="ns")
    assert not tmp_db.find_by_namespace_and_hash("ns", "")
    assert tmp_db.batch_existing_namespace_hashes([("ns", "")]) == set()


def test_import_sqlite_respects_namespace_and_reports_same_scope_skip(tmp_path):
    """#100: SQLite fast-path import must not collapse cross-namespace twins."""
    src = MemoryDB(tmp_path / "src.sqlite")
    dst = MemoryDB(tmp_path / "dst.sqlite")
    try:
        src.insert_many_ignore(
            [
                MemoryRecord(
                    id="src-a",
                    identifier="doc",
                    fact_text="same fact",
                    embedding=b"",
                    content_hash="shared-hash",
                    namespace="ns-a",
                ),
                MemoryRecord(
                    id="src-b",
                    identifier="doc",
                    fact_text="same fact",
                    embedding=b"",
                    content_hash="shared-hash",
                    namespace="ns-b",
                ),
            ]
        )

        assert dst.import_sqlite(tmp_path / "src.sqlite") == (2, 0)
        assert dst.count() == 2
        assert {row["namespace"] for row in dst.all_rows()} == {"ns-a", "ns-b"}

        assert dst.import_sqlite(tmp_path / "src.sqlite") == (0, 2)
        assert dst.count() == 2
    finally:
        src.close()
        dst.close()


def test_import_sqlite_keeps_empty_hash_rows_distinct(tmp_path):
    src = MemoryDB(tmp_path / "src-empty.sqlite")
    dst = MemoryDB(tmp_path / "dst-empty.sqlite")
    try:
        src.insert_many_ignore(
            [
                MemoryRecord(
                    id="e1",
                    identifier="a",
                    fact_text="one",
                    embedding=b"",
                    content_hash="",
                    namespace="ns",
                ),
                MemoryRecord(
                    id="e2",
                    identifier="b",
                    fact_text="two",
                    embedding=b"",
                    content_hash="",
                    namespace="ns",
                ),
            ]
        )

        assert dst.import_sqlite(tmp_path / "src-empty.sqlite") == (2, 0)
        assert dst.import_sqlite(tmp_path / "src-empty.sqlite") == (0, 2)
        assert dst.count() == 2
    finally:
        src.close()
        dst.close()


def test_legacy_migration_is_atomic_when_scoped_duplicates_exist(tmp_path):
    """#100: an ambiguous legacy store fails deterministically and unchanged."""
    db_path = tmp_path / "legacy-duplicates.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT DEFAULT '',
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            ttl_seconds INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            namespace TEXT DEFAULT ''
        )"""
    )
    conn.executemany(
        """INSERT INTO memories (id, identifier, fact_text, content_hash, namespace)
           VALUES (?, 'doc', 'same fact', 'legacy-hash', 'ns')""",
        [("dupe-1",), ("dupe-2",)],
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="duplicate"):
        MemoryDB(db_path)

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
        index = check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_memories_content_hash_unique'"
        ).fetchone()
        assert index is None
        assert check.execute("PRAGMA user_version").fetchone()[0] == 5


def test_legacy_database_rows_keep_identity_when_index_migrates(tmp_path):
    """#100: existing rows survive the v6 index migration and scope later writes."""
    db_path = tmp_path / "legacy-row.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL,
            fact_text TEXT DEFAULT '',
            embedding BLOB,
            embedding_dim INTEGER,
            embedding_model TEXT DEFAULT '',
            source TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT DEFAULT '{}',
            content_hash TEXT DEFAULT '',
            ttl_seconds INTEGER,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            namespace TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX idx_memories_content_hash_unique
           ON memories(content_hash) WHERE content_hash != ''"""
    )
    conn.execute(
        """INSERT INTO memories (id, identifier, fact_text, content_hash, namespace)
           VALUES ('legacy', 'doc', 'same fact', 'legacy-hash', 'ns-a')"""
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    db = MemoryDB(db_path)
    try:
        assert db.get_memory("legacy")["content_hash"] == "legacy-hash"
        db.insert(
            id="fresh",
            identifier="doc",
            fact_text="same fact",
            embedding=b"",
            content_hash="legacy-hash",
            namespace="ns-b",
        )
        assert db.count() == 2
        assert db.batch_existing_namespace_hashes(
            [("ns-a", "legacy-hash"), ("ns-b", "legacy-hash"), ("ns-c", "legacy-hash")]
        ) == {("ns-a", "legacy-hash"), ("ns-b", "legacy-hash")}
    finally:
        db.close()


def test_import_sqlite_applies_target_namespace_to_unscoped_rows(tmp_path):
    """#100: import_sqlite fills target_namespace on unscoped rows and preserves explicit ones."""
    src = MemoryDB(tmp_path / "src-unscoped.sqlite")
    dst = MemoryDB(tmp_path / "dst-target.sqlite")
    try:
        src.insert_many_ignore(
            [
                MemoryRecord(
                    id="u1",
                    identifier="doc",
                    fact_text="unscoped fact",
                    embedding=b"",
                    content_hash="hash-u1",
                    namespace="",
                ),
                MemoryRecord(
                    id="e1",
                    identifier="doc",
                    fact_text="explicit fact",
                    embedding=b"",
                    content_hash="hash-e1",
                    namespace="explicit-ns",
                ),
            ]
        )

        assert dst.import_sqlite(
            tmp_path / "src-unscoped.sqlite", target_namespace="target-ns"
        ) == (2, 0)
        assert dst.get_memory("u1")["namespace"] == "target-ns"
        assert dst.get_memory("e1")["namespace"] == "explicit-ns"

        # Repeating into the same target_namespace skips both.
        assert dst.import_sqlite(
            tmp_path / "src-unscoped.sqlite", target_namespace="target-ns"
        ) == (0, 2)
    finally:
        src.close()
        dst.close()


def test_trim_events_records_trimmed_through_seq_and_preserves_head_watermark(tmp_path):
    """#94: event retention helpers record trimmed_through_seq so trimmed histories
    are distinguishable from empty genesis histories."""
    db = MemoryDB(tmp_path / "trim.sqlite")
    try:
        bounds0 = db.event_log_bounds()
        assert bounds0["event_count"] == 0
        assert bounds0["max_seq"] == 0
        assert bounds0["head_seq"] == 0
        assert bounds0["trimmed_through_seq"] == 0
        assert bounds0["legacy_history"] is False

        seqs = [
            db.append_event(
                event_id=f"evt-{i}",
                event_type="memory.created",
                occurred_at="2026-10-30T00:00:00Z",
                memory_id=f"m-{i}",
            )
            for i in range(1, 6)
        ]
        assert seqs == [1, 2, 3, 4, 5]
        assert db.event_at_seq(3)["event_id"] == "evt-3"

        deleted = db.trim_events_before_seq(3)
        assert deleted == 2
        bounds1 = db.event_log_bounds()
        assert bounds1["min_seq"] == 3
        assert bounds1["max_seq"] == 5
        assert bounds1["head_seq"] == 5
        assert bounds1["trimmed_through_seq"] == 2
        assert bounds1["head_event_id"] == "evt-5"
        assert db.event_at_seq(2) is None

        # Trimming all remaining events (keep=0) retains the head watermark.
        deleted_all = db.trim_events_by_count(0)
        assert deleted_all == 3
        bounds2 = db.event_log_bounds()
        assert bounds2["event_count"] == 0
        assert bounds2["max_seq"] == 0
        assert bounds2["head_seq"] == 5
        assert bounds2["trimmed_through_seq"] == 5
        assert bounds2["head_event_id"] == "evt-5"
    finally:
        db.close()


def test_pre_94_database_with_rows_marks_legacy_history_on_open(tmp_path):
    """#94: opening a pre-#94 database that already has memories/events without
    mount_id in store_meta backfills mount/replica/history IDs and sets legacy_history."""
    db_path = tmp_path / "legacy94.sqlite"
    db = MemoryDB(db_path)
    db.insert(
        id="leg-1",
        identifier="legacy/doc",
        fact_text="legacy row before mount identity",
        embedding=b"",
        content_hash="legacy-hash-94",
    )
    # Simulate a pre-#94 database that had rows before mount_id/replica_id/history_id existed.
    db._conn.execute(
        "DELETE FROM store_meta "
        "WHERE key IN ('mount_id', 'replica_id', 'history_id', 'legacy_history')"
    )
    db.commit()
    db.close()

    reopened = MemoryDB(db_path)
    try:
        ident = reopened.mount_identity()
        assert ident.mount_id.startswith("mnt-")
        assert ident.replica_id.startswith("rep-")
        assert ident.history_id.startswith("hist-")
        assert ident.legacy_history is True
    finally:
        reopened.close()


def test_mount_manifest_identity_mismatch_fails_closed(tmp_path):
    import json

    from hotmem.mount import bootstrap_mount

    mount_dir = tmp_path / "mismatch_mount"
    cfg = bootstrap_mount(mount_dir, mount_id="mnt-original")
    db = MemoryDB(cfg.db_path)
    assert db.mount_identity().mount_id == "mnt-original"
    db.close()

    manifest = json.loads(cfg.manifest_path.read_text())
    manifest["mount_id"] = "mnt-other"
    cfg.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="refusing to mix logical identities"):
        MemoryDB(cfg.db_path)


def test_legacy_mount_manifest_is_backfilled_from_store_identity(tmp_path):
    import json

    from hotmem.mount import bootstrap_mount

    mount_dir = tmp_path / "legacy_mount_94"
    mount_dir.mkdir()
    manifest_path = mount_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "hotmem_version": "0.1.0",
                "created_at": "2024-01-01T00:00:00Z",
                "mount_path": str(mount_dir),
            }
        )
    )
    cfg = bootstrap_mount(mount_dir)
    assert cfg.mount_id == ""

    db = MemoryDB(cfg.db_path)
    try:
        ident = db.mount_identity()
        assert ident.mount_id.startswith("mnt-")
        assert ident.mount_bound is True
    finally:
        db.close()

    persisted = json.loads(manifest_path.read_text())
    assert persisted["mount_id"] == ident.mount_id
    assert persisted["created_at"] == "2024-01-01T00:00:00Z"

    reopened = MemoryDB(cfg.db_path)
    try:
        assert reopened.mount_identity().mount_id == ident.mount_id
    finally:
        reopened.close()


def test_trim_events_restores_recursive_triggers_pragma(tmp_path):
    db = MemoryDB(tmp_path / "pragma.sqlite")
    try:
        db.append_event(
            event_id="evt-1",
            event_type="memory.created",
            occurred_at="2026-10-30T00:00:00Z",
        )
        db.append_event(
            event_id="evt-2",
            event_type="memory.created",
            occurred_at="2026-10-30T00:00:00Z",
        )
        db.trim_events_before_seq(2)
        enabled = db._conn.execute("PRAGMA recursive_triggers").fetchone()[0]
        assert int(enabled) == 1
    finally:
        db.close()
