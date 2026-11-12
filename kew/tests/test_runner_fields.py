"""Credential containment for field traversal and final representations."""

from collections import UserDict, UserList
from dataclasses import dataclass

from kew.runner_fields import (
    exception_text,
    redact_authorization_values,
    runner_text,
    sanitize_exception_args,
)


@dataclass(frozen=True, slots=True)
class _OpaqueHeader:
    Authorization: str

    def __repr__(self):
        raise AssertionError("dataclass fields must be read without repr")


def test_round13_dataclass_fields_are_read_without_repr():
    value = UserDict({"nested": UserList([_OpaqueHeader("opaque-TOPSECRET")])})
    assert "opaque-TOPSECRET" not in runner_text(value)
    assert "opaque-TOPSECRET" not in str(redact_authorization_values(value))
    assert value["nested"][0].Authorization == "opaque-TOPSECRET"


def test_round13_hashable_dataclass_mapping_keys_remain_safe():
    value = {_OpaqueHeader("opaque-TOPSECRET"): "public", "status": "available"}
    retained = exception_text(ValueError(value))
    assert "opaque-TOPSECRET" not in retained
    # The serialized key contains the Authorization stem and is opaque too.
    assert "public" not in retained
    assert "available" in retained
    assert "<redacted>" in retained


def test_round13_unknown_exception_args_are_safe_after_propagation(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://user:ProxySecret123@proxy.invalid")

    class ReprOnly:
        def __repr__(self):
            return "ProxySecret123"

    error = ValueError(UserDict({"nested": [ReprOnly()]}))
    sanitize_exception_args(error)
    assert "ProxySecret123" not in str(error)
    assert "<HTTP_PROXY redacted>" in str(error)
