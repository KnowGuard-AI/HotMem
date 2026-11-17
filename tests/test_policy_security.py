"""Focused security properties for persisted daemon credentials."""

from __future__ import annotations

import json

from hotmem.policy import CallerCredential, PolicyConfig, hash_token


def test_policy_serializes_token_digest_and_reads_legacy_plain_token():
    secret = "test-only-high-entropy-hotmem-token"
    credential = CallerCredential.from_token(token=secret, caller_id="test-client")
    policy = PolicyConfig(require_auth=True, credentials=[credential])

    serialized = json.dumps(policy.to_dict())
    assert secret not in serialized
    assert hash_token(secret) in serialized
    assert credential.matches_token(secret)
    assert not credential.matches_token("wrong-token")

    restored = PolicyConfig.from_dict(policy.to_dict())
    assert restored.credentials[0].matches_token(secret)

    legacy = CallerCredential.from_dict({"token": secret, "caller_id": "legacy-client"})
    assert legacy.token_hash == hash_token(secret)
    assert secret not in json.dumps(legacy.to_dict())
