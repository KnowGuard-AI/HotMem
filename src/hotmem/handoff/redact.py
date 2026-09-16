"""Deny-by-default redaction for handoff content (#101).

Purpose:
    Layer 2 and layer 3 of the handoff-v1 §7 redaction policy: secret
    patterns over all transferred text (credentials, tokens, private keys)
    replaced with ``[REDACTED:<kind>]`` placeholders, plus an output gate
    that re-scans rendered briefs and error strings so redacted values can
    never leak through logs or diagnostics (acceptance criterion 5).

    Records carry the secret KIND and location only — never the value.

Interface:
    RedactionEngine.patterns — the compiled deny-by-default pattern set
    engine.redact_text(text) -> (redacted_text, hits)   # hits: [{kind, reason}]
    engine.scan(text) -> [kind, ...]                    # detect without replacing
    assert_no_secrets(text) — the output gate (raises RedactionLeakError)
    redact_normalized(session) -> NormalizedSession     # entries + memories

Deps: stdlib + hotmem.handoff (NormalizedSession) + hotmem.handoff.report.
Extension: new credential shapes append (kind, pattern, reason) tuples to
    the pattern set; kinds are machine-stable contract values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from hotmem.handoff.codex_source import NormalizedSession
from hotmem.handoff.report import redaction
from hotmem.trace import get_tracer

_trace = get_tracer("handoff.redact")

PLACEHOLDER = "[REDACTED:{kind}]"

# Kind, pattern, reason. Order matters: assignment patterns run before
# prefixed-credential patterns so the surrounding context survives.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "api_key",
        # <name>=value / <name>: value for api keys, secrets, tokens,
        # passwords — including env-var style names (HOTMEM_API_KEY,
        # GITHUB_TOKEN) where the keyword is a suffix of a longer
        # identifier, and JSON/config forms where the name is quoted
        # ("api_key": "value").
        #
        # Performance: the leading ``(?<![a-z0-9_.-])`` anchors every
        # attempt at an identifier-run boundary. Without it the engine
        # retries the whole prefix/keyword combination at every character,
        # which cost ~113 s for a single 64 KiB entry (the configured
        # max_entry_bytes) and made prepare/verify hang on legitimate
        # single-token content (base64 blobs, minified JSON, long tokens).
        # Recall is unaffected: any maximal identifier run that contains a
        # keyword also *starts* at a boundary, so the run start is always a
        # candidate position. The bounded ``{0,64}`` prefix follows
        # env-var identifiers, which are far shorter than 64 chars.
        #
        # Brackets are excluded from the value charset so the engine's own
        # [REDACTED:<kind>] placeholder can never re-match (gate stability).
        re.compile(
            r"(?i)(?<![a-z0-9_.-])([a-z0-9_.-]{0,64}"
            r"(?:api[_-]?key|apikey|secret|token|password|passwd|pwd)[a-z0-9_.-]{0,64})"
            r"[\"']?\s*[=:]\s*[\"']?([^\s\"'\[\]]{4,})[\"']?"
        ),
        "credential pattern: key/password assignment",
    ),
    (
        "bearer_token",
        # The scheme keyword stays visible; the credential itself is group 2.
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/=]{8,})"),
        "credential pattern: bearer token",
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.)*?-----END [A-Z ]*PRIVATE KEY-----"),
        "credential pattern: PEM private key block",
    ),
    (
        "prefixed_credential",
        re.compile(
            r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"sk-[A-Za-z0-9]{16,}|hotmem_sk_[A-Za-z0-9]{8,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
        ),
        "credential pattern: vendor-prefixed credential",
    ),
)


def _make_replacer(kind: str):
    """Build the per-pattern replacement function (binds kind by closure)."""

    def _replace(match: re.Match[str]) -> str:
        groups = match.groups()
        # Patterns that capture a visible prefix keep it: the value
        # (last group) is what gets replaced.
        if groups and groups[-1] is not None and len(groups) >= 2:
            return match.group(0).replace(groups[-1], PLACEHOLDER.format(kind=kind))
        return PLACEHOLDER.format(kind=kind)

    return _replace


@dataclass(frozen=True)
class RedactionEngine:
    """The deny-by-default pattern set (handoff-v1 §7 layer 2)."""

    patterns: tuple[tuple[str, re.Pattern[str], str], ...] = _PATTERNS

    def redact_text(self, text: str) -> tuple[str, list[dict[str, str]]]:
        """Replace every secret in ``text``; return hits with kind + reason.

        Hits never carry the matched value — only the kind and the static
        reason, so records and logs stay leak-free by construction.
        """
        hits: list[dict[str, str]] = []
        for kind, pattern, reason in self.patterns:
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            hits.extend({"kind": kind, "reason": reason} for _ in matches)
            text = pattern.sub(_make_replacer(kind), text)
        return text, hits

    def scan(self, text: str) -> list[str]:
        """Detect secret kinds without replacing (the output gate's probe)."""
        kinds: list[str] = []
        for kind, pattern, _reason in self.patterns:
            if pattern.search(text):
                kinds.append(kind)
        return kinds


DEFAULT_ENGINE = RedactionEngine()


class RedactionLeakError(Exception):
    """Raised by the output gate when a secret kind appears in output.

    The message names the KIND and location context only — never the value.
    """

    def __init__(self, kinds: list[str], where: str) -> None:
        self.kinds = kinds
        self.where = where
        super().__init__(
            f"redaction gate failed: secret kind(s) {', '.join(sorted(set(kinds)))} "
            f"still present in {where}"
        )


def assert_no_secrets(text: str, *, where: str = "output") -> None:
    """The layer-3 output gate (handoff-v1 §7): fail closed on any secret kind.

    Raise RedactionLeakError naming the kind and location; the message never
    includes the matched value.
    """
    kinds = DEFAULT_ENGINE.scan(text)
    if kinds:
        raise RedactionLeakError(kinds, where)


def redact_normalized(
    session: NormalizedSession, engine: RedactionEngine | None = None
) -> NormalizedSession:
    """Apply layer-2 redaction to a normalized session's entries and memories.

    Returns a NEW NormalizedSession (frozen dataclass, replace): redacted
    text, ``redacted: true`` markers on touched entries, and redaction
    records in ``session.redactions`` with location and kind only.
    """
    engine = engine or DEFAULT_ENGINE
    records: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []

    for entry in session.entries:
        entry = dict(entry)
        for field_name in ("text", "summary"):
            value = entry.get(field_name)
            if not isinstance(value, str) or not value:
                continue
            redacted, hits = engine.redact_text(value)
            if hits:
                entry[field_name] = redacted
                entry["redacted"] = True
                for hit in hits:
                    records.append(
                        redaction(
                            entry["source"]["source_entry_id"],
                            field_name,
                            hit["kind"],
                            hit["reason"],
                            recoverable=True,
                        )
                    )
        entries.append(entry)

    memory_records: list[dict[str, Any]] = []
    for record in session.memory_records:
        record = dict(record)
        fact = str(record.get("fact_text") or "")
        redacted, hits = engine.redact_text(fact)
        if hits:
            record["fact_text"] = redacted
            for hit in hits:
                records.append(
                    redaction(
                        str(record.get("identifier")),
                        "fact_text",
                        hit["kind"],
                        hit["reason"],
                        recoverable=True,
                    )
                )
        memory_records.append(record)

    result = replace(
        session,
        entries=entries,
        memory_records=memory_records,
        redactions=records,
    )
    if records:
        _trace.info(
            "redact",
            f"applied {len(records)} redaction record(s) across "
            f"{len(entries)} entries and {len(memory_records)} memories",
            detail={"session_id": session.session_id},
        )
    return result
