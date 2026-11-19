#!/usr/bin/env python3
"""Generate the deterministic retrieval-eval fixtures (#77).

Purpose:
     Emits bench/retrieval/corpus.jsonl (1,000 synthetic memories) and
     bench/retrieval/queries.jsonl (368 graded queries, >=40 per required
     category) with stable ids and frozen temporal fields. Content is
     synthetic and safe to publish: no keys, no personal data, no copied
     text.

     Design notes:
       - The original 60-memory / 48-query fixtures are frozen separately
         for the historical baseline. This generator appends 40 examples in
         each category, including 40 project collisions, 40 duplicate groups,
         40 frozen revision pairs, and 40 no-answer queries.
       - Five hundred deterministic operational distractors exercise ranking
         against a larger store without referencing graded answer ids.
       - Cross-project queries carry their intended namespace; ordinary runs
         remain unscoped unless the evaluator option is supplied.

Interface:
      uv run python bench/retrieval/gen_fixtures.py   # rewrites both scale files

Deps: stdlib only. Output is byte-identical across runs.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def _mem(mid: str, identifier: str, ns: str, fact: str, **extra) -> dict:
    rec = {
        "memory_id": mid,
        "identifier": identifier,
        "namespace": ns,
        "fact": fact,
        "importance": 0.5,
        "tags": [],
    }
    rec.update(extra)
    return rec


def _q(
    qid: str,
    category: str,
    query: str,
    relevance: dict[str, int],
    notes: str,
    *,
    namespace: str | None = None,
) -> dict:
    record = {
        "query_id": qid,
        "category": category,
        "query": query,
        "relevance": relevance,
        "notes": notes,
    }
    if namespace is not None:
        record["namespace"] = namespace
    return record


def _scale_fixtures() -> tuple[list[dict], list[dict]]:
    """Build deterministic, category-balanced scale coverage (#141 WP3)."""
    corpus: list[dict] = []
    queries: list[dict] = []
    namespaces = ("finance", "logistics", "infra", "hr")

    # Exact lexical lookup over unique operational records.
    for i in range(40):
        code = f"ARC-{i:03d}"
        memory_id = f"scale-exact-{i:03d}"
        days = 14 + (i * 7) % 73
        corpus.append(
            _mem(
                memory_id,
                f"records.archive-{code.lower()}",
                namespaces[i % len(namespaces)],
                f"Archive {code} retains signed delivery receipts for {days} days.",
            )
        )
        queries.append(
            _q(
                f"q-scale-exact-{i:03d}",
                "exact_lexical",
                f"How many days does archive {code} retain signed delivery receipts?",
                {memory_id: 3},
                "Generated exact entity and fact lookup at scale.",
            )
        )

    # Meaning-preserving paraphrases use varied but ordinary operational language.
    resources = (
        ("sealed claim packets", "locked storage"),
        ("returned access badges", "secure bins"),
        ("damaged parcel labels", "a restricted cabinet"),
        ("retired signing keys", "offline storage"),
    )
    for i in range(40):
        code = f"SITE-{i:03d}"
        memory_id = f"scale-semantic-{i:03d}"
        item, place = resources[i % len(resources)]
        days = 3 + (i * 5) % 41
        corpus.append(
            _mem(
                memory_id,
                f"operations.retention-{code.lower()}",
                namespaces[(i + 1) % len(namespaces)],
                f"At {code}, {item} remain in {place} for {days} days before disposal.",
            )
        )
        queries.append(
            _q(
                f"q-scale-semantic-{i:03d}",
                "semantic_paraphrase",
                f"How long must {code} keep those {item} before they can be discarded?",
                {memory_id: 3},
                "Generated paraphrase lookup at scale.",
            )
        )

    # Identifier lookups exercise distinctive IDs amid a larger corpus.
    for i in range(40):
        code = f"MTR-{i:03d}-Q"
        memory_id = f"scale-identifier-{i:03d}"
        limit = 48 + (i * 11) % 97
        corpus.append(
            _mem(
                memory_id,
                f"infra.telemetry.{code.lower()}",
                namespaces[(i + 2) % len(namespaces)],
                f"Metric {code} raises a pressure warning above {limit} kilopascals.",
            )
        )
        queries.append(
            _q(
                f"q-scale-identifier-{i:03d}",
                "identifier_or_entity_name",
                f"What pressure reading triggers the warning for metric {code}?",
                {memory_id: 3},
                "Generated identifier lookup at scale.",
            )
        )

    # Forty version pairs each have a frozen timestamp and an explicitly graded
    # older answer, so the suite checks current-vs-obsolete retrieval at scale.
    for i in range(40):
        code = f"ROUTE-{i:03d}"
        old_id = f"scale-revision-{i:03d}-old"
        new_id = f"scale-revision-{i:03d}-current"
        old_limit = 20 + (i * 3) % 30
        new_limit = old_limit + 7 + (i % 9)
        namespace = namespaces[(i + 3) % len(namespaces)]
        corpus.extend(
            (
                _mem(
                    old_id,
                    f"service.breaker.{code.lower()}",
                    namespace,
                    f"The {code} circuit breaker timeout was {old_limit} seconds.",
                    created_at="2025-02-01T09:00:00Z",
                ),
                _mem(
                    new_id,
                    f"service.breaker.{code.lower()}",
                    namespace,
                    f"The current {code} circuit breaker timeout is {new_limit} seconds.",
                    created_at="2026-08-01T09:00:00Z",
                ),
            )
        )
        queries.append(
            _q(
                f"q-scale-temporal-{i:03d}",
                "temporal_or_revision",
                f"What is the current timeout for the {code} circuit breaker?",
                {new_id: 3, old_id: 1},
                "Generated current-revision lookup over a frozen revision pair.",
            )
        )

    # Forty duplicate groups provide 120 related records with a known grouping.
    inspection_items = (
        "badge printer",
        "audit queue",
        "packing station",
        "archive index",
    )
    for i in range(40):
        code = f"UNIT-{i:03d}"
        representative = f"scale-duplicate-{i:03d}-a"
        interval = 2 + (i % 11)
        item = inspection_items[i % len(inspection_items)]
        group = (
            (
                f"The {item} at {code} is inspected every {interval} weeks.",
                f"scale-duplicate-{i:03d}-b",
            ),
            (
                f"Teams inspect the {code} {item} once per {interval}-week cycle.",
                f"scale-duplicate-{i:03d}-c",
            ),
        )
        namespace = namespaces[i % len(namespaces)]
        corpus.append(
            _mem(
                representative,
                f"operations.inspection.{code.lower()}",
                namespace,
                f"Inspection of the {item} at {code} happens every {interval} weeks.",
            )
        )
        for fact, memory_id in group:
            corpus.append(
                _mem(
                    memory_id,
                    f"operations.inspection.{code.lower()}",
                    namespace,
                    fact,
                    duplicate_of=representative,
                )
            )
        queries.append(
            _q(
                f"q-scale-duplicate-{i:03d}",
                "near_duplicate_diversity",
                f"How often is the {item} at {code} inspected?",
                {
                    representative: 3,
                    f"scale-duplicate-{i:03d}-b": 3,
                    f"scale-duplicate-{i:03d}-c": 3,
                },
                "All members of the generated duplicate group answer the query.",
            )
        )

    # No-answer cases deliberately share ordinary domain terms with corpus rows.
    for i in range(40):
        code = f"UNLISTED-{i:03d}"
        queries.append(
            _q(
                f"q-scale-negative-{i:03d}",
                "negative_or_no_answer",
                f"Which encryption key is assigned to storage locker {code}?",
                {},
                "Generated no-answer query; this locker has no stored key record.",
            )
        )

    # Same entity names in finance and logistics create controlled collisions.
    for i in range(40):
        entity = f"Beacon {i:03d}"
        finance_id = f"scale-cross-{i:03d}-finance"
        logistics_id = f"scale-cross-{i:03d}-logistics"
        credit = 1200 + (i * 137) % 9800
        transit = 2 + (i * 3) % 19
        corpus.extend(
            (
                _mem(
                    finance_id,
                    f"finance.beacon-{i:03d}",
                    "finance",
                    f"{entity} supplier invoices require review above EUR {credit}.",
                ),
                _mem(
                    logistics_id,
                    f"logistics.beacon-{i:03d}",
                    "logistics",
                    f"{entity} freight reaches the regional hub in {transit} days.",
                ),
            )
        )
        if i % 2:
            namespace = "finance"
            query = f"In finance, at what EUR amount does {entity} need invoice review?"
            relevance = {finance_id: 3, logistics_id: 1}
        else:
            namespace = "logistics"
            query = f"In logistics, how many days does {entity} freight take to reach the hub?"
            relevance = {logistics_id: 3, finance_id: 1}
        queries.append(
            _q(
                f"q-scale-cross-{i:03d}",
                "cross_project_isolation",
                query,
                relevance,
                "Generated cross-project entity collision.",
                namespace=namespace,
            )
        )

    # Stable facts run through the evaluator's clone-and-hydrate comparison.
    for i in range(40):
        code = f"NODE-{i:03d}"
        memory_id = f"scale-snapshot-{i:03d}"
        window = 6 + (i * 3) % 12
        corpus.append(
            _mem(
                memory_id,
                f"operations.maintenance.{code.lower()}",
                namespaces[(i + 1) % len(namespaces)],
                f"Maintenance window for {code} begins at {window:02d}:00 UTC each Sunday.",
            )
        )
        queries.append(
            _q(
                f"q-scale-snapshot-{i:03d}",
                "snapshot_hydration_equivalence",
                f"When does the weekly maintenance window for {code} begin?",
                {memory_id: 3},
                "Generated stable lookup checked before and after clone plus hydrate.",
            )
        )

    # Fill the remainder with varied, plausible non-answer operational facts.
    # These make every query compete with 1,000 stored memories rather than a
    # hand-picked set of 60, without duplicating any query targets.
    for i in range(500):
        code = f"ASSET-{i:04d}"
        namespace = namespaces[i % len(namespaces)]
        region = ("north", "central", "coastal", "western")[i % 4]
        facts = (
            f"Asset {code} in the {region} region reports inventory every {2 + i % 13} hours.",
            f"The {code} service ticket is routed to the {region} support desk.",
            f"Operations checks {code} during the {region} shift handover.",
            f"The {code} record is retained in the {region} audit ledger.",
            f"Weekly status for {code} is reviewed by the {region} team lead.",
        )
        corpus.append(
            _mem(
                f"scale-decoy-{i:04d}",
                f"operations.asset.{code.lower()}",
                namespace,
                facts[i % len(facts)],
            )
        )

    return corpus, queries


CORPUS = [
    # ── exact-lexical targets ────────────────────────────────────────────
    _mem(
        "mem-invoice-001",
        "finance.acme-invoices",
        "finance",
        "Acme vendor invoices require two-person approval above EUR 5,000.",
    ),
    _mem(
        "mem-password-001",
        "infra.password-rotation",
        "infra",
        "Password rotation runs every 90 days for production servers.",
    ),
    _mem(
        "mem-backup-001",
        "infra.db-backups",
        "infra",
        "Database backups run nightly at 02:00 UTC with 30-day retention.",
    ),
    _mem(
        "mem-expense-001",
        "finance.expense-receipts",
        "finance",
        "Expense reports over EUR 400 require a receipt attached.",
    ),
    _mem(
        "mem-onboard-001",
        "hr.onboarding-day1",
        "hr",
        "New hires receive laptop and badge on their first day during onboarding.",
    ),
    _mem(
        "mem-deploy-001",
        "infra.deploy-window",
        "infra",
        "Production deploys happen only on Tuesday and Thursday mornings.",
    ),
    _mem(
        "mem-refund-001",
        "logistics.refund-window",
        "logistics",
        "Customers can request a refund within 30 days of delivery.",
    ),
    _mem(
        "mem-ratelimit-001",
        "infra.api-rate-limit",
        "infra",
        "The public API allows 100 requests per minute per API key.",
    ),
    # ── semantic-paraphrase targets ──────────────────────────────────────
    _mem(
        "mem-badge-001",
        "hr.badge-lifecycle",
        "hr",
        "Access badges deactivate automatically 30 days after an employee leaves the company.",
    ),
    _mem(
        "mem-standup-001",
        "infra.daily-sync",
        "infra",
        "The team meets every morning at 09:30 for a fifteen-minute sync.",
    ),
    _mem(
        "mem-vpn-001",
        "infra.remote-access",
        "infra",
        "Remote engineers must connect through the VPN before reaching internal tools.",
    ),
    _mem(
        "mem-expensetool-001",
        "finance.travel-costs",
        "finance",
        "Employees submit travel costs through the finance portal within two weeks.",
    ),
    _mem(
        "mem-mentor-001",
        "hr.engineer-mentoring",
        "hr",
        "Every new engineer is paired with a senior colleague for their first three months.",
    ),
    _mem(
        "mem-outage-001",
        "infra.incident-response",
        "infra",
        "When the main service goes down, the on-call engineer "
        "pages the incident channel immediately.",
    ),
    # ── entity lookups, with two deliberate cross-project name collisions ─
    _mem(
        "mem-meridian-fin-001",
        "finance.meridian",
        "finance",
        "Meridian is the payment processor charging 1.9 percent per transaction.",
    ),
    _mem(
        "mem-meridian-log-001",
        "logistics.meridian",
        "logistics",
        "Meridian is the freight carrier we book for overseas shipments.",
    ),
    _mem(
        "mem-northwind-fin-001",
        "finance.northwind",
        "finance",
        "Northwind vendor invoices require two-person approval above EUR 5,000.",
    ),
    _mem(
        "mem-northwind-log-001",
        "logistics.northwind",
        "logistics",
        "Northwind freight requires a 48-hour lead time for overseas bookings.",
    ),
    _mem(
        "mem-falcon-001",
        "infra.falcon",
        "infra",
        "Falcon is the internal monitoring dashboard built by the platform team.",
    ),
    _mem(
        "mem-quill-001",
        "hr.quill",
        "hr",
        "Quill is the documentation wiki maintained by HR and engineering.",
    ),
    _mem(
        "mem-aurora-001",
        "finance.aurora",
        "finance",
        "Aurora is the budgeting spreadsheet owned by the finance operations lead.",
    ),
    _mem(
        "mem-orchid-001",
        "logistics.orchid",
        "logistics",
        "Orchid is the returns-processing vendor for damaged parcels.",
    ),
    # ── temporal revision pairs (frozen created_at) ──────────────────────
    _mem(
        "mem-deadline-old-001",
        "finance.report-deadline",
        "finance",
        "The quarterly report deadline is the 5th day after quarter end.",
        created_at="2026-01-10T09:00:00Z",
    ),
    _mem(
        "mem-deadline-new-001",
        "finance.report-deadline",
        "finance",
        "The quarterly report deadline moved to the 10th day after quarter end.",
        created_at="2026-06-01T09:00:00Z",
    ),
    _mem(
        "mem-portal-old-001",
        "infra.vendor-portal",
        "infra",
        "The vendor portal login page is portal.example.com/old.",
        created_at="2026-02-01T09:00:00Z",
    ),
    _mem(
        "mem-portal-new-001",
        "infra.vendor-portal",
        "infra",
        "The vendor portal moved to portal.example.com/v2 with single sign-on.",
        created_at="2026-07-15T09:00:00Z",
    ),
    _mem(
        "mem-leadtime-old-001",
        "logistics.lead-time",
        "logistics",
        "Standard delivery lead time is two weeks for most regions.",
        created_at="2026-03-01T09:00:00Z",
    ),
    _mem(
        "mem-leadtime-new-001",
        "logistics.lead-time",
        "logistics",
        "Standard delivery lead time improved to five business days for most regions.",
        created_at="2026-08-01T09:00:00Z",
    ),
    # ── near-duplicate groups ────────────────────────────────────────────
    _mem(
        "mem-handbook-001",
        "hr.handbook-review",
        "hr",
        "The employee handbook is reviewed once per year by HR and legal.",
    ),
    _mem(
        "mem-handbook-002",
        "hr.handbook-review",
        "hr",
        "HR and legal review the employee handbook once per year.",
        duplicate_of="mem-handbook-001",
    ),
    _mem(
        "mem-handbook-003",
        "hr.handbook-review",
        "hr",
        "The staff handbook gets its yearly review from the HR and legal teams.",
        duplicate_of="mem-handbook-001",
    ),
    _mem(
        "mem-cache-001",
        "infra.search-cache",
        "infra",
        "The search cache refreshes every ten minutes during business hours.",
    ),
    _mem(
        "mem-cache-002",
        "infra.search-cache",
        "infra",
        "During business hours the search cache is refreshed every ten minutes.",
        duplicate_of="mem-cache-001",
    ),
    _mem(
        "mem-cache-003",
        "infra.search-cache",
        "infra",
        "Search cache refresh interval is ten minutes when the office is open.",
        duplicate_of="mem-cache-001",
    ),
    _mem(
        "mem-travel-001",
        "finance.travel-approval",
        "finance",
        "International travel bookings need manager approval before ticketing.",
    ),
    _mem(
        "mem-travel-002",
        "finance.travel-approval",
        "finance",
        "Managers must approve international travel bookings before tickets are issued.",
        duplicate_of="mem-travel-001",
    ),
    _mem(
        "mem-travel-003",
        "finance.travel-approval",
        "finance",
        "Before ticketing, international trips require the manager's approval.",
        duplicate_of="mem-travel-001",
    ),
    # ── distractors (distinct vocabulary) ────────────────────────────────
    _mem(
        "mem-sunny-001",
        "social.weather-log",
        "social",
        "The weather log for June 3rd records clear skies and a light breeze.",
    ),
    _mem(
        "mem-soup-001",
        "social.soup-recipe",
        "social",
        "The canteen's pumpkin soup uses roasted garlic and a pinch of nutmeg.",
    ),
    _mem(
        "mem-running-001",
        "social.running-club",
        "social",
        "The office running club meets Thursdays at the riverside path.",
    ),
    _mem(
        "mem-photo-001",
        "social.photo-contest",
        "social",
        "The annual photo contest accepts entries until the end of October.",
    ),
    _mem(
        "mem-plants-001",
        "social.desk-plants",
        "social",
        "Desk plants are watered every Friday by the office volunteers.",
    ),
    _mem(
        "mem-books-001",
        "social.book-club",
        "social",
        "The book club votes on next month's novel during the first meeting.",
    ),
    _mem(
        "mem-party-001",
        "social.holiday-party",
        "social",
        "The holiday party venue is confirmed for the second Friday of December.",
    ),
    _mem(
        "mem-coffee-001",
        "social.coffee-machine",
        "social",
        "The third-floor coffee machine is descaled on the first Monday monthly.",
    ),
    _mem(
        "mem-parking-001",
        "social.parking-lot",
        "social",
        "Parking lot B resurfacing finished two weeks ahead of schedule.",
    ),
    _mem(
        "mem-bike-001",
        "social.bike-repairs",
        "social",
        "The bicycle repair corner keeps spare tubes in the blue drawer.",
    ),
    _mem(
        "mem-charity-001",
        "social.charity-run",
        "social",
        "The charity run raised funds for the local library this spring.",
    ),
    _mem(
        "mem-language-001",
        "social.language-lunch",
        "social",
        "The Spanish language lunch table meets on Wednesdays in the atrium.",
    ),
    _mem(
        "mem-guitar-001",
        "social.guitar-lessons",
        "social",
        "Lunchtime guitar lessons moved to the small rehearsal room.",
    ),
    _mem(
        "mem-garden-001",
        "social.gardening-tips",
        "social",
        "The rooftop garden beds use a drip irrigation line all summer.",
    ),
    _mem(
        "mem-movie-001",
        "social.movie-night",
        "social",
        "Movie night features a staff-voted film on the last Friday monthly.",
    ),
    _mem(
        "mem-board-001",
        "social.board-games",
        "social",
        "The board games shelf gained three new strategy titles this quarter.",
    ),
    _mem(
        "mem-hike-001",
        "social.hiking-trip",
        "social",
        "The autumn hiking trip carpool leaves from the north car park.",
    ),
    _mem(
        "mem-chess-001",
        "social.chess-club",
        "social",
        "The chess club ladder resets every January with open challenges.",
    ),
    _mem(
        "mem-photowalk-001",
        "social.photography-walk",
        "social",
        "The photography walk circles the old harbor at golden hour.",
    ),
    _mem(
        "mem-knitting-001",
        "social.knitting-circle",
        "social",
        "The knitting circle donates winter hats to the animal shelter.",
    ),
    _mem(
        "mem-stars-001",
        "social.stargazing",
        "social",
        "The stargazing evening uses the rooftop terrace and two telescopes.",
    ),
    _mem(
        "mem-origami-001",
        "social.origami-workshop",
        "social",
        "The origami workshop teaches paper cranes at the spring fair.",
    ),
    _mem(
        "mem-tea-001",
        "social.tea-shelf",
        "social",
        "The tea shelf stocks chamomile, mint, and a strong breakfast blend.",
    ),
]

QUERIES = [
    # ── exact_lexical ────────────────────────────────────────────────────
    _q(
        "q-exact-001",
        "exact_lexical",
        "What approval does an Acme invoice above EUR 5,000 need?",
        {"mem-invoice-001": 3},
        "Shares approval/invoice/EUR terms with the target.",
    ),
    _q(
        "q-exact-002",
        "exact_lexical",
        "How often is password rotation for production servers?",
        {"mem-password-001": 3},
        "Direct term overlap.",
    ),
    _q(
        "q-exact-003",
        "exact_lexical",
        "When do database backups run?",
        {"mem-backup-001": 3},
        "Direct term overlap.",
    ),
    _q(
        "q-exact-004",
        "exact_lexical",
        "Do expense reports need receipts?",
        {"mem-expense-001": 3},
        "Direct term overlap.",
    ),
    _q(
        "q-exact-005",
        "exact_lexical",
        "Which days can we deploy to production?",
        {"mem-deploy-001": 3},
        "Direct term overlap.",
    ),
    _q(
        "q-exact-006",
        "exact_lexical",
        "What is the public API rate limit per key?",
        {"mem-ratelimit-001": 3},
        "Direct term overlap.",
    ),
    # ── semantic_paraphrase ──────────────────────────────────────────────
    _q(
        "q-para-001",
        "semantic_paraphrase",
        "When do building cards stop working after someone exits?",
        {"mem-badge-001": 3},
        "Paraphrase: badges/access vs building cards; leaves vs exits.",
    ),
    _q(
        "q-para-002",
        "semantic_paraphrase",
        "What time is the daily short meeting?",
        {"mem-standup-001": 3},
        "Paraphrase: sync vs short meeting.",
    ),
    _q(
        "q-para-003",
        "semantic_paraphrase",
        "How do staff working from home reach internal systems?",
        {"mem-vpn-001": 3},
        "Paraphrase: remote engineers/VPN vs working from home.",
    ),
    _q(
        "q-para-004",
        "semantic_paraphrase",
        "Where do I file my trip receipts and how long do I have?",
        {"mem-expensetool-001": 3},
        "Paraphrase: travel costs/finance portal vs trip receipts.",
    ),
    _q(
        "q-para-005",
        "semantic_paraphrase",
        "Who helps newcomers learn the codebase at the start?",
        {"mem-mentor-001": 3},
        "Paraphrase: senior colleague/first months vs newcomers at the start.",
    ),
    _q(
        "q-para-006",
        "semantic_paraphrase",
        "What happens if the website stops responding for customers?",
        {"mem-outage-001": 3},
        "Paraphrase: main service goes down vs website stops responding.",
    ),
    # ── identifier_or_entity_name ────────────────────────────────────────
    _q(
        "q-ent-001",
        "identifier_or_entity_name",
        "What does Meridian charge per transaction?",
        {"mem-meridian-fin-001": 3, "mem-meridian-log-001": 1},
        "Entity lookup; the same-named freight fact is partially relevant.",
    ),
    _q(
        "q-ent-002",
        "identifier_or_entity_name",
        "Which Meridian service handles overseas shipments?",
        {"mem-meridian-log-001": 3, "mem-meridian-fin-001": 1},
        "Entity lookup with project framing.",
    ),
    _q(
        "q-ent-003",
        "identifier_or_entity_name",
        "What is Falcon?",
        {"mem-falcon-001": 3},
        "Entity lookup.",
    ),
    _q(
        "q-ent-004",
        "identifier_or_entity_name",
        "What is Quill?",
        {"mem-quill-001": 3},
        "Entity lookup.",
    ),
    _q(
        "q-ent-005",
        "identifier_or_entity_name",
        "What is Aurora?",
        {"mem-aurora-001": 3},
        "Entity lookup.",
    ),
    _q(
        "q-ent-006",
        "identifier_or_entity_name",
        "What is the returns vendor called Orchid responsible for?",
        {"mem-orchid-001": 3},
        "Entity lookup.",
    ),
    # ── temporal_or_revision ─────────────────────────────────────────────
    _q(
        "q-temp-001",
        "temporal_or_revision",
        "What is the current quarterly report deadline?",
        {"mem-deadline-new-001": 3, "mem-deadline-old-001": 1},
        "Newer revision graded higher; both revisions coexist.",
    ),
    _q(
        "q-temp-002",
        "temporal_or_revision",
        "Where is the vendor portal login now?",
        {"mem-portal-new-001": 3, "mem-portal-old-001": 1},
        "Newer revision graded higher.",
    ),
    _q(
        "q-temp-003",
        "temporal_or_revision",
        "What is the standard delivery lead time today?",
        {"mem-leadtime-new-001": 3, "mem-leadtime-old-001": 1},
        "Newer revision graded higher.",
    ),
    _q(
        "q-temp-004",
        "temporal_or_revision",
        "What was the quarterly report deadline before the change?",
        {"mem-deadline-old-001": 2, "mem-deadline-new-001": 1},
        "Historical framing flips the expected order.",
    ),
    _q(
        "q-temp-005",
        "temporal_or_revision",
        "Did the vendor portal address ever change?",
        {"mem-portal-new-001": 2, "mem-portal-old-001": 2},
        "Both revisions equally supporting.",
    ),
    _q(
        "q-temp-006",
        "temporal_or_revision",
        "How long was standard delivery lead time in early 2026?",
        {"mem-leadtime-old-001": 3, "mem-leadtime-new-001": 1},
        "Time-scoped query; the older revision answers.",
    ),
    # ── near_duplicate_diversity ─────────────────────────────────────────
    _q(
        "q-dup-001",
        "near_duplicate_diversity",
        "How often is the employee handbook reviewed?",
        {"mem-handbook-001": 3, "mem-handbook-002": 3, "mem-handbook-003": 3},
        "All three near-duplicates are correct answers; slots may collapse.",
    ),
    _q(
        "q-dup-002",
        "near_duplicate_diversity",
        "Who reviews the staff handbook and how often?",
        {"mem-handbook-001": 3, "mem-handbook-002": 3, "mem-handbook-003": 3},
        "Duplicate group reuse.",
    ),
    _q(
        "q-dup-003",
        "near_duplicate_diversity",
        "How often does the search cache refresh?",
        {"mem-cache-001": 3, "mem-cache-002": 3, "mem-cache-003": 3},
        "Duplicate group reuse.",
    ),
    _q(
        "q-dup-004",
        "near_duplicate_diversity",
        "When is the search cache refreshed during business hours?",
        {"mem-cache-001": 3, "mem-cache-002": 3, "mem-cache-003": 3},
        "Duplicate group reuse.",
    ),
    _q(
        "q-dup-005",
        "near_duplicate_diversity",
        "Do international travel bookings need approval?",
        {"mem-travel-001": 3, "mem-travel-002": 3, "mem-travel-003": 3},
        "Duplicate group reuse.",
    ),
    _q(
        "q-dup-006",
        "near_duplicate_diversity",
        "Who approves international trips before ticketing?",
        {"mem-travel-001": 3, "mem-travel-002": 3, "mem-travel-003": 3},
        "Duplicate group reuse.",
    ),
    # ── negative_or_no_answer ────────────────────────────────────────────
    _q(
        "q-neg-001",
        "negative_or_no_answer",
        "How do I fix a flat tire on my car?",
        {},
        "No answer in corpus; bike-repair distractor is a near miss.",
    ),
    _q(
        "q-neg-002",
        "negative_or_no_answer",
        "What wine pairs well with salmon?",
        {},
        "No answer; food distractors are near misses.",
    ),
    _q(
        "q-neg-003",
        "negative_or_no_answer",
        "Who won the last football world cup?",
        {},
        "No answer; sports distractors do not cover it.",
    ),
    _q(
        "q-neg-004",
        "negative_or_no_answer",
        "What is the freezing point of liquid nitrogen?",
        {},
        "No answer in corpus.",
    ),
    _q(
        "q-neg-005",
        "negative_or_no_answer",
        "How do I care for an orchid houseplant?",
        {},
        "Deliberate collision: the Orchid entity is a returns vendor, not a plant.",
    ),
    _q(
        "q-neg-006",
        "negative_or_no_answer",
        "Which guitar chords should a beginner learn first?",
        {},
        "Deliberate collision: guitar lessons distractor shares the term.",
    ),
    # ── cross_project_isolation ──────────────────────────────────────────
    _q(
        "q-iso-001",
        "cross_project_isolation",
        "In the finance project, what are Northwind's payment terms?",
        {"mem-northwind-fin-001": 3, "mem-northwind-log-001": 1},
        "Same entity name lives in two projects with conflicting facts.",
        namespace="finance",
    ),
    _q(
        "q-iso-002",
        "cross_project_isolation",
        "In the logistics project, what lead time does Northwind freight need?",
        {"mem-northwind-log-001": 3, "mem-northwind-fin-001": 1},
        "Project framing flips the expected winner.",
        namespace="logistics",
    ),
    _q(
        "q-iso-003",
        "cross_project_isolation",
        "For payments, what is Meridian's fee?",
        {"mem-meridian-fin-001": 3, "mem-meridian-log-001": 1},
        "Leakage from the logistics Meridian counts against attribution.",
        namespace="finance",
    ),
    _q(
        "q-iso-004",
        "cross_project_isolation",
        "For overseas shipments, which Meridian do we book?",
        {"mem-meridian-log-001": 3, "mem-meridian-fin-001": 1},
        "Project framing flips the expected winner.",
        namespace="logistics",
    ),
    _q(
        "q-iso-005",
        "cross_project_isolation",
        "Northwind invoice approval limit in the finance project?",
        {"mem-northwind-fin-001": 3, "mem-invoice-001": 2},
        "Acme invoice rule is supporting context, not the answer.",
        namespace="finance",
    ),
    _q(
        "q-iso-006",
        "cross_project_isolation",
        "In the logistics project, which vendor processes damaged parcel returns?",
        {"mem-orchid-001": 3},
        "Namespace-scoped lookup without a name collision.",
        namespace="logistics",
    ),
    # ── snapshot_hydration_equivalence ───────────────────────────────────
    _q(
        "q-snap-001",
        "snapshot_hydration_equivalence",
        "What is the badge deactivation policy after an employee leaves?",
        {"mem-badge-001": 3},
        "Stable fact; evaluated before and after clone + hydrate.",
    ),
    _q(
        "q-snap-002",
        "snapshot_hydration_equivalence",
        "What is the nightly backup retention?",
        {"mem-backup-001": 3},
        "Stable fact.",
    ),
    _q(
        "q-snap-003",
        "snapshot_hydration_equivalence",
        "How do new engineers get a mentor?",
        {"mem-mentor-001": 3},
        "Stable fact.",
    ),
    _q(
        "q-snap-004",
        "snapshot_hydration_equivalence",
        "What happens during a service outage?",
        {"mem-outage-001": 3},
        "Stable fact.",
    ),
    _q(
        "q-snap-005",
        "snapshot_hydration_equivalence",
        "Where do employees submit travel costs?",
        {"mem-expensetool-001": 3},
        "Stable fact.",
    ),
    _q(
        "q-snap-006",
        "snapshot_hydration_equivalence",
        "When is the morning team sync?",
        {"mem-standup-001": 3},
        "Stable fact.",
    ),
]


def main() -> None:
    scale_corpus, scale_queries = _scale_fixtures()
    corpus = CORPUS + scale_corpus
    queries = QUERIES + scale_queries
    assert len(corpus) == 1000, f"expected 1,000 memories, got {len(corpus)}"
    assert len(queries) == 368, f"expected 368 queries, got {len(queries)}"
    for cat in (
        "exact_lexical",
        "semantic_paraphrase",
        "identifier_or_entity_name",
        "temporal_or_revision",
        "near_duplicate_diversity",
        "negative_or_no_answer",
        "cross_project_isolation",
        "snapshot_hydration_equivalence",
    ):
        n = sum(1 for q in queries if q["category"] == cat)
        assert n >= 40, f"{cat} has only {n} queries"
    ids = [m["memory_id"] for m in corpus]
    assert len(ids) == len(set(ids)), "duplicate memory ids"
    known_ids = set(ids)
    for query in queries:
        unknown = set(query["relevance"]) - known_ids
        assert not unknown, f"{query['query_id']} refers to unknown memories: {sorted(unknown)}"
    qids = [q["query_id"] for q in queries]
    assert len(qids) == len(set(qids)), "duplicate query ids"

    for path, rows in ((HERE / "corpus.jsonl", corpus), (HERE / "queries.jsonl", queries)):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        print(f"wrote {len(rows)} records -> {path}")


if __name__ == "__main__":
    main()
