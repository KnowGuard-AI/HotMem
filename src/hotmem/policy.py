"""HotMem daemon policy & session attribution boundary (#109, #110, #111).

Purpose:
    Enforce credential-bound store/namespace/operation scope (#110) and
    explicit session/source attribution (#111) at the shared daemon operation
    boundary before candidate selection, sensitive processing, or mutation.

    - A supplied actor string, localhost binding, or origin header is never
      treated as authentication.
    - Management authority (`is_management=True`) is distinct from ordinary
      memory read/write access; a management-only credential cannot read raw
      private memories unless explicitly granted that namespace.
    - Policy filters candidates prior to ranking/output and redacts private
      payloads from activity streams and diagnostics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hotmem.handoff.redact import DEFAULT_ENGINE as HANDOFF_ENGINE
from hotmem.handoff.redact import RedactionEngine

SESSION_ENVELOPE_VERSION = "hotmem-session-v1"

VALID_SECRET_ACTIONS: frozenset[str] = frozenset({"allow", "redact", "omit", "fail_closed"})
VALID_CLASSIFICATION_ACTIONS: frozenset[str] = frozenset({"allow", "omit", "fail_closed"})
PRIVATE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"private", "secret", "confidential", "restricted"}
)

_EXTRA_DISCLOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "prefixed_credential",
        re.compile(
            r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"sk-[A-Za-z0-9_-]{16,}|hotmem_sk_[A-Za-z0-9]{8,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
        ),
        "credential pattern: vendor-prefixed credential",
    ),
    (
        "dsn_inline_password",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s\"\'\[\]]+)(?=@[^\s\"\']+)"),
        "credential pattern: inline DSN password",
    ),
    (
        "pii_ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "pii pattern: US SSN",
    ),
    (
        "pii_email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "pii pattern: email address",
    ),
)
DISCLOSURE_ENGINE = RedactionEngine(
    patterns=(
        *[
            p
            for p in HANDOFF_ENGINE.patterns
            if p[0] not in {"prefixed_credential", "dsn_inline_password"}
        ],
        *_EXTRA_DISCLOSURE_PATTERNS,
    )
)


def scan_and_redact_text(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Scan ``text`` for secret/PII patterns and replace matches with markers.

    Returns ``(redacted_text, [(pattern_name, count), ...])`` in deterministic
    pattern order without ever recording the raw matched substring.
    """
    if not text:
        return text, []
    redacted, raw_hits = DISCLOSURE_ENGINE.redact_text(text)
    if not raw_hits:
        return text, []
    counts: dict[str, int] = {}
    for h in raw_hits:
        kind = h["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    return redacted, list(counts.items())


def detect_disclosure_patterns(text: str) -> list[str]:
    """Return sorted unique pattern names matched in ``text`` (never raw values)."""
    if not text:
        return []
    return sorted(set(DISCLOSURE_ENGINE.scan(text)))


@dataclass
class DisclosureReport:
    """Machine-readable summary of outbound redactions and policy omissions (#110)."""

    redacted_count: int = 0
    redacted_records: int = 0
    omitted_count: int = 0
    redactions: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)

    def record_redaction(
        self,
        *,
        memory_id: str,
        field_name: str,
        pattern: str,
        count: int,
    ) -> None:
        self.redacted_count += count
        self.redactions.append(
            {
                "memory_id": memory_id,
                "field": field_name,
                "pattern": pattern,
                "count": count,
            }
        )

    def record_omission(
        self,
        *,
        memory_id: str,
        identifier: str,
        reason: str,
        patterns: list[str] | None = None,
    ) -> None:
        self.omitted_count += 1
        entry: dict[str, Any] = {
            "memory_id": memory_id,
            "identifier": identifier,
            "reason": reason,
        }
        if patterns:
            entry["patterns"] = list(patterns)
        self.omissions.append(entry)

    def to_dict(self) -> dict[str, Any]:
        reasons = sorted({str(o["reason"]) for o in self.omissions})
        patterns = sorted({str(r["pattern"]) for r in self.redactions})
        return {
            "redacted_count": self.redacted_count,
            "redacted_records": self.redacted_records,
            "omitted_count": self.omitted_count,
            "reasons": reasons,
            "patterns": patterns,
        }


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest of a bearer/client token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PolicyError(Exception):
    """Structured policy/authentication/scope error raised at the daemon boundary."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.error,
            "message": self.message,
        }
        payload.update(self.detail)
        return payload


def _validate_secret_action(action: str) -> str:
    if action not in VALID_SECRET_ACTIONS:
        raise ValueError(
            f"invalid secret_action {action!r}; expected one of {sorted(VALID_SECRET_ACTIONS)}"
        )
    return action


def _validate_classification_action(action: str) -> str:
    if action not in VALID_CLASSIFICATION_ACTIONS:
        raise ValueError(
            f"invalid private_classification_action {action!r}; "
            f"expected one of {sorted(VALID_CLASSIFICATION_ACTIONS)}"
        )
    return action


def _redact_metadata_obj(value: Any) -> tuple[Any, list[tuple[str, int]]]:
    """Recursively scan and redact string values inside a metadata object."""
    if isinstance(value, str):
        return scan_and_redact_text(value)
    if isinstance(value, list):
        out_list: list[Any] = []
        hits: list[tuple[str, int]] = []
        for item in value:
            red_item, item_hits = _redact_metadata_obj(item)
            out_list.append(red_item)
            hits.extend(item_hits)
        return out_list, hits
    if isinstance(value, dict):
        out_dict: dict[str, Any] = {}
        hits = []
        for k, v in value.items():
            red_v, v_hits = _redact_metadata_obj(v)
            out_dict[k] = red_v
            hits.extend(v_hits)
        return out_dict, hits
    return value, []


def redact_structure(value: Any) -> tuple[Any, list[tuple[str, int]]]:
    """Recursively redact secret/PII patterns in arbitrary JSON-like values."""
    return _redact_metadata_obj(value)


def _is_private_classified(meta: dict[str, Any], tags: list[Any]) -> bool:
    cls_val = str(meta.get("classification") or "").strip().lower()
    vis_val = str(meta.get("visibility") or "").strip().lower()
    if cls_val in PRIVATE_CLASSIFICATIONS or vis_val in PRIVATE_CLASSIFICATIONS:
        return True
    if meta.get("private") is True:
        return True
    for tag in tags:
        t = str(tag).strip().lower()
        if t in PRIVATE_CLASSIFICATIONS or t in {
            f"classification:{c}" for c in PRIVATE_CLASSIFICATIONS
        }:
            return True
    return False


@dataclass(frozen=True)
class ShareApproval:
    """Explicit share approval granting an audience access to selected context (#110)."""

    approval_id: str
    namespace: str
    identifiers: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    audience: tuple[str, ...] = ()
    operations: tuple[str, ...] = ("read",)
    version: int = 1
    revoked: bool = False
    secret_action: str | None = None
    private_classification_action: str | None = None

    def __post_init__(self) -> None:
        if self.secret_action is not None:
            _validate_secret_action(self.secret_action)
        if self.private_classification_action is not None:
            _validate_classification_action(self.private_classification_action)

    def allows_row(self, row: dict[str, Any], caller_id: str, operation: str = "read") -> bool:
        if self.revoked:
            return False
        if self.audience and caller_id not in self.audience:
            return False
        if operation not in self.operations:
            return False
        row_ns = str(row.get("namespace") or "")
        if row_ns != self.namespace:
            return False
        if self.memory_ids and str(row.get("id") or "") not in self.memory_ids:
            return False
        return not (self.identifiers and str(row.get("identifier") or "") not in self.identifiers)

    def allows_namespace(self, namespace: str, caller_id: str, operation: str = "read") -> bool:
        if self.revoked:
            return False
        if self.audience and caller_id not in self.audience:
            return False
        if operation not in self.operations:
            return False
        return self.namespace == namespace

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "approval_id": self.approval_id,
            "namespace": self.namespace,
            "identifiers": list(self.identifiers),
            "memory_ids": list(self.memory_ids),
            "audience": list(self.audience),
            "operations": list(self.operations),
            "version": self.version,
            "revoked": self.revoked,
        }
        if self.secret_action is not None:
            d["secret_action"] = self.secret_action
        if self.private_classification_action is not None:
            d["private_classification_action"] = self.private_classification_action
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShareApproval:
        return cls(
            approval_id=str(data["approval_id"]),
            namespace=str(data.get("namespace", "")),
            identifiers=tuple(data.get("identifiers", ())),
            memory_ids=tuple(data.get("memory_ids", ())),
            audience=tuple(data.get("audience", ())),
            operations=tuple(data.get("operations", ("read",))),
            version=int(data.get("version", 1)),
            revoked=bool(data.get("revoked", False)),
            secret_action=(
                str(data["secret_action"]) if data.get("secret_action") is not None else None
            ),
            private_classification_action=(
                str(data["private_classification_action"])
                if data.get("private_classification_action") is not None
                else None
            ),
        )


