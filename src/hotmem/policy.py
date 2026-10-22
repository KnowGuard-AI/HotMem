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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_ENVELOPE_VERSION = "hotmem-session-v1"


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
        return not (
            self.identifiers and str(row.get("identifier") or "") not in self.identifiers
        )

    def allows_namespace(self, namespace: str, caller_id: str, operation: str = "read") -> bool:
        if self.revoked:
            return False
        if self.audience and caller_id not in self.audience:
            return False
        if operation not in self.operations:
            return False
        return self.namespace == namespace

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "namespace": self.namespace,
            "identifiers": list(self.identifiers),
            "memory_ids": list(self.memory_ids),
            "audience": list(self.audience),
            "operations": list(self.operations),
            "version": self.version,
            "revoked": self.revoked,
        }

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
        )

    def matches_token(self, token: str) -> bool:
        return hmac.compare_digest(self.token_hash, hash_token(token))

    def to_dict(self) -> dict[str, Any]:
        return {
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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallerCredential:
        token_hash = data.get("token_hash")
        if not token_hash and "token" in data:
            token_hash = hash_token(str(data["token"]))
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
            is_management=bool(data.get("is_management", False)),
            revoked=bool(data.get("revoked", False)),
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

    def to_provenance_dict(
        self, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
        if not self.can_access_namespace(target, operation="write"):
            raise PolicyError(
                403,
                "scope_denied",
                f"caller {self.caller_id!r} is not permitted to write to namespace {target!r}",
            )
        return target

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
                not s.revoked and operation in s.operations
                for s in self.credential.share_approvals
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
            s.allows_row(row, self.caller_id, operation)
            for s in self.credential.share_approvals
        )

    def filter_rows(
        self, rows: Iterable[dict[str, Any]], *, operation: str = "read"
    ) -> list[dict[str, Any]]:
        if not self.policy_enforced or self.credential is None:
            if self.namespace is not None:
                return [
                    r for r in rows if str(r.get("namespace") or "") == self.namespace
                ]
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

        # 1. Verify store selection first if caller specified an expected store
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

        enforced = self.require_auth or bool(self.credentials) or (token is not None)
        if not enforced:
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "require_auth": self.require_auth,
            "revision": self.revision,
            "credentials": [c.to_dict() for c in self.credentials],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyConfig:
        return cls(
            store_id=str(data.get("store_id", "")),
            require_auth=bool(data.get("require_auth", False)),
            revision=int(data.get("revision", 1)),
            credentials=[
                CallerCredential.from_dict(c) for c in data.get("credentials", ())
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
