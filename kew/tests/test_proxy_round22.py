"""Round 22: preserve flooded reports and blank checkout proxy defaults."""

import json
import time

import pytest
from test_docs_truthfulness import _assert_checkout_proxy_security_documentation
from test_proxy_round20 import _agent_launch, _container_launch
from test_proxy_round21 import _retain

from kew import sandbox
from kew.runner_fields import redact_authorization_values

MARKER = "<HTTPS_PROXY redacted>"
CONTROLS = "## Dispatch Report\nerror_category: transient\nexit code 1\n"
EXTRA_NAMES = ("ALL_PROXY", "FTP_PROXY", "all_proxy", "ftp_proxy")
TEXT_SURFACES = ("text", "values", "display", "exception", "log_record", "evidence")


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("surface", TEXT_SURFACES)
@pytest.mark.parametrize("count", [9, 500])
def test_round22_long_form_flood_preserves_every_other_character(
    monkeypatch, tmp_path, capsys, caplog, surface, count
):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid/timeout")
    report = CONTROLS + "retry: timeout\n" * count + "Done."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retain(report, surface, tmp_path, capsys, caplog)
        assert retained == report.replace("timeout", MARKER)
        assert retained.count(MARKER) == count
        assert "timeout" not in retained
        # Opaque structured fields still replace only their containing leaf.
        payload = {"credential": report, "header": CONTROLS, "exit_code": 1}
        assert redact_authorization_values(payload) == {
            **payload,
            "credential": "<redacted>",
        }
        assert payload["credential"] == report


@pytest.mark.serial
def test_round22_five_hundred_matches_finish_within_bound(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid/timeout")
    report = CONTROLS + "retry: timeout\n" * 500 + "z" * 65_536
    started = time.monotonic()
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials(report)
    elapsed = time.monotonic() - started
    assert retained == report.replace("timeout", MARKER)[:65_536]
    assert retained.count(MARKER) == 500
    assert len(retained) == sandbox.MAX_REDACTED_FIELD_CHARS == 65_536
    assert elapsed < 2.0


@pytest.mark.parametrize(
    "launch", [_agent_launch, _container_launch], ids=["agent", "container"]
)
@pytest.mark.parametrize("count", [9, 500])
def test_round22_launch_persists_every_flood_marker(
    tmp_path, monkeypatch, launch, count
):
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid/timeout")
    emitted = "error_category: transient\nexit code 1\n" + "retry: timeout\n" * count
    launch(tmp_path, monkeypatch, emitted)
    [path] = tmp_path.glob("*.evidence.json")
    report = json.loads(path.read_text())["report"]
    assert report == f"## Dispatch Report\n{emitted.replace('timeout', MARKER)}\nDone."
    assert report.count(MARKER) == count


@pytest.mark.parametrize("surface", TEXT_SURFACES)
def test_round22_generated_marker_long_form_is_replaced_locally(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/timeout")
    # HTTP_PROXY appears only after the first replacement generates its marker.
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid/HTTP_PROXY")
    report = CONTROLS + "retry: timeout\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retain(report, surface, tmp_path, capsys, caplog)
    assert retained == report.replace("timeout", f"<{MARKER} redacted>")
    assert "HTTP_PROXY" not in retained


@pytest.mark.parametrize("component", ["HTTPS_PROXY", "redacted"])
def test_round22_self_reproducing_marker_preserves_unrelated_text(
    monkeypatch, component
):
    monkeypatch.setenv("HTTPS_PROXY", f"https://proxy.invalid/{component}")
    report = CONTROLS + f"retry: {component}\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials(report)
    assert retained.startswith(CONTROLS + "retry: ")
    assert retained.endswith("\nDone.")
    assert component not in retained
    assert "█" in retained


@pytest.mark.parametrize("suffix", ["HTTP_PROXY%20%3C%3E", "%E2%96%88marker"])
def test_round22_residual_marker_cannot_recreate_selected_forms(monkeypatch, suffix):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/redacted")
    monkeypatch.setenv("HTTPS_PROXY", f"https://proxy.invalid/{suffix}")
    report = CONTROLS + "retry: redactedmarker\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials(report)
        long_forms = [item for item in sandbox._credential_forms() if not item[1].short]
    assert retained.startswith(CONTROLS + "retry: ")
    assert retained.endswith("marker\nDone.")
    assert not sandbox.credential_redaction.contains(retained, long_forms)


def _checkout_argv(tmp_path, policy, launch):
    common = {
        "runtime": "docker",
        "image": "test:image",
        "checkout_dir": tmp_path / "checkout",
        "slug": "round22",
        "uid": 501,
        "gid": 20,
    }
    if launch == "extract":
        return sandbox.build_extract_argv(
            **common, artifact_dir=tmp_path / "artifact", base_sha="a" * 40
        )
    return sandbox.build_checkout_program_argv(**common, program=["/bin/true"])


def _environment_args(argv):
    return [argv[index + 1] for index, argument in enumerate(argv) if argument == "-e"]


@pytest.mark.parametrize("policy", ["none", "inherit"])
@pytest.mark.parametrize("launch", ["extract", "verification"])
@pytest.mark.parametrize("host_present", [False, True])
def test_round22_checkout_explicitly_blanks_extra_proxy_names(
    monkeypatch, tmp_path, policy, launch, host_present
):
    docker_config = tmp_path / "docker"
    docker_config.mkdir()
    docker_config.joinpath("config.json").write_text(
        json.dumps({"proxies": {"default": {"allProxy": "socks5://user:secret@proxy"}}})
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    if host_present:
        for name in sandbox._PROXY_BUILD_NAMES:
            monkeypatch.setenv(name, "socks5://user:host-secret@proxy")
    argv = _checkout_argv(tmp_path, policy, launch)
    env = _environment_args(argv)
    for name in EXTRA_NAMES:
        # Explicit assignments override Docker's allProxy configuration injection.
        assert f"{name}=" in env
    assert len(env) == 11  # Ten proxy assignments plus HOME.
    for name in sandbox.CONTAINER_PROXY_NAMES:
        # These launches carry --network none, so a forwarded proxy value can
        # never be used and would only place its credentials in the container:
        # every name is blanked whatever the policy says (governed review round
        # 4, 2026-09-21). `policy` still parametrizes the caller's posture.
        assert f"{name}=" in env
        assert name not in env
    assert all("secret" not in argument for argument in argv)


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round22_security_docs_pin_extra_proxy_blanking(monkeypatch, tmp_path, policy):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.setenv(name, "https://proxy.invalid/fixture")
    argv = _checkout_argv(tmp_path, policy, "extract")
    _assert_checkout_proxy_security_documentation(_environment_args(argv), policy)
