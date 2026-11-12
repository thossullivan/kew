"""Bounded OpenRouter issuer fixture for production container wiring tests."""

import hashlib
from datetime import datetime
from unittest.mock import Mock

import pytest

from kew import credential_issuer


def _mint_key(
    run_id: str, limit_usd: float, expires_at: datetime, *, ledger_id: str, on_malformed
) -> credential_issuer.BoundedRunKey:
    return credential_issuer.BoundedRunKey(
        "issued-container-secret",
        hashlib.sha256(run_id.encode()).hexdigest(),
        limit_usd,
        expires_at.isoformat(),
    )


@pytest.fixture
def issued_openrouter(monkeypatch):
    """Meet C1 without bypassing issuance, launch, or confirmed revocation."""
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "test-provisioning-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "reusable-host-secret")
    mint = Mock(side_effect=_mint_key)
    revoke = Mock(return_value="deleted")
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    monkeypatch.setattr(credential_issuer, "list_run_keys", Mock(return_value=[]))
    return mint, revoke