@dataclass
class CallerCredential:
    """Credential binding a token to store, namespaces, operations, and session scope."""

    token_hash: str
    caller_id: str
    store_id: str = ""
    namespaces: tuple[str, ...] = ("",)
    operations: tuple[str, ...] = ("read", "write", "export", "hydrate", "promote")
    allowed_sessions: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    share_approvals: tuple[ShareApproval, ...] = ()
    is_management: bool = False
    revoked: bool = False
    secret_action: str | None = None
    private_classification_action: str | None = None
    allow_attachments: bool = False
    redact_host_paths: bool = True

    def __post_init__(self) -> None:
        if self.secret_action is not None:
            _validate_secret_action(self.secret_action)
        if self.private_classification_action is not None:
            _validate_classification_action(self.private_classification_action)

    @classmethod
    def from_token(
        cls,
        *,
        token: str,
        caller_id: str,
        store_id: str = "",
        namespaces: Iterable[str] = ("",),
        operations: Iterable[str] = ("read", "write", "export", "hydrate", "promote"),
        allowed_sessions: Iterable[str] = (),
        allowed_sources: Iterable[str] = (),
        share_approvals: Iterable[ShareApproval] = (),
        is_management: bool = False,
        revoked: bool = False,
        secret_action: str | None = None,
        private_classification_action: str | None = None,
        allow_attachments: bool = False,
        redact_host_paths: bool = True,
    ) -> CallerCredential:
        return cls(
            token_hash=hash_token(token),
            caller_id=caller_id,
            store_id=store_id,
            namespaces=tuple(namespaces),
            operations=tuple(operations),
            allowed_sessions=tuple(allowed_sessions),
            allowed_sources=tuple(allowed_sources),
            share_approvals=tuple(share_approvals),
            is_management=is_management,
            revoked=revoked,
            secret_action=secret_action,
            private_classification_action=private_classification_action,
            allow_attachments=allow_attachments,
            redact_host_paths=redact_host_paths,
        )

    def matches_token(self, token: str) -> bool:
        return hmac.compare_digest(self.token_hash, hash_token(token))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "token_hash": self.token_hash,
            "caller_id": self.caller_id,
            "store_id": self.store_id,
            "namespaces": list(self.namespaces),
            "operations": list(self.operations),
            "allowed_sessions": list(self.allowed_sessions),
            "allowed_sources": list(self.allowed_sources),
            "share_approvals": [s.to_dict() for s in self.share_approvals],
            "is_management": self.is_management,
            "revoked": self.revoked,
            "allow_attachments": self.allow_attachments,
            "redact_host_paths": self.redact_host_paths,
        }
        if self.secret_action is not None:
            d["secret_action"] = self.secret_action
        if self.private_classification_action is not None:
            d["private_classification_action"] = self.private_classification_action
        return d

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        default_secret_action: str | None = None,
        default_private_classification_action: str | None = None,
    ) -> CallerCredential:
        token_hash = data.get("token_hash")
        if not token_hash and "token" in data:
            token_hash = hash_token(str(data["token"]))
        is_mgmt = bool(data.get("is_management", False))
        raw_secret = data.get("secret_action", default_secret_action)
        raw_cls = data.get("private_classification_action", default_private_classification_action)
        return cls(
            token_hash=str(token_hash or ""),
            caller_id=str(data["caller_id"]),
            store_id=str(data.get("store_id", "")),
            namespaces=tuple(data.get("namespaces", ("",))),
            operations=tuple(
                data.get("operations", ("read", "write", "export", "hydrate", "promote"))
            ),
            allowed_sessions=tuple(data.get("allowed_sessions", ())),
            allowed_sources=tuple(data.get("allowed_sources", ())),
            share_approvals=tuple(
                ShareApproval.from_dict(s) for s in data.get("share_approvals", ())
            ),
            is_management=is_mgmt,
            revoked=bool(data.get("revoked", False)),
            secret_action=str(raw_secret) if raw_secret is not None else None,
            private_classification_action=str(raw_cls) if raw_cls is not None else None,
            allow_attachments=bool(data.get("allow_attachments", False)),
            redact_host_paths=bool(data.get("redact_host_paths", True)),
        )


