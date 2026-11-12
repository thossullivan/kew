"""Contract tests for the pinned Switchyard compatibility evidence."""

from __future__ import annotations

import hashlib
import re
import ssl
from http.client import HTTPException
from pathlib import Path

import pytest

from kew.switchyard import ProbeResponse, probe_switchyard

UPSTREAM_REVISION = "6ff491acd7eb1ec239525a1baa015730c3d3564a"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "switchyard"
# The provenance record sits beside the fixtures it binds to the pinned
# upstream build: the revision and the SHA-256 of each captured file.
REPORT = FIXTURE_DIR / "PROVENANCE.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixture_contract_binds_captures_to_pinned_build() -> None:
    report = REPORT.read_text(encoding="utf-8")
    help_fixture = FIXTURE_DIR / "server-help.txt"
    version_fixture = FIXTURE_DIR / "server-version.txt"

    assert UPSTREAM_REVISION in report
    assert version_fixture.read_text(encoding="utf-8") == "switchyard-server 0.1.0\n"

    recorded_hashes = dict(
        re.findall(
            r"`(server-(?:help|version)\.txt)`\s*\|\s*`([0-9a-f]{64})`",
            report,
        )
    )
    assert recorded_hashes == {
        help_fixture.name: _sha256(help_fixture),
        version_fixture.name: _sha256(version_fixture),
    }


class FakeProbeTransport:
    def __init__(
        self,
        *,
        version: str = "switchyard-server 0.1.0\n",
        dry_run: str = "ok",
        responses: dict[str, ProbeResponse] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.version = version
        self.dry_run = dry_run
        self.responses = responses or {
            "/health": ProbeResponse(200, {"status": "ok"}),
            "/v1/models": ProbeResponse(
                200, {"data": [{"id": "switchyard/passthrough"}]}
            ),
        }
        self.failure = failure
        self.timeouts: list[float] = []

    def probe_version(self, timeout_seconds: float) -> str:
        self.timeouts.append(timeout_seconds)
        if self.failure:
            raise self.failure
        return self.version

    def probe_dry_run(self, timeout_seconds: float) -> str:
        self.timeouts.append(timeout_seconds)
        return self.dry_run

    def get_json(self, url: str, timeout_seconds: float) -> ProbeResponse:
        self.timeouts.append(timeout_seconds)
        return self.responses[url.removeprefix("https://switchyard.example")]


def test_switchyard_probe_admits_only_after_all_bounded_checks() -> None:
    transport = FakeProbeTransport()

    result = probe_switchyard(
        endpoint="https://switchyard.example",
        route="switchyard/passthrough",
        expected_version="0.1.0",
        transport=transport,
        timeout_seconds=1.25,
    )

    assert result.admitted is True
    assert result.reason == "ready"
    assert result.observed_version == "0.1.0"
    assert result.endpoint == "https://switchyard.example"
    assert transport.timeouts == [1.25, 1.25, 1.25, 1.25]


@pytest.mark.parametrize(
    ("transport", "reason"),
    [
        (FakeProbeTransport(failure=OSError("secret-token=abc")), "unreachable"),
        (FakeProbeTransport(failure=HTTPException("private response")), "unreachable"),
        (FakeProbeTransport(failure=KeyError("private-key")), "unreachable"),
        (
            FakeProbeTransport(failure=ssl.SSLError("private certificate path")),
            "tls_error",
        ),
        (FakeProbeTransport(version="switchyard-server 9.9.9\n"), "wrong_version"),
        (FakeProbeTransport(dry_run="invalid"), "dry_run_failed"),
        (
            FakeProbeTransport(
                responses={
                    "/health": ProbeResponse(200, {"status": "ok"}),
                    "/v1/models": ProbeResponse(200, {"data": [{"id": "other"}]}),
                }
            ),
            "missing_route",
        ),
        (
            FakeProbeTransport(
                responses={
                    "/health": ProbeResponse(200, {"status": "ok"}),
                    "/v1/models": ProbeResponse(200, {"unexpected": []}),
                }
            ),
            "malformed_response",
        ),
        (
            FakeProbeTransport(
                responses={"/health": ProbeResponse(503, {"status": "ok"})}
            ),
            "unhealthy",
        ),
        (
            FakeProbeTransport(
                responses={
                    "/health": ProbeResponse(200, {"status": "ok"}),
                    "/v1/models": ProbeResponse(500, {}),
                }
            ),
            "models_unavailable",
        ),
    ],
)
def test_switchyard_probe_refusals_are_uniform_and_sanitized(
    transport: FakeProbeTransport, reason: str
) -> None:
    result = probe_switchyard(
        endpoint="https://switchyard.example",
        route="switchyard/passthrough",
        expected_version="0.1.0",
        transport=transport,
    )

    assert result.admitted is False
    assert result.reason == reason
    rendered = repr(result)
    assert "secret-token" not in rendered
    assert "private certificate" not in rendered


def test_switchyard_probe_result_sanitizes_attacker_reachable_endpoint() -> None:
    result = probe_switchyard(
        endpoint="https://token@example.test:444/private?api_key=secret",
        route="switchyard/passthrough",
        expected_version="0.1.0",
        transport=FakeProbeTransport(failure=OSError("offline")),
    )

    assert result.endpoint == "https://example.test:444"
    assert "token" not in repr(result)
    assert "api_key" not in repr(result)


def test_switchyard_probe_normalizes_trailing_slash() -> None:
    result = probe_switchyard(
        endpoint="https://switchyard.example/",
        route="switchyard/passthrough",
        expected_version="0.1.0",
        transport=FakeProbeTransport(),
    )

    assert result.admitted is True


def test_switchyard_probe_sanitizes_invalid_scheme() -> None:
    result = probe_switchyard(
        endpoint="file://token@example.test/private?api_key=secret",
        route="switchyard/passthrough",
        expected_version="0.1.0",
        transport=FakeProbeTransport(failure=OSError("offline")),
    )

    assert result.endpoint == "invalid-endpoint"
