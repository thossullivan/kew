"""Bounded issuer requests use mocked HTTP exclusively."""

import json
import traceback
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest


@pytest.fixture
def issuer(monkeypatch):
    from kew import credential_issuer

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    return credential_issuer


def reply(body):
    return json.dumps(
        {"key": "run-secret-1944", "data": {"hash": "a" * 64, **body}}
    ).encode()


@pytest.mark.parametrize("status", [200, 201])
def test_bounded_issuer_request_and_reply(issuer, status):
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=7)
    http = Mock(
        side_effect=lambda method, url, **kw: (status, reply(json.loads(kw["body"])))
    )
    key = issuer.mint_bounded_run_key(
        "run-1944",
        3.5,
        expiry,
        ledger_id="11111111-1111-4111-8111-111111111111",
        http=http,
    )
    args, kwargs = http.call_args
    assert args == ("POST", "https://openrouter.ai/api/v1/keys")
    assert kwargs["headers"]["Authorization"] == "Bearer management-secret-1944"
    assert json.loads(kwargs["body"]) == {
        "name": "kew-run:11111111-1111-4111-8111-111111111111:run-1944",
        "limit": 3.5,
        "limit_reset": None,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "include_byok_in_limit": True,
    }
    assert datetime.fromisoformat(key.expires_at) > now
    assert datetime.fromisoformat(key.expires_at).utcoffset() == timedelta(0)
    assert key.secret == "run-secret-1944"
    assert key.identity == {
        "issuer": "openrouter",
        "key_hash": "a" * 64,
        "limit_usd": 3.5,
        "expires_at": key.expires_at,
        "limit_reset": None,
        "name": "kew-run:11111111-1111-4111-8111-111111111111:run-1944",
    }
    assert "run-secret-1944" not in repr(key)
    with pytest.raises(FrozenInstanceError):
        key.limit_usd = 100


@pytest.mark.parametrize(
    "limit", [0, -1, float("nan"), float("inf"), -float("inf"), True]
)
def test_bounded_issuer_refuses_invalid_limits_before_http(issuer, limit):
    http = Mock()
    with pytest.raises(issuer.IssuerRefused):
        issuer.mint_bounded_run_key(
            "run",
            limit,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    http.assert_not_called()


@pytest.mark.parametrize(
    "expiry", [datetime(2000, 1, 1, tzinfo=UTC), datetime(2099, 1, 1)]
)
def test_bounded_issuer_refuses_past_or_naive_expiry(issuer, expiry):
    http = Mock()
    with pytest.raises(issuer.IssuerRefused):
        issuer.mint_bounded_run_key(
            "run",
            1,
            expiry,
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    http.assert_not_called()


def test_bounded_issuer_normalizes_timezone(issuer):
    expiry = datetime.now(timezone(timedelta(hours=5))) + timedelta(minutes=5)
    http = Mock(
        side_effect=lambda method, url, **kw: (200, reply(json.loads(kw["body"])))
    )
    key = issuer.mint_bounded_run_key(
        "run", 1, expiry, ledger_id="11111111-1111-4111-8111-111111111111", http=http
    )
    assert key.expires_at.endswith("Z")
    assert datetime.fromisoformat(key.expires_at) == expiry


@pytest.mark.parametrize(
    "status, error",
    [
        (401, "IssuerRefused"),
        (403, "IssuerRefused"),
        (302, "IssuerRefused"),
        (429, "IssuerUnavailable"),
        (500, "IssuerUnavailable"),
    ],
)
def test_bounded_issuer_http_errors_are_secret_free(issuer, status, error):
    http = Mock(return_value=(status, b"run-secret-1944 management-secret-1944"))
    with pytest.raises(getattr(issuer, error)) as caught:
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "run-secret-1944" not in diagnostic
    assert "management-secret-1944" not in diagnostic


def test_bounded_issuer_transport_error_is_secret_free(issuer):
    http = Mock(side_effect=RuntimeError("run-secret-1944 management-secret-1944"))
    with pytest.raises(issuer.IssuerUnavailable) as caught:
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    assert "secret-1944" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "payload",
    [
        b"not json run-secret-1944",
        b"{}",
        b"[]",
        b'{"key": "run-secret-1944", "data": null}',
        b'{"key": "", "data": {"hash": "aa"}}',
    ],
)
def test_bounded_issuer_malformed_reply_is_typed(issuer, payload):
    with pytest.raises(issuer.MalformedIssuerReply) as caught:
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=Mock(return_value=(200, payload)),
        )
    assert "secret-1944" not in str(caught.value)


@pytest.mark.parametrize(
    "field, value",
    [
        ("limit", None),
        ("limit", 100),
        ("limit", True),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("limit_reset", "daily"),
        ("include_byok_in_limit", False),
        ("hash", "../bad"),
    ],
)
def test_bounded_issuer_rejects_unconfirmed_bounds(issuer, field, value):
    def http(method, url, **kw):
        return 200, reply({**json.loads(kw["body"]), field: value})

    with pytest.raises(issuer.MalformedIssuerReply):
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )


def test_bounded_issuer_revoke_contract(issuer):
    http = Mock(return_value=(204, b""))
    issuer.revoke_run_key("a" * 64, http=http)
    assert http.call_args.args == (
        "DELETE",
        "https://openrouter.ai/api/v1/keys/" + "a" * 64,
    )
    assert (
        http.call_args.kwargs["headers"]["Authorization"]
        == "Bearer management-secret-1944"
    )
    assert http.call_args.kwargs["body"] is None


def test_bounded_issuer_without_management_key_refuses(issuer, monkeypatch):
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    http = Mock()
    with pytest.raises(issuer.IssuerUnavailable):
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    http.assert_not_called()