@dataclass(frozen=True)
class SessionEnvelope:
    """Explicit session context envelope for memory writes and handoffs (#111)."""

    caller_id: str
    session_id: str
    source: str
    namespace: str
    store_id: str
    envelope_version: str = SESSION_ENVELOPE_VERSION

    def to_provenance_dict(self, existing: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(
            {
                "envelope_version": self.envelope_version,
                "caller_id": self.caller_id,
                "session_id": self.session_id,
                "source": self.source,
                "namespace": self.namespace,
                "store_id": self.store_id,
            }
        )
        return merged


@dataclass
class CallerContext:
    """Resolved per-request caller identity, scope, and session context."""

    authenticated: bool
    policy_enforced: bool
    caller_id: str
    store_id: str
    session_id: str = ""
    source: str = ""
    namespace: str | None = None
    credential: CallerCredential | None = None
    idempotency_key: str | None = None
    policy_default_secret_action: str = "redact"
    policy_default_private_classification_action: str = "omit"

    @property
    def effective_write_namespace(self) -> str:
        """Return the namespace to stamp on a write, validating caller permission."""
        if not self.policy_enforced or self.credential is None:
            return self.namespace or ""
        target = (
            self.namespace
            if self.namespace is not None
            else (self.credential.namespaces[0] if self.credential.namespaces else "")
        )
        if target == "*":
            raise PolicyError(
                400,
                "namespace_required",
                "a concrete namespace is required for writes; "
                "'*' is a read wildcard and cannot be used as a write target",
            )
        if not self.can_access_namespace(target, operation="write"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {self.caller_id!r} is not permitted to write to namespace {target!r}",
            )
        return target

    def hydrate_target_namespace(self) -> str:
        """Namespace to bind unscoped imported records to ("" = leave as-is)."""
        if not self.policy_enforced or self.credential is None:
            return self.namespace or ""
        if self.namespace is not None:
            return self.effective_write_namespace
        cred = self.credential
        if cred.namespaces and "*" not in cred.namespaces:
            return self.effective_write_namespace
        return ""

    def require_operation(self, operation: str) -> None:
        if not self.policy_enforced or self.credential is None:
            return
        if operation == "manage":
            if not self.credential.is_management:
                raise PolicyError(
                    403,
                    "management_authority_required",
                    f"caller {self.caller_id!r} lacks management authority",
                )
            return
        if operation not in self.credential.operations:
            # Also check if any share approval grants this operation
            has_share_op = any(
                not s.revoked and operation in s.operations for s in self.credential.share_approvals
            )
            if not has_share_op:
                raise PolicyError(
                    403,
                    "scope_denied",
                    f"caller {self.caller_id!r} is not permitted to perform {operation!r}",
                )
        if self.namespace is not None and not self.can_access_namespace(
            self.namespace, operation=operation
        ):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {self.caller_id!r} cannot {operation} in namespace {self.namespace!r}",
            )

    def can_access_namespace(self, namespace: str, *, operation: str = "read") -> bool:
        if not self.policy_enforced or self.credential is None:
            return True
        if self.namespace is not None and namespace != self.namespace:
            return False
        if operation in self.credential.operations and (
            "*" in self.credential.namespaces or namespace in self.credential.namespaces
        ):
            return True
        return any(
            s.allows_namespace(namespace, self.caller_id, operation)
            for s in self.credential.share_approvals
        )

    def can_access_row(self, row: dict[str, Any], *, operation: str = "read") -> bool:
        if not self.policy_enforced or self.credential is None:
            return True
        row_ns = str(row.get("namespace") or "")
        if self.namespace is not None and row_ns != self.namespace:
            return False
        if operation in self.credential.operations and (
            "*" in self.credential.namespaces or row_ns in self.credential.namespaces
        ):
            return True
        return any(
            s.allows_row(row, self.caller_id, operation) for s in self.credential.share_approvals
        )

    @property
    def redact_host_paths_enabled(self) -> bool:
        if not self.policy_enforced:
            return False
        if self.credential is None:
            return True
        return self.credential.redact_host_paths

    @property
    def allow_attachments_enabled(self) -> bool:
        if not self.policy_enforced:
            return True
        if self.credential is None:
            return False
        return bool(self.credential.allow_attachments or self.credential.is_management)

    def sanitize_source_uri(
        self, source_uri: str | None, *, base_dir: str | Path | None = None
    ) -> str:
        """Sanitize ``source_uri`` so raw host paths are never disclosed under policy."""
        raw = str(source_uri or "")
        if not raw or not self.redact_host_paths_enabled:
            return raw
        candidate = raw
        scheme = ""
        if "://" in raw:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(raw)
            scheme = (parsed.scheme or "").lower()
            if scheme and scheme != "file":
                # Non-local URIs are not host filesystem paths: preserve them
                # verbatim rather than collapsing ``://`` through Path().
                return raw
            candidate = unquote(parsed.path or "")
        p = Path(candidate)
        if base_dir is not None:
            try:
                base_resolved = Path(base_dir).resolve()
                resolved = (base_resolved / p).resolve() if not p.is_absolute() else p.resolve()
                if resolved == base_resolved or base_resolved in resolved.parents:
                    rel = resolved.relative_to(base_resolved).as_posix()
                    return "" if rel == "." else rel
            except Exception:
                return ""
            return ""
        if p.is_absolute() or raw.startswith("file://") or ".." in p.parts:
            return ""
        return raw

    def effective_disclosure_actions(
        self, row: dict[str, Any], *, operation: str = "read"
    ) -> tuple[str, str]:
        """Return ``(secret_action, private_classification_action)`` for ``row``."""
        if not self.policy_enforced or self.credential is None:
            return ("allow", "allow")
        row_ns = str(row.get("namespace") or "")
        is_direct = operation in self.credential.operations and (
            "*" in self.credential.namespaces or row_ns in self.credential.namespaces
        )
        matched_share: ShareApproval | None = None
        for share in self.credential.share_approvals:
            if share.allows_row(row, self.caller_id, operation):
                matched_share = share
                break

        if matched_share is not None and not is_direct:
            secret_action = (
                matched_share.secret_action
                or self.credential.secret_action
                or self.policy_default_secret_action
            )
            cls_action = (
                matched_share.private_classification_action
                or self.credential.private_classification_action
                or self.policy_default_private_classification_action
            )
            return (secret_action, cls_action)

        # Direct namespace access (with optional share override if present)
        secret_action = (
            (matched_share.secret_action if matched_share else None)
            or self.credential.secret_action
            or "allow"
        )
        cls_action = (
            (matched_share.private_classification_action if matched_share else None)
            or self.credential.private_classification_action
            or "allow"
        )
        return (secret_action, cls_action)

    def apply_disclosure_to_row(
        self,
        row: dict[str, Any],
        *,
        report: DisclosureReport | None = None,
        operation: str = "read",
    ) -> dict[str, Any] | None:
        """Apply classification and secret/PII disclosure policy to one row (#110).

        Returns a (possibly redacted) copy of ``row``, or ``None`` if omitted.
        Raises :class:`PolicyError` (403 ``policy_disclosure_blocked``) when
        configured to fail closed. Never echoes raw secret values in errors or
        reports.
        """
        if not self.policy_enforced or self.credential is None:
            return dict(row)

        secret_action, cls_action = self.effective_disclosure_actions(row, operation=operation)
        row_id = str(row.get("id") or row.get("memory_id") or "")
        identifier = str(row.get("identifier") or "")

        meta_obj: dict[str, Any] = {}
        if isinstance(row.get("metadata"), dict):
            meta_obj = dict(row["metadata"])
        elif isinstance(row.get("metadata_json"), str) and row.get("metadata_json"):
            try:
                parsed = json.loads(row["metadata_json"])
                if isinstance(parsed, dict):
                    meta_obj = parsed
            except (json.JSONDecodeError, TypeError):
                meta_obj = {}

        tags_list: list[Any] = []
        raw_tags = row.get("tags")
        if isinstance(raw_tags, list):
            tags_list = raw_tags
        elif isinstance(raw_tags, str) and raw_tags:
            try:
                parsed_tags = json.loads(raw_tags)
                if isinstance(parsed_tags, list):
                    tags_list = parsed_tags
            except (json.JSONDecodeError, TypeError):
                tags_list = []

        if _is_private_classified(meta_obj, tags_list):
            if cls_action == "fail_closed":
                raise PolicyError(
                    403,
                    "policy_disclosure_blocked",
                    f"policy blocked disclosure of private-classified memory {row_id!r}",
                    detail={
                        "reason": "private_classification",
                        "memory_id": row_id,
                    },
                )
            if cls_action == "omit":
                if report is not None:
                    report.record_omission(
                        memory_id=row_id,
                        identifier=identifier,
                        reason="policy_classification",
                    )
                return None

        if secret_action == "allow":
            return dict(row)

        out = dict(row)
        field_hits: list[tuple[str, str, int]] = []

        for text_field in (
            "fact_text",
            "fact_summary",
            "content",
            "identifier",
            "source_uri",
        ):
            val = out.get(text_field)
            if isinstance(val, str) and val:
                redacted_val, hits = scan_and_redact_text(val)
                if hits:
                    out[text_field] = redacted_val
                    for pat_name, cnt in hits:
                        field_hits.append((text_field, pat_name, cnt))

        if meta_obj:
            redacted_meta, meta_hits = _redact_metadata_obj(meta_obj)
            if meta_hits:
                for pat_name, cnt in meta_hits:
                    field_hits.append(("metadata", pat_name, cnt))
                if "metadata" in out and isinstance(out["metadata"], dict):
                    out["metadata"] = redacted_meta
                if "metadata_json" in out:
                    out["metadata_json"] = json.dumps(redacted_meta)

        prov_obj: Any = out.get("provenance")
        if prov_obj is None and isinstance(out.get("provenance_json"), str):
            try:
                parsed_prov = json.loads(out["provenance_json"])
            except (json.JSONDecodeError, TypeError):
                parsed_prov = None
            if isinstance(parsed_prov, (dict, list)):
                prov_obj = parsed_prov
        if prov_obj is not None:
            redacted_prov, prov_hits = _redact_metadata_obj(prov_obj)
            if prov_hits:
                for pat_name, cnt in prov_hits:
                    field_hits.append(("provenance", pat_name, cnt))
                if "provenance" in out:
                    out["provenance"] = redacted_prov
                if "provenance_json" in out:
                    out["provenance_json"] = json.dumps(redacted_prov)

        tags_obj: Any = out.get("tags")
        if isinstance(tags_obj, str) and tags_obj:
            try:
                tags_obj = json.loads(tags_obj)
            except (json.JSONDecodeError, TypeError):
                tags_obj = None
        if isinstance(tags_obj, (list, dict)):
            redacted_tags, tag_hits = _redact_metadata_obj(tags_obj)
            if tag_hits:
                for pat_name, cnt in tag_hits:
                    field_hits.append(("tags", pat_name, cnt))
                out["tags"] = (
                    json.dumps(redacted_tags) if isinstance(out.get("tags"), str) else redacted_tags
                )

        if not field_hits:
            return out

        matched_patterns = sorted({pat for _, pat, _ in field_hits})
        if secret_action == "fail_closed":
            raise PolicyError(
                403,
                "policy_disclosure_blocked",
                f"policy blocked disclosure of secret/PII pattern(s) in memory {row_id!r}",
                detail={
                    "reason": "secret_detected",
                    "patterns": matched_patterns,
                    "memory_id": row_id,
                },
            )
        if secret_action == "omit":
            if report is not None:
                report.record_omission(
                    memory_id=row_id,
                    identifier=identifier,
                    reason="policy_secret_omitted",
                    patterns=matched_patterns,
                )
            return None

        # secret_action == "redact"
        if report is not None:
            report.redacted_records += 1
            for field_name, pat_name, cnt in field_hits:
                report.record_redaction(
                    memory_id=row_id,
                    field_name=field_name,
                    pattern=pat_name,
                    count=cnt,
                )
        return out

    def filter_rows(
        self, rows: Iterable[dict[str, Any]], *, operation: str = "read"
    ) -> list[dict[str, Any]]:
        if not self.policy_enforced or self.credential is None:
            if self.namespace is not None:
                return [r for r in rows if str(r.get("namespace") or "") == self.namespace]
            return list(rows)
        return [r for r in rows if self.can_access_row(r, operation=operation)]

    def build_session_envelope(
        self, *, namespace: str, request_source: str = ""
    ) -> SessionEnvelope | None:
        effective_source = self.source or request_source
        if not self.policy_enforced and not self.session_id:
            return None
        return SessionEnvelope(
            caller_id=self.caller_id or "anonymous",
            session_id=self.session_id,
            source=effective_source,
            namespace=namespace,
            store_id=self.store_id,
        )


