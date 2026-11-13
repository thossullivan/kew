"""``kew sandbox network`` creates the allowlist network the doctor checks (#2501)."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

import kew.cli.doctor as doctor_module
from kew import subprocess_utils
from kew.cli import app
from kew.config import SandboxConfig
from kew.readiness import CheckStatus

runner = CliRunner()

_NETWORK = "kew-allowlist"


class FakeRuntime:
    """A container runtime whose networks live in a dict, not a daemon."""

    def __init__(self, name: str = "docker", *, internal: bool = False) -> None:
        self.name = name
        self.internal = internal
        self.networks: dict[str, dict[str, object]] = {}
        self.calls: list[list[str]] = []
        #: Another creator wins the race between our inspect and our create.
        self.race_on_create = False

    def run(self, cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        assert cmd[0] == self.name, cmd
        verb = cmd[1:3]
        if verb == ["network", "inspect"]:
            network = self.networks.get(cmd[3])
            if network is None:
                return subprocess.CompletedProcess(cmd, 1, "[]", "network not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps([network]), "")
        if verb == ["network", "create"]:
            name = cmd[-1]
            if self.race_on_create:
                self._add(name, "bridge")
            if name in self.networks:
                return subprocess.CompletedProcess(cmd, 1, "", "already exists")
            self._add(name, cmd[cmd.index("--driver") + 1])
            return subprocess.CompletedProcess(cmd, 0, f"{name}\n", "")
        raise AssertionError(f"unexpected runtime call {cmd}")

    def _add(self, name: str, driver: str) -> None:
        self.networks[name] = {
            "Name": name,
            "Driver": driver,
            "Internal": self.internal,
        }

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["network", "create"]]


def _write_config(tmp_path, *, runtime: str = "docker", network: str | None = None):
    logs = tmp_path / "logs"
    logs.mkdir()
    sandbox = f'runtime = "{runtime}"\n'
    if network is not None:
        sandbox += f'network = "{network}"\n'
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "claude-code"\n'
        f"[sandbox]\n{sandbox}"
        f'[logging]\ndir = "{logs}"\n'
    )


@pytest.fixture
def fake(tmp_path, monkeypatch, request) -> FakeRuntime:
    name = getattr(request, "param", "docker")
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, runtime=name)
    runtime = FakeRuntime(name)
    monkeypatch.setattr(subprocess_utils, "TOOL_PATHS", {})
    monkeypatch.setattr(subprocess_utils, "run", runtime.run)
    monkeypatch.setattr("kew.sandbox.detect_runtime", lambda _c: f"/usr/bin/{name}")
    return runtime


def _doctor_network_check(runtime: FakeRuntime) -> tuple[str | None, CheckStatus]:
    """Run the real ``allowlist-network`` doctor check against the fake."""
    recorded: dict[str, CheckStatus] = {}

    def record(check_id, status, *_args, **_kwargs) -> None:
        recorded[check_id] = status

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(doctor_module, "_record", record)
        issue = doctor_module._check_allowlist_network(
            SandboxConfig(runtime=runtime.name), runtime.name
        )
    return issue, recorded["allowlist-network"]


@pytest.mark.parametrize("fake", ["docker", "podman"], indirect=True)
def test_creates_the_network_the_doctor_check_then_passes(fake: FakeRuntime) -> None:
    issue, status = _doctor_network_check(fake)
    assert status == CheckStatus.FAIL and issue is not None

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert fake.creates() == [
        [fake.name, "network", "create", "--driver", "bridge", _NETWORK]
    ]
    assert fake.networks[_NETWORK]["Driver"] == "bridge"
    assert f"created {_NETWORK} on {fake.name}" in result.output
    assert _doctor_network_check(fake) == (None, CheckStatus.OK)


def test_created_plain_bridge_says_egress_is_open(fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert "plain bridge" in result.stderr
    assert "full egress" in result.stderr


def test_internal_network_carries_no_open_egress_warning(fake: FakeRuntime) -> None:
    fake.internal = True

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert "plain bridge" not in result.output


def test_second_run_changes_nothing_and_exits_zero(fake: FakeRuntime) -> None:
    first = runner.invoke(app, ["sandbox", "network"])
    assert first.exit_code == 0, first.output
    before = json.dumps(fake.networks, sort_keys=True)

    second = runner.invoke(app, ["sandbox", "network"])

    assert second.exit_code == 0, second.output
    assert len(fake.creates()) == 1
    assert json.dumps(fake.networks, sort_keys=True) == before
    assert "exists. kew did not modify it" in second.output


def test_existing_network_of_another_shape_is_left_alone(fake: FakeRuntime) -> None:
    fake.networks[_NETWORK] = {"Name": _NETWORK, "Driver": "macvlan", "Internal": 0}

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert fake.creates() == []
    assert fake.networks[_NETWORK]["Driver"] == "macvlan"


def test_losing_a_create_race_reports_the_network_present(fake: FakeRuntime) -> None:
    fake.race_on_create = True

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert "exists. kew did not modify it" in result.output
    assert _doctor_network_check(fake) == (None, CheckStatus.OK)


def test_failed_create_that_leaves_no_network_exits_two(
    fake: FakeRuntime, monkeypatch
) -> None:
    def refuse(cmd, **kwargs):
        if cmd[1:3] == ["network", "create"]:
            fake.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "daemon unreachable")
        return fake.run(cmd, **kwargs)

    monkeypatch.setattr(subprocess_utils, "run", refuse)

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 2, result.output
    assert "daemon unreachable" in result.output
    assert _NETWORK not in fake.networks


def test_check_reports_absent_and_creates_nothing(fake: FakeRuntime) -> None:
    result = runner.invoke(app, ["sandbox", "network", "--check"])

    assert result.exit_code == 1, result.output
    assert fake.creates() == []
    assert _NETWORK not in fake.networks
    assert f"{_NETWORK} is absent" in result.output


def test_check_reports_present_and_exits_zero(fake: FakeRuntime) -> None:
    runner.invoke(app, ["sandbox", "network"])

    result = runner.invoke(app, ["sandbox", "network", "--check"])

    assert result.exit_code == 0, result.output
    assert len(fake.creates()) == 1


def test_openshell_creates_nothing_and_says_why(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, runtime="openshell")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess_utils, "run", lambda cmd, **_k: calls.append(cmd))

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "OpenShell enforces its own egress policy" in result.output
    assert "kew created nothing" in result.output


def test_missing_runtime_exits_two_without_creating(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess_utils, "run", lambda cmd, **_k: calls.append(cmd))
    monkeypatch.setattr("kew.sandbox.detect_runtime", lambda _c: None)

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 2, result.output
    assert "not found on PATH" in result.output
    assert calls == []


@pytest.mark.parametrize("network", ["open", "corp-egress"])
def test_non_allowlist_posture_refuses_without_creating(
    tmp_path, monkeypatch, network: str
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, network=network)
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess_utils, "run", lambda cmd, **_k: calls.append(cmd))

    result = runner.invoke(app, ["sandbox", "network"])

    assert result.exit_code == 2, result.output
    assert 'network = "allowlist"' in result.output
    assert calls == []