def test_bounded_issuer_default_http_closes_connection(issuer, monkeypatch):
    connection = Mock()
    connection.getresponse.return_value.status = 204
    connection.getresponse.return_value.read.return_value = b""
    factory = Mock(return_value=connection)
    monkeypatch.setattr(issuer, "HTTPSConnection", factory)
    issuer.revoke_run_key("a" * 64)
    factory.assert_called_once_with("openrouter.ai", timeout=15)
    assert connection.request.call_args.args[:2] == (
        "DELETE",
        "/api/v1/keys/" + "a" * 64,
    )
    connection.close.assert_called_once()


def test_bounded_issuer_revokes_unconfirmed_reply(issuer):
    def http(method, url, **kw):
        if method == "DELETE":
            return 204, b""
        return 200, reply({**json.loads(kw["body"]), "limit": 999})

    mock_http = Mock(side_effect=http)
    with pytest.raises(issuer.MalformedIssuerReply):
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=mock_http,
        )
    assert [call.args[0] for call in mock_http.call_args_list] == ["POST", "DELETE"]
    assert mock_http.call_args.args[1].endswith("/" + "a" * 64)


@pytest.mark.parametrize("field", ["limit_reset", "include_byok_in_limit"])
def test_bounded_reply_requires_explicit_bound_confirmation(issuer, field):
    """Missing fields, changed semantics, and the positive control share a reply."""
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    invalid_values = {
        "limit_reset": ["daily", "monthly", False],
        "include_byok_in_limit": [False, None, 1],
    }
    missing = object()
    for value in [missing, *invalid_values[field]]:

        def http(method, url, value=value, **kw):
            if method == "DELETE":
                return 204, b""
            data = json.loads(kw["body"])
            if value is missing:
                del data[field]
            else:
                data[field] = value
            return 200, reply(data)

        mock_http = Mock(side_effect=http)
        with pytest.raises(issuer.MalformedIssuerReply):
            issuer.mint_bounded_run_key(
                "run",
                1,
                expiry,
                ledger_id="11111111-1111-4111-8111-111111111111",
                http=mock_http,
            )
        assert [c.args[0] for c in mock_http.call_args_list] == ["POST", "DELETE"]
        assert mock_http.call_args.args[1] == issuer.KEYS_ENDPOINT + "/" + "a" * 64

    confirmed_http = Mock(
        side_effect=lambda method, url, **kw: (200, reply(json.loads(kw["body"])))
    )
    key = issuer.mint_bounded_run_key(
        "run",
        1,
        expiry,
        ledger_id="11111111-1111-4111-8111-111111111111",
        http=confirmed_http,
    )
    assert key.key_hash == "a" * 64
    confirmed_http.assert_called_once()


@pytest.mark.parametrize(
    "identity, source",
    [
        ("management-secret-1944", "provisioning"),
        ("b" * 64, "provisioning"),
        ("run-secret-1944", "issued"),
        ("c" * 64, "issued"),
        ("sk-or-v1-" + "d" * 64, "credential"),
        ("sk-or-" + "e" * 64, "credential"),
        ("Q9mT2vWx7RpLs4Za8NdJ6cFb3YhK5uEo" * 2, "credential"),
        ("a" * 63, "grammar"),
        ("a" * 65, "grammar"),
        ("A" * 64, "grammar"),
        ("hash_with-dashes", "grammar"),
    ],
    ids=[
        "management",
        "hex-management",
        "issued",
        "hex-issued",
        "sk-or-v1",
        "sk-or",
        "opaque-token",
        "short",
        "long",
        "uppercase",
        "punctuation",
    ],
)
def test_bounded_cleanup_never_uses_credentials_as_identities(
    issuer, monkeypatch, caplog, identity, source
):
    if source == "provisioning":
        monkeypatch.setenv(issuer.PROVISIONING_ENV, identity)
    secret = identity if source == "issued" else "run-secret-1944"

    def http(method, url, **kw):
        if method == "DELETE":
            return 204, b""
        data = {**json.loads(kw["body"]), "hash": identity, "limit": 999}
        return 200, json.dumps({"key": secret, "data": data}).encode()

    mock_http = Mock(side_effect=http)
    with pytest.raises(issuer.MalformedIssuerReply):
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=mock_http,
        )
    assert [c.args[0] for c in mock_http.call_args_list] == ["POST"]
    assert all(identity not in c.args[1] for c in mock_http.call_args_list)
    assert "revocation skipped: invalid issuer identity" in caplog.text
    assert identity not in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "identity",
    ["management-secret-1944", "sk-or-v1-" + "f" * 64, "z" * 64],
    ids=["management-secret", "key-shaped", "hex-length-run"],
)
def test_bounded_direct_revocation_checks_identity(issuer, caplog, identity):
    http = Mock(return_value=(204, b""))
    with pytest.raises(issuer.IssuerRefused):
        issuer.revoke_run_key(identity, http=http)
    http.assert_not_called()
    assert "revocation skipped: invalid issuer identity" in caplog.text
    assert identity not in caplog.text


def test_bounded_cleanup_records_transport_failure(issuer, caplog):
    def http(method, url, **kw):
        if method == "DELETE":
            raise RuntimeError("management-secret-1944 run-secret-1944")
        return 200, reply({**json.loads(kw["body"]), "limit": 999})

    with pytest.raises(issuer.MalformedIssuerReply):
        issuer.mint_bounded_run_key(
            "run",
            1,
            datetime.now(UTC) + timedelta(minutes=5),
            ledger_id="11111111-1111-4111-8111-111111111111",
            http=http,
        )
    assert "revocation outcome unconfirmed" in caplog.text
    assert "secret-1944" not in caplog.text