@dataclass
class PolicyConfig:
    """Runtime policy configuration for store identity, credentials, and scopes."""

    store_id: str = ""
    require_auth: bool = False
    credentials: list[CallerCredential] = field(default_factory=list)
    revision: int = 1
    default_secret_action: str = "redact"
    default_private_classification_action: str = "omit"

    def __post_init__(self) -> None:
        _validate_secret_action(self.default_secret_action)
        _validate_classification_action(self.default_private_classification_action)

    def revoke_caller(self, caller_id: str) -> int:
        """Revoke all credentials and share approvals for ``caller_id``."""
        count = 0
        for idx, cred in enumerate(self.credentials):
            if cred.caller_id == caller_id and not cred.revoked:
                cred.revoked = True
                count += 1
            updated_shares = []
            changed = False
            for s in cred.share_approvals:
                if caller_id in s.audience and not s.revoked:
                    updated_shares.append(
                        ShareApproval(
                            approval_id=s.approval_id,
                            namespace=s.namespace,
                            identifiers=s.identifiers,
                            memory_ids=s.memory_ids,
                            audience=s.audience,
                            operations=s.operations,
                            version=s.version,
                            revoked=True,
                            secret_action=s.secret_action,
                            private_classification_action=s.private_classification_action,
                        )
                    )
                    changed = True
                    count += 1
                else:
                    updated_shares.append(s)
            if changed:
                self.credentials[idx].share_approvals = tuple(updated_shares)
        self.revision += 1
        return count

    def revoke_share_approval(self, approval_id: str) -> int:
        """Revoke a specific ShareApproval by ID across all credentials."""
        count = 0
        for cred in self.credentials:
            updated = []
            for s in cred.share_approvals:
                if s.approval_id == approval_id and not s.revoked:
                    updated.append(
                        ShareApproval(
                            approval_id=s.approval_id,
                            namespace=s.namespace,
                            identifiers=s.identifiers,
                            memory_ids=s.memory_ids,
                            audience=s.audience,
                            operations=s.operations,
                            version=s.version,
                            revoked=True,
                            secret_action=s.secret_action,
                            private_classification_action=s.private_classification_action,
                        )
                    )
                    count += 1
                else:
                    updated.append(s)
            cred.share_approvals = tuple(updated)
        self.revision += 1
        return count

    def _claimed_sessions_of_others(self, caller_id: str) -> set[str]:
        out: set[str] = set()
        for c in self.credentials:
            if c.caller_id != caller_id:
                out.update(c.allowed_sessions)
        return out

    def _claimed_sources_of_others(self, caller_id: str) -> set[str]:
        out: set[str] = set()
        for c in self.credentials:
            if c.caller_id != caller_id:
                out.update(c.allowed_sources)
        return out

    def validate_write_source(self, ctx: CallerContext, source: str | None) -> None:
        """Reject source attribution the credential is not entitled to claim (#111)."""
        if not source or not ctx.policy_enforced or ctx.credential is None:
            return
        if ctx.source and source != ctx.source:
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"request payload source {source!r} conflicts with "
                f"session header source {ctx.source!r}",
            )
        cred = ctx.credential
        if cred.allowed_sources and source not in cred.allowed_sources:
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"source {source!r} is not permitted for caller {ctx.caller_id!r}",
            )
        if source in self._claimed_sources_of_others(ctx.caller_id):
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"source {source!r} belongs to another caller",
            )

    def resolve_request(
        self,
        *,
        runtime_store_id: str,
        token: str | None = None,
        requested_store_id: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        namespace: str | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> CallerContext:
        """Authenticate and authorize request metadata, returning a CallerContext."""
        active_store_id = self.store_id or runtime_store_id
        namespace = namespace or None

        enforced = self.require_auth or bool(self.credentials) or (token is not None)
        if not enforced:
            # No credential boundary (embedded/local operator): the store
            # selection is still checked, and there is nothing to authenticate.
            if requested_store_id and active_store_id and requested_store_id != active_store_id:
                msg = (
                    f"requested store {requested_store_id!r} does not match "
                    f"active store {active_store_id!r}"
                )
                raise PolicyError(
                    409,
                    "store_mismatch",
                    msg,
                    detail={
                        "expected_store_id": requested_store_id,
                        "actual_store_id": active_store_id,
                    },
                )
            return CallerContext(
                authenticated=False,
                policy_enforced=False,
                caller_id=actor or "embedded",
                store_id=active_store_id,
                session_id=session_id or "",
                source=source or "",
                namespace=namespace,
                credential=None,
                idempotency_key=idempotency_key,
            )

        if not token:
            raise PolicyError(
                401,
                "missing_credential",
                "daemon policy requires an authenticated bearer or X-HotMem-Token credential",
            )

        matched: CallerCredential | None = None
        for cred in self.credentials:
            if cred.matches_token(token):
                matched = cred
                break

        if matched is None:
            raise PolicyError(
                401,
                "invalid_credential",
                "provided credential is not recognized by this daemon",
            )

        if matched.revoked:
            raise PolicyError(
                403,
                "revoked_credential",
                f"credential for caller {matched.caller_id!r} has been revoked",
            )

        # Store identity is disclosed only after the caller is authenticated
        # (an unauthenticated store-id oracle is itself a leak).
        if requested_store_id and active_store_id and requested_store_id != active_store_id:
            msg = (
                f"requested store {requested_store_id!r} does not match "
                f"active store {active_store_id!r}"
            )
            raise PolicyError(
                409,
                "store_mismatch",
                msg,
                detail={
                    "expected_store_id": requested_store_id,
                    "actual_store_id": active_store_id,
                },
            )

        if matched.store_id and active_store_id and matched.store_id != active_store_id:
            msg = (
                f"credential bound to store {matched.store_id!r}, "
                f"active store is {active_store_id!r}"
            )
            raise PolicyError(
                409,
                "store_mismatch",
                msg,
            )

        # Actor / session / source impersonation checks (#110 / #111)
        if actor and actor != matched.caller_id:
            raise PolicyError(
                403,
                "session_impersonation_denied",
                f"caller {matched.caller_id!r} cannot impersonate actor {actor!r}",
            )

        if session_id:
            if matched.allowed_sessions and session_id not in matched.allowed_sessions:
                raise PolicyError(
                    403,
                    "session_impersonation_denied",
                    f"session {session_id!r} is not permitted for caller {matched.caller_id!r}",
                )
            if session_id in self._claimed_sessions_of_others(matched.caller_id):
                raise PolicyError(
                    403,
                    "session_impersonation_denied",
                    f"session {session_id!r} belongs to another caller",
                )

        if source:
            if matched.allowed_sources and source not in matched.allowed_sources:
                raise PolicyError(
                    403,
                    "session_impersonation_denied",
                    f"source {source!r} is not permitted for caller {matched.caller_id!r}",
                )
            if source in self._claimed_sources_of_others(matched.caller_id):
                raise PolicyError(
                    403,
                    "session_impersonation_denied",
                    f"source {source!r} belongs to another caller",
                )

        return CallerContext(
            authenticated=True,
            policy_enforced=True,
            caller_id=matched.caller_id,
            store_id=active_store_id,
            session_id=session_id or "",
            source=source or "",
            namespace=namespace,
            credential=matched,
            idempotency_key=idempotency_key,
            policy_default_secret_action=self.default_secret_action,
            policy_default_private_classification_action=(
                self.default_private_classification_action
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "require_auth": self.require_auth,
            "revision": self.revision,
            "default_secret_action": self.default_secret_action,
            "default_private_classification_action": self.default_private_classification_action,
            "credentials": [c.to_dict() for c in self.credentials],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyConfig:
        raw_secret = data.get("default_secret_action")
        raw_cls = data.get("default_private_classification_action")
        default_secret = str(raw_secret) if raw_secret is not None else "redact"
        default_cls = str(raw_cls) if raw_cls is not None else "omit"
        return cls(
            store_id=str(data.get("store_id", "")),
            require_auth=bool(data.get("require_auth", False)),
            revision=int(data.get("revision", 1)),
            default_secret_action=default_secret,
            default_private_classification_action=default_cls,
            credentials=[
                # Top-level defaults are applied to shared access at request
                # time; direct self-owned access keeps its documented "allow"
                # fallback unless the credential sets an action explicitly.
                CallerCredential.from_dict(c)
                for c in data.get("credentials", ())
            ],
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @classmethod
    def load(cls, path: str | Path) -> PolicyConfig:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)
