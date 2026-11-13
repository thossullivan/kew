"""The exact-boundary live readiness probe (I05B, Tasks 19 and 21).

What is under test here is not "does a provider answer" — it is every promise
kew makes about the ONE call it is willing to pay for as a diagnostic:

* it is bounded (one turn, 90 seconds, $0.02) and never retried;
* it crosses the EXACT boundary a dispatch would, carrying none of the
  customer credentials, checkout, or task shape a dispatch carries;
* its cost is recorded with honest certainty — priced, unpriced and
  subscription-metered are three different truths, and none of them is $0;
* a process death leaves a terminal, unacknowledged record rather than an
  orphan or a silent re-spend;
* and no amount of passing STATIC checks ever gets to imply the answer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from kew import subprocess_utils
from kew.config import (
    AgentConfig,
    DispatchConfig,
    ProjectConfig,
    SandboxConfig,
    TrackerConfig,
)
from kew.readiness import Containment, ProbeCost, Provenance
from kew.readiness_probe import (
    PROBE_CEILING_USD,
    PROBE_EXPECTED_REPLY,
    PROBE_PROMPT,
    PROBE_TIMEOUT_SECONDS,
    LiveProbeUnsupported,
    LiveStatus,
    ProbeExecution,
    ProbeLaunch,
    ProbeRoute,
    SupportingAccess,
    build_probe_launch,
    check_supporting_access,
    confirm_route_live,
    create_probe_workspace,
    execute_probe,
    interpret_probe_cost,
    judge_probe,
    models_agree,
    probe_bound,
    probe_env,
    reclaim_stranded_probes,
    resolve_probe_route,
    route_live_status,
    stranded_probes,
)
from kew.run_store import READINESS_PROBE_RUN_KIND, SqliteRunStore
from kew.run_store_readiness import ProbeState

_MODEL = "claude-opus-5"


def _config(**overrides: object) -> DispatchConfig:
    """A route that resolves to a HOST claude-code leg without a runtime."""
    return DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        agent=AgentConfig(model=_MODEL),
        # Uncontained on purpose: the probe must resolve an exact boundary on a
        # machine with no container runtime, which is where doctor usually runs.
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
        **overrides,  # type: ignore[arg-type]
    )


def _route(config: DispatchConfig | None = None) -> ProbeRoute:
    return resolve_probe_route(config or _config(), complexity="complex")


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(str(tmp_path / "dispatch.db"))


def _fake_call(tmp_path: Path, body: str) -> ProbeLaunch:
    """A launch whose "runner CLI" is this interpreter running *body*.

    Binding the allowlist entry rather than dropping a shell script keeps the
    test honest on hosts whose temp filesystem is mounted ``noexec`` — the
    thing under test is kew's launch/read/cap loop, not the exec bit.
    """
    subprocess_utils.TOOL_PATHS["claude"] = sys.executable
    return ProbeLaunch(
        argv=("claude", "-c", body),
        env=dict(os.environ),
        cwd=tmp_path,
        workspace=tmp_path,
    )


@pytest.fixture(autouse=True)
def _restore_tool_paths():
    before = dict(subprocess_utils.TOOL_PATHS)
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(before)


@pytest.fixture
def _access_ok():
    with patch(
        "kew.readiness_probe.check_supporting_access", return_value=SupportingAccess()
    ) as spy:
        yield spy


def _ok_reply_lines(model: str = _MODEL, cost: float | None = 0.0031) -> list[str]:
    result: dict[str, object] = {"type": "result", "subtype": "success"}
    if cost is not None:
        result["modelUsage"] = {model: {"costUSD": cost, "outputTokens": 2}}
    return [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "OK"}]},
            }
        ),
        json.dumps(result),
    ]


# ---------------------------------------------------------------------------
# Task 19 — the bounded call itself
# ---------------------------------------------------------------------------


def test_the_probe_call_is_one_turn_capped_at_two_cents() -> None:
    launch = build_probe_launch(_route(), probe_id="readiness-probe-abc")
    argv = list(launch.argv)

    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--max-budget-usd") + 1] == str(PROBE_CEILING_USD)
    assert argv[argv.index("-p") + 1] == PROBE_PROMPT
    assert PROBE_TIMEOUT_SECONDS == 90


def test_the_probe_never_carries_the_dispatch_task_shape() -> None:
    """A probe proves the boundary; it must not be able to DO anything."""
    launch = build_probe_launch(_route(), probe_id="readiness-probe-abc")

    assert "--dangerously-skip-permissions" not in launch.argv
    assert "--resume" not in launch.argv
    # Without permission skipping a non-interactive turn cannot get tool
    # permission at all, and the prompt asks for none either way.
    assert "do not read or write any file" in PROBE_PROMPT


def test_an_unboundable_runner_is_refused_rather_than_probed_unbounded() -> None:
    with pytest.raises(LiveProbeUnsupported):
        probe_bound("opencode")


def test_the_probe_workspace_is_an_empty_host_owned_repository() -> None:
    workspace = create_probe_workspace("readiness-probe-abc")
    try:
        visible = sorted(p.name for p in workspace.iterdir())
        assert visible == [".git"], visible
    finally:
        for path in sorted(workspace.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        workspace.rmdir()


def test_customer_and_publication_credentials_never_reach_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_not-a-real-token")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_not-a-real-token")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    env = probe_env()

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "LINEAR_API_KEY" not in env
    # The inference credential IS the boundary under test, so it stays.
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-not-a-real-key"


@pytest.mark.serial
@pytest.mark.parametrize("contained", [False, True], ids=["host", "contained"])
@pytest.mark.parametrize("issued", [False, True], ids=["host-key", "issued-key"])
def test_bounded_probe_child_has_no_management_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contained: bool, issued: bool
) -> None:
    from kew.readiness_probe import _execute_probe_once
    from kew.runners.claude_code import ClaudeCodeRunner

    inference = "run-secret-1944" if issued else "host-secret-1944"
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": os.defpath,
            "OPENROUTER_PROVISIONING_KEY": "management-secret-1944",
            "OPENROUTER_API_KEY": inference,
            "GH_TOKEN": "publication-secret-1944",
        },
    )
    monkeypatch.setattr(
        "kew.readiness_probe.create_probe_workspace", lambda _: tmp_path
    )
    # Compose the production container argv without querying a runtime.
    monkeypatch.setattr("kew.sandbox.resource_limit_args", lambda *_: [])
    route = _contained_route() if contained else _route()
    launch = build_probe_launch(route, probe_id="readiness-probe-1944")
    assert (launch.container_name is not None) is contained
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "claude", sys.executable)
    body = "import json, os; print(json.dumps(dict(os.environ)))"
    # Execute a real child with the exact composed environment; no Docker runs.
    execution, lines = _execute_probe_once(
        replace(launch, argv=("claude", "-c", body)),
        ClaudeCodeRunner(),
        deadline=time.monotonic() + PROBE_TIMEOUT_SECONDS,
        on_started=None,
    )
    assert execution.exit_code == 0
    child_env = json.loads("".join(lines))
    assert "OPENROUTER_PROVISIONING_KEY" not in child_env
    assert not {k for k in child_env if k.startswith("OPENROUTER_")}
    assert "GH_TOKEN" not in child_env
    assert child_env["KEW_READINESS_PROBE"] == "1"
    assert os.environ["OPENROUTER_PROVISIONING_KEY"] == "management-secret-1944"


def test_the_probe_boundary_claims_no_customer_source_identity() -> None:
    facts = _route().profile.column_values()

    assert facts["source_provider"] is None
    assert facts["containment"] == Containment.UNCONTAINED.value


# ---------------------------------------------------------------------------
# Task 19 — judging one call
# ---------------------------------------------------------------------------


def test_a_reply_that_is_not_exactly_ok_fails_the_probe() -> None:
    ok, detail = judge_probe(
        ProbeExecution(exit_code=0, reply="Sure! OK."), requested_model=_MODEL
    )

    assert not ok
    assert PROBE_EXPECTED_REPLY in detail


def test_a_reported_model_that_is_not_the_requested_one_fails_the_probe() -> None:
    ok, detail = judge_probe(
        ProbeExecution(exit_code=0, reply="OK", reported_model="claude-haiku-4-5"),
        requested_model=_MODEL,
    )

    assert not ok
    assert "claude-haiku-4-5" in detail


def test_a_dated_snapshot_of_the_requested_family_is_the_same_route() -> None:
    assert models_agree(_MODEL, "claude-opus-5-20260401")
    assert not models_agree(_MODEL, "claude-sonnet-5")


def test_a_nonzero_exit_fails_the_probe_without_inventing_a_reason() -> None:
    ok, detail = judge_probe(ProbeExecution(exit_code=7), requested_model=_MODEL)

    assert not ok
    assert "7" in detail


# ---------------------------------------------------------------------------
# Task 19 — priced, subscription, and unpriced are three truths
# ---------------------------------------------------------------------------


def test_a_priced_runner_records_the_cost_it_reported() -> None:
    cost = interpret_probe_cost("claude-code", 0.0031)

    assert cost.amount_usd == pytest.approx(0.0031)
    assert cost.certainty is Provenance.MEASURED


def test_a_subscription_runner_records_an_unknown_cost_never_zero() -> None:
    """codex meters on a subscription kew cannot read — that is not free."""
    cost = interpret_probe_cost("codex", None)

    assert cost.certainty is Provenance.UNAVAILABLE
    assert cost.source == ""


def test_an_unpriced_local_runner_records_a_measured_zero() -> None:
    cost = interpret_probe_cost("lemonade", None)

    assert cost.amount_usd == 0.0
    assert cost.certainty is Provenance.MEASURED


def test_a_priced_runner_that_reports_nothing_records_unknown_not_zero() -> None:
    cost = interpret_probe_cost("claude-code", None)

    assert cost.certainty is Provenance.UNAVAILABLE


# ---------------------------------------------------------------------------
# Task 19 — execution, caps, and process-death windows
# ---------------------------------------------------------------------------


def test_a_real_call_reports_the_reply_the_model_and_the_cost(
    tmp_path: Path,
) -> None:
    from kew.runners.claude_code import ClaudeCodeRunner

    emit = "\n".join(f"print({line!r})" for line in _ok_reply_lines())
    launch = _fake_call(tmp_path, emit)

    execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 0
    assert execution.reply == "OK"
    assert execution.reported_model == _MODEL
    assert execution.reported_cost_usd == pytest.approx(0.0031)


_CPU_CAP_REJECTION = (
    "docker: Error response from daemon: Range of CPUs is from "
    "0.01 to 2.00, as there are only 2 CPUs available."
)


_NAME_CONFLICT = (
    'docker: Error response from daemon: Conflict. The container name "/kew-probe-1"'
    ' is already in use by container "abc123". You have to remove (or rename) that'
    " container to be able to reuse that name."
)


def _contained_fake_call(
    tmp_path: Path,
    *,
    cpus: str,
    reject_cpus: set[str],
    conflict_cpus: set[str] | None = None,
) -> tuple[ProbeLaunch, Path]:
    """A contained-looking launch whose runtime is this interpreter.

    The script plays the docker client: it refuses (exit 125, the canonical
    range message) when the ``--cpus`` value is in *reject_cpus*, refuses with
    the daemon's name-conflict message when it is in *conflict_cpus* (the name
    is held by another container), and otherwise prints the runner's OK reply.
    Every launch appends its argv to a log.
    """
    subprocess_utils.TOOL_PATHS["docker"] = sys.executable
    log = tmp_path / "launches.log"
    reply = tmp_path / "reply.txt"
    reply.write_text("\n".join(_ok_reply_lines()) + "\n")
    script = tmp_path / "fake-docker.py"
    script.write_text(
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "cpus = sys.argv[sys.argv.index('--cpus') + 1]\n"
        f"if cpus in {sorted(reject_cpus)!r}:\n"
        f"    print({_CPU_CAP_REJECTION!r}); sys.exit(125)\n"
        f"if cpus in {sorted(conflict_cpus or set())!r}:\n"
        f"    print({_NAME_CONFLICT!r}, file=sys.stderr); sys.exit(125)\n"
        f"sys.stdout.write(open({str(reply)!r}).read())\n"
    )
    launch = ProbeLaunch(
        argv=(
            "docker",
            str(script),
            "run",
            "--name",
            "kew-probe-1",
            "--cpus",
            cpus,
            "--cidfile",
            str(tmp_path / "probe.cid"),
        ),
        env=dict(os.environ),
        cwd=tmp_path,
        workspace=tmp_path,
        container_name="kew-probe-1",
        runtime="docker",
    )
    return launch, log


def _launch_count(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_a_contained_cpu_cap_refusal_is_retried_once_at_one_cpu(
    tmp_path: Path,
) -> None:
    """4 CPUs asked, 2 advertised, container confirmed absent: relaunch at 1."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="4", reject_cpus={"4"})
    no_container = subprocess.CompletedProcess([], 0, "", "")
    with (
        patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
        patch("kew.sandbox._daemon_cpu_count", return_value=2),
    ):
        execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 0
    assert execution.reply == "OK"
    assert _launch_count(log) == 2
    assert "--cpus 1 " in log.read_text().splitlines()[1] + " "


def test_a_second_contained_cpu_cap_refusal_is_not_retried(tmp_path: Path) -> None:
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="4", reject_cpus={"4", "1"})
    no_container = subprocess.CompletedProcess([], 0, "", "")
    with (
        patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
        patch("kew.sandbox._daemon_cpu_count", return_value=2),
    ):
        execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 125
    assert _launch_count(log) == 2
    ok, _detail = judge_probe(execution, requested_model=_MODEL)
    assert not ok


def test_a_refusal_inside_the_advertised_range_is_not_retried(
    tmp_path: Path,
) -> None:
    """2 CPUs asked against a maximum of 2 is not the race; do not mask it."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="2", reject_cpus={"2"})
    with patch("kew.sandbox.subprocess_utils.run") as absence:
        execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 125
    assert _launch_count(log) == 1
    absence.assert_not_called()


def test_a_refusal_whose_name_is_held_by_another_container_fails_closed(
    tmp_path: Path,
) -> None:
    """#1946: the launch recorded no container id, so the CPU-cap retry is
    permitted, but the name is held by another container and the daemon refuses
    the retry with its conflict message; that is a static failure, never a
    further retry and never a removal of the other container."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(
        tmp_path, cpus="4", reject_cpus={"4"}, conflict_cpus={"1"}
    )
    present = subprocess.CompletedProcess([], 0, "abc123\n", "")
    with patch("kew.sandbox.subprocess_utils.run", return_value=present) as run:
        execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 125
    assert _launch_count(log) == 2
    assert not any("rm" in call.args[0] for call in run.call_args_list)


def test_a_retry_with_no_budget_left_never_launches(tmp_path: Path) -> None:
    """The refusal consumed the cap: the retry must not reach the provider."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="4", reject_cpus={"4"})
    no_container = subprocess.CompletedProcess([], 0, "", "")
    # deadline, first remaining, second remaining (past the deadline)
    clock = iter([1000.0, 1000.0, 1100.0])
    with (
        patch("kew.readiness_probe.time.monotonic", side_effect=lambda: next(clock)),
        patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
        patch("kew.sandbox._daemon_cpu_count", return_value=2),
    ):
        execution = execute_probe(launch, ClaudeCodeRunner(), timeout_seconds=30)

    assert execution.timed_out
    assert "no time left" in (execution.detail or "")
    assert _launch_count(log) == 1


def test_on_started_fires_for_the_retry_too(tmp_path: Path) -> None:
    """The retry is a new child; its handles must reach the lifecycle owner."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="4", reject_cpus={"4"})
    no_container = subprocess.CompletedProcess([], 0, "", "")
    pids: list[int] = []
    with (
        patch("kew.sandbox.subprocess_utils.run", return_value=no_container),
        patch("kew.sandbox._daemon_cpu_count", return_value=2),
    ):
        execution = execute_probe(
            launch, ClaudeCodeRunner(), on_started=lambda proc: pids.append(proc.pid)
        )

    assert execution.exit_code == 0
    assert _launch_count(log) == 2
    assert len(pids) == 2
    assert pids[0] != pids[1]


def test_an_uncontained_refusal_is_never_retried(tmp_path: Path) -> None:
    from kew.runners.claude_code import ClaudeCodeRunner

    launch, log = _contained_fake_call(tmp_path, cpus="4", reject_cpus={"4"})
    launch = ProbeLaunch(
        argv=launch.argv,
        env=launch.env,
        cwd=launch.cwd,
        workspace=launch.workspace,
    )
    with patch("kew.sandbox.subprocess_utils.run") as absence:
        execution = execute_probe(launch, ClaudeCodeRunner())

    assert execution.exit_code == 125
    assert _launch_count(log) == 1
    absence.assert_not_called()


def test_the_wall_clock_cap_kills_the_call_and_fails_the_probe(
    tmp_path: Path,
) -> None:
    from kew.runners.claude_code import ClaudeCodeRunner

    launch = _fake_call(tmp_path, "import time; time.sleep(30)")

    execution = execute_probe(launch, ClaudeCodeRunner(), timeout_seconds=0.5)

    assert execution.timed_out
    ok, detail = judge_probe(execution, requested_model=_MODEL)
    assert not ok
    assert "cap" in detail


def test_execution_handles_are_recorded_before_the_call_is_waited_on(
    tmp_path: Path,
) -> None:
    """The crash window opens the moment the child exists, so the handles that
    clean a corpse up must be durable before anything blocks on it."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch = _fake_call(tmp_path, "import time; time.sleep(0.3); print('{}')")
    seen: list[bool] = []

    def _on_started(proc: subprocess.Popen[str]) -> None:
        seen.append(proc.poll() is None)
        assert os.getpgid(proc.pid) == proc.pid  # its own group, killable

    execute_probe(launch, ClaudeCodeRunner(), on_started=_on_started)

    assert seen == [True]


def test_a_crash_while_the_call_is_live_does_not_orphan_the_process(
    tmp_path: Path,
) -> None:
    """If anything unwinds while the child is live — here on_started raising —
    execute_probe must kill the call rather than leave a provider process
    running loose to spend against a budget nobody is watching."""
    from kew.runners.claude_code import ClaudeCodeRunner

    launch = _fake_call(tmp_path, "import time; time.sleep(30)")
    seen: list[subprocess.Popen[str]] = []

    def _boom(proc: subprocess.Popen[str]) -> None:
        seen.append(proc)
        raise RuntimeError("attach failed after the child was already live")

    with pytest.raises(RuntimeError, match="attach failed"):
        execute_probe(launch, ClaudeCodeRunner(), on_started=_boom)

    assert len(seen) == 1
    # The finally killed the process group; wait() reaps the corpse without
    # blocking, and a signal death is a non-zero (negative) return code.
    assert seen[0].wait(timeout=5) != 0


# ---------------------------------------------------------------------------
# Task 19 — source and forge access, rechecked every time
# ---------------------------------------------------------------------------


def _gh_results(auth_rc: int, view_rc: int = 0):
    def _run(cmd, **kwargs):
        rc = auth_rc if cmd[:3] == ["gh", "auth", "status"] else view_rc
        return subprocess.CompletedProcess(cmd, rc, "", "")

    return _run


def test_an_unauthenticated_forge_is_reported_before_any_call() -> None:
    with patch("kew.readiness_probe.subprocess_utils.run", _gh_results(1)):
        access = check_supporting_access(_config())

    assert not access.ok
    assert any("forge" in failure for failure in access.failures)


def test_an_unreachable_source_repository_is_reported() -> None:
    with patch("kew.readiness_probe.subprocess_utils.run", _gh_results(0, view_rc=1)):
        access = check_supporting_access(_config())

    assert any("source" in failure for failure in access.failures)


def test_a_linear_source_without_its_key_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    config = _config(tracker=TrackerConfig(type="linear"))

    with patch("kew.readiness_probe.subprocess_utils.run", _gh_results(0)):
        access = check_supporting_access(config)

    assert any("LINEAR_API_KEY" in failure for failure in access.failures)


def test_broken_supporting_access_refuses_before_spending(tmp_path: Path) -> None:
    with (
        patch("kew.readiness_probe.subprocess_utils.run", _gh_results(1)),
        patch("kew.readiness_probe.execute_probe") as never,
        _store(tmp_path) as store,
    ):
        result = confirm_route_live(store, _config(), complexity="complex")

        assert result.status is LiveStatus.REFUSED
        never.assert_not_called()
        assert store.list_readiness_probes() == []


def test_probe_refuses_missing_sidecar_packet_evidence(tmp_path: Path) -> None:
    import kew.readiness as readiness

    packet_type = getattr(readiness, "SidecarPacketEvidence", None)
    assert packet_type is not None

    route = _route()
    sidecar_transport = {
        "mode": "switchyard",
        "requested_model": route.model,
        "model": route.model,
        "config_sha256": "a" * 64,
        "endpoint": None,
        "route": "synthetic/route",
        "managed": False,
        "sidecar": True,
    }
    sidecar_route = ProbeRoute(
        runner=route.runner,
        model=route.model,
        profile=replace(
            route.profile,
            route=replace(
                route.profile.route,
                transport=readiness.ProfileFact(
                    sidecar_transport,
                    readiness.Provenance.ENFORCED,
                    "inference transport resolver",
                ),
            ),
        ),
        sandbox=route.sandbox,
        network=route.network,
    )
    with _store(tmp_path) as store:
        store.lease_readiness_probe(
            probe_id="readiness-probe-cached",
            fingerprint=sidecar_route.fingerprint,
            planned_cost=ProbeCost.planned(PROBE_CEILING_USD, "test"),
        )
        store.settle_readiness_probe(
            "readiness-probe-cached",
            succeeded=True,
            recorded_cost=ProbeCost.observed(0.001, "test"),
            detail="cached success that lacks packet proof",
        )
        store.acknowledge_probe_cleanup("readiness-probe-cached")
        with (
            patch(
                "kew.readiness_probe.resolve_probe_route",
                return_value=sidecar_route,
            ),
            patch("kew.readiness_probe.execute_probe") as paid_probe,
        ):
            result = confirm_route_live(store, _config(), complexity="complex")

    assert result.status is LiveStatus.REFUSED
    assert "packet evidence" in result.detail
    assert not result.from_evidence
    paid_probe.assert_not_called()


def test_sidecar_probe_uses_isolated_worker_after_packet_certification(
    tmp_path: Path, _access_ok
) -> None:
    import contextlib
    from unittest.mock import MagicMock

    from kew.audit import Actor, append_sidecar_packet_certification
    from kew.config import SwitchyardConfig, SwitchyardSidecarConfig
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        RealizedLaunchBoundary,
        SidecarPacketCertification,
        SidecarPacketEvidence,
        compose_realized_profile,
        compose_route_profile,
        evaluate_topology_denial,
    )
    from kew.sandbox import ContainedExecution, SandboxDecision

    worker_image_id = "sha256:" + "a" * 64
    proxy_image_id = "sha256:" + "b" * 64
    route_config = tmp_path / "routes.toml"
    route_config.write_text("[routes]\n", encoding="utf-8")
    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        agent=AgentConfig(model=_MODEL),
        sandbox=SandboxConfig(enabled=True, image="worker:pinned"),
        inference_transport={
            "mode": "switchyard",
            "switchyard": SwitchyardConfig(
                route="synthetic/route",
                config_path=route_config,
                expected_version="0.1.0",
                sidecar=SwitchyardSidecarConfig(
                    enabled=True,
                    image="proxy:pinned",
                    egress_network="approved-egress",
                ),
            ),
        },
    )
    transport = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model=_MODEL,
        model="synthetic/route",
        config_sha256="c" * 64,
        route="synthetic/route",
        api_format="anthropic-messages",
        sidecar=True,
        sidecar_image="proxy:pinned",
        sidecar_egress_network="approved-egress",
        worker_credential_env_name="ANTHROPIC_API_KEY",
    )
    host = _route()
    route = ProbeRoute(
        runner=host.runner,
        model=_MODEL,
        profile=compose_realized_profile(
            run_id="readiness-probe",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code",
                requested_model=_MODEL,
                transport=transport,
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="worker:pinned",
                image_id=worker_image_id,
                auth_path="synthetic:per-attempt",
            ),
        ),
        sandbox=SandboxDecision(
            sandboxed=True,
            runtime="docker",
            posture="sandboxed",
            notice=None,
            image="worker:pinned",
        ),
        network="allowlist",
        transport=transport,
    )
    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )
    supervisor = MagicMock(
        endpoint="http://192.0.2.10:4000",
        config_sha256="d" * 64,
    )
    contained = ContainedExecution(
        argv=("docker", "exec", "1" * 64, "kew-agent-entrypoint"),
        name="sidecar-probe-worker",
        runtime="docker",
        checkout_dir=tmp_path,
        container_id="1" * 64,
        prepared=True,
    )
    with _store(tmp_path) as store:
        append_sidecar_packet_certification(
            store,
            SidecarPacketCertification(
                schema_version=1,
                worker_image_id=worker_image_id,
                proxy_image_id=proxy_image_id,
                packet=packet,
            ),
            actor=Actor("policy", "sidecar-packet-gate"),
        )
        with (
            patch("kew.readiness_probe.resolve_probe_route", return_value=route),
            patch("kew.sandbox.require_image_id", return_value=proxy_image_id),
            patch(
                "kew.orchestrator.sidecar_switchyard_context",
                return_value=contextlib.nullcontext(supervisor),
            ),
            patch(
                "kew.dispatch_sandbox.prepare_sidecar_worker",
                return_value=contained,
            ) as prepare,
            patch(
                "kew.readiness_probe._run_leased_probe",
                return_value=ProbeExecution(
                    exit_code=0,
                    reply="OK",
                    reported_model=_MODEL,
                    reported_cost_usd=0.001,
                ),
            ),
            patch("kew.readiness_probe.build_probe_launch") as ordinary_launch,
        ):
            result = confirm_route_live(store, config, complexity="complex")

    assert result.status is LiveStatus.CONFIRMED
    ordinary_launch.assert_not_called()
    command = prepare.call_args.kwargs["agent_command"]
    assert command[command.index("--max-turns") + 1] == "1"
    assert "--dangerously-skip-permissions" not in command


# ---------------------------------------------------------------------------
# Task 21 — one call, one payer, and no implied validity
# ---------------------------------------------------------------------------


def _sidecar_probe_config(tmp_path: Path) -> DispatchConfig:
    routes = tmp_path / "routes.toml"
    routes.write_text("[routes]\n", encoding="utf-8")
    return DispatchConfig.model_validate(
        {
            "agent": {"model": _MODEL},
            "sandbox": {"enabled": True, "runtime": "docker", "image": "worker:pinned"},
            "inference_transport": {
                "mode": "switchyard",
                "switchyard": {
                    "route": "synthetic/route",
                    "config_path": routes,
                    "expected_version": "0.1.0",
                    "sidecar": {
                        "enabled": True,
                        "image": "proxy:pinned",
                        "egress_network": "approved-egress",
                    },
                },
            },
        }
    )


def _sidecar_probe_route(config: DispatchConfig) -> ProbeRoute:
    from kew.inference_transport import resolve_inference_transport
    from kew.readiness import (
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )
    from kew.runners import get_runner
    from kew.sandbox import SandboxDecision

    runner = get_runner("claude-code")
    transport = resolve_inference_transport(config, runner, _MODEL)
    return ProbeRoute(
        runner=runner,
        model=_MODEL,
        profile=compose_realized_profile(
            run_id="readiness-probe",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model=_MODEL, transport=transport
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="worker:pinned",
                image_id="sha256:" + "a" * 64,
                auth_path="synthetic:per-attempt",
            ),
        ),
        sandbox=SandboxDecision(
            sandboxed=True,
            runtime="docker",
            posture="sandboxed",
            notice=None,
            image="worker:pinned",
        ),
        network="allowlist",
        transport=transport,
    )


@pytest.mark.parametrize("host_credential", [False, True])
def test_sidecar_resolver_uses_production_synthetic_admission(
    tmp_path: Path, host_credential: bool
) -> None:
    from kew.sandbox import resolve_effective_sidecar_sandbox

    config = _sidecar_probe_config(tmp_path)
    environment = (
        {"ANTHROPIC_API_KEY": "synthetic-host-secret"} if host_credential else {}
    )
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("kew.sandbox.detect_runtime_cached", return_value="docker"),
        patch("kew.sandbox.require_image_id", return_value="sha256:" + "a" * 64),
    ):
        production = resolve_effective_sidecar_sandbox(
            config.sandbox,
            runner="claude-code",
            synthetic_credential_env_name="ANTHROPIC_API_KEY",
        )
        try:
            resolved = resolve_probe_route(config, complexity="complex")
        except RuntimeError:
            resolved = None
    assert resolved is not None, (
        "production's synthetic admission must also admit readiness"
    )
    assert resolved.sandbox == production
    assert resolved.profile.auth_path.value == "synthetic:per-attempt"


def _sidecar_probe_supervisor():
    from unittest.mock import MagicMock

    supervisor = MagicMock(
        endpoint="http://192.0.2.10:4000",
        config_sha256="c" * 64,
        worker_name="sc2-worker",
        network_name="sc2-internal",
        owner="d" * 32,
        proxy_image_id="sha256:" + "b" * 64,
    )
    supervisor.create_worker.return_value = "e" * 64
    supervisor.worker_environment.return_value = {
        "ANTHROPIC_API_KEY": "synthetic-client"
    }
    return supervisor


@pytest.mark.parametrize("check", ["prompt", "model"])
def test_prepared_sidecar_probe_uses_mounted_prompt_and_routed_model(
    tmp_path: Path, check: str
) -> None:
    import contextlib

    from kew.dispatch_sandbox import WorkerNetnsFilterEvidence
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        SidecarPacketEvidence,
        evaluate_topology_denial,
    )
    from kew.readiness_probe import _run_leased_sidecar_probe

    config = _sidecar_probe_config(tmp_path)
    route = _sidecar_probe_route(config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}
    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )

    def observe(_store, _route, launch, *, probe_id):
        handoff = launch.workspace / ".kew-prompt.txt"
        captured["prompt"] = handoff.read_text() if handoff.is_file() else None
        captured["argv"] = launch.argv
        return ProbeExecution(exit_code=0, reply="OK")

    with (
        _store(tmp_path) as store,
        patch("kew.readiness_probe.create_probe_workspace", return_value=workspace),
        patch(
            "kew.orchestrator.sidecar_switchyard_context",
            return_value=contextlib.nullcontext(_sidecar_probe_supervisor()),
        ),
        patch(
            "kew.dispatch_sandbox.install_worker_netns_filter",
            return_value=WorkerNetnsFilterEvidence(
                worker_id="e" * 64,
                helper_id="f" * 64,
                image_id="sha256:" + "a" * 64,
                table="kew_sc0_" + "d" * 32,
                readback="{}",
            ),
        ),
        patch("kew.sandbox._daemon_cpu_count", return_value=1),
        patch(
            "kew.dispatch_sandbox._require_sidecar_packet_evidence", return_value=packet
        ),
        patch("kew.readiness_probe._run_leased_probe", side_effect=observe),
    ):
        _run_leased_sidecar_probe(store, config, route, probe_id="readiness-probe-sc2")
    argv = captured["argv"]
    if check == "prompt":
        assert captured["prompt"] == PROBE_PROMPT
        assert "-p" not in argv
    else:
        assert argv[argv.index("--model") + 1] == route.transport.model
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--max-budget-usd") + 1] == str(PROBE_CEILING_USD)
    assert "--dangerously-skip-permissions" not in argv


@pytest.mark.parametrize(
    "call_started,cleanup_passed,reported",
    [
        (False, False, None),
        (True, False, 0.001),
        (True, False, None),
        (False, True, None),
        (True, True, 0.001),
        (True, False, float("nan")),
        (True, False, float("inf")),
        (True, False, -0.001),
    ],
)
def test_sidecar_probe_settlement_preserves_cleanup_and_cost(
    tmp_path: Path, call_started: bool, cleanup_passed: bool, reported: float | None
) -> None:
    import contextlib
    from math import isfinite
    from unittest.mock import MagicMock

    from kew.readiness_probe import _spend_one_call

    config = _sidecar_probe_config(tmp_path)
    route = _sidecar_probe_route(config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    @contextlib.contextmanager
    def lifecycle(*args, **kwargs):
        try:
            yield _sidecar_probe_supervisor()
        finally:
            if not cleanup_passed:
                raise RuntimeError("synthetic exact worker absence was not verified")

    contained = MagicMock(
        argv=("docker", "exec", "e" * 64, "kew-agent-entrypoint"), runtime="docker"
    )
    contained.name = "sc2-worker"
    prepare = (
        {"return_value": contained}
        if call_started
        else {"side_effect": RuntimeError("synthetic filter failure")}
    )
    with (
        _store(tmp_path) as store,
        patch("kew.readiness_probe.create_probe_workspace", return_value=workspace),
        patch("kew.orchestrator.sidecar_switchyard_context", side_effect=lifecycle),
        patch("kew.dispatch_sandbox.prepare_sidecar_worker", **prepare),
        patch(
            "kew.readiness_probe._run_leased_probe",
            return_value=ProbeExecution(
                exit_code=0, reply="OK", reported_cost_usd=reported
            ),
        ) as execute,
    ):
        result = _spend_one_call(store, config, route, now=None)
        stored = store.get_readiness_probe(result.probe_id)
        assert stored is not None
        assert stored.cleanup_acknowledged is cleanup_passed
        assert bool(stranded_probes(store, route.fingerprint)) is not cleanup_passed
        assert stored.state is (
            ProbeState.SUCCEEDED
            if call_started and cleanup_passed
            else ProbeState.FAILED
        )
        assert execute.call_count == int(call_started)
        money = [
            run for run in store.list_all() if run.run_kind == READINESS_PROBE_RUN_KIND
        ]
        assert len(money) == 1
        invalid = reported is not None and (not isfinite(reported) or reported < 0)
        amount = (None if invalid else reported) if call_started else 0.0
        assert result.cost.amount_usd == (0.0 if amount is None else amount)
        assert stored.recorded_cost_usd == (0.0 if amount is None else amount)
        certainty = Provenance.UNAVAILABLE if amount is None else Provenance.MEASURED
        assert result.cost.certainty is certainty
        assert stored.recorded_cost_certainty == certainty.value
        assert money[0].estimated_cost_usd == (
            PROBE_CEILING_USD if amount is None else amount
        )
        if invalid:
            assert "reported cost was invalid" in result.detail
            assert "reported cost was invalid" in stored.detail
        if not call_started:
            assert result.cost.source == "no provider call was made"
        if not cleanup_passed:
            with (
                patch("kew.readiness_probe.resolve_probe_route", return_value=route),
                patch("kew.readiness_probe._sidecar_packet_refusal", return_value=None),
            ):
                forced = confirm_route_live(store, config, force=True)
            assert forced.status is LiveStatus.REFUSED
            assert "cleanup" in forced.detail
            assert execute.call_count == int(call_started)


def _stub_execution(**kwargs: object):
    execution = ProbeExecution(**{"exit_code": 0, "reply": "OK", **kwargs})  # type: ignore[arg-type]
    return patch("kew.readiness_probe.execute_probe", return_value=execution)


@pytest.mark.parametrize("change", ["policy", "names"])
def test_proxy_change_is_attested_beside_probe_identity(monkeypatch, change):
    """Probe identity is the realized profile's fingerprint, which the readiness
    consumer reconstructs from the recorded profile alone. Mixing this launch's
    proxy posture in made every settled probe unfindable and retired the
    readiness_refuted gate (governed review round 4, 2026-09-21). The posture is
    attested beside the identity, and re-confirming after a posture change needs
    the stored probe to carry it (kew issue #1973)."""
    from kew.sandbox import CONTAINER_PROXY_NAMES

    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    if change == "policy":
        monkeypatch.setenv("HTTP_PROXY", "http://user:s3cret@proxy.invalid:3128")
    config = _config()
    config.sandbox.proxy_environment = "none" if change == "policy" else "inherit"
    before = _route(config)
    config.sandbox.proxy_environment = "inherit"
    monkeypatch.setenv("HTTP_PROXY", "http://user:s3cret@proxy.invalid:3128")
    after = _route(config)

    assert before.fingerprint == after.fingerprint == before.profile.fingerprint
    assert before.proxy_material != after.proxy_material
    assert "s3cret" not in json.dumps(after.proxy_material)


def test_proxy_change_reuses_the_live_confirmation_for_its_route(
    tmp_path, monkeypatch, _access_ok
):
    """A confirmed probe answers for its route's realized profile.

    Re-confirming after a proxy posture change would need the stored probe to
    record that posture, so the reader could tell the two apart under one
    identity. Keying identity on the posture instead is what silently retired
    the readiness gate (governed review round 4, 2026-09-21), so this states
    the reuse plainly rather than asserting a property kew does not have."""
    from kew.sandbox import CONTAINER_PROXY_NAMES

    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    config = _config()
    config.sandbox.proxy_environment = "none"
    with _store(tmp_path) as store, _stub_execution(reported_cost_usd=0.004) as call:
        first = confirm_route_live(store, config, complexity="complex")
        assert first.status is LiveStatus.CONFIRMED
        config.sandbox.proxy_environment = "inherit"
        monkeypatch.setenv("HTTP_PROXY", "http://user:s3cret@proxy.invalid:3128")
        second = confirm_route_live(store, config, complexity="complex")

        assert second.status is LiveStatus.CONFIRMED
        assert second.from_evidence
        assert not second.spent
        assert call.call_count == 1
        assert second.fingerprint == first.fingerprint


def test_proxy_fingerprint_snapshots_values_without_plaintext(tmp_path, monkeypatch):
    from dataclasses import fields

    config = _config()
    config.sandbox.proxy_environment = "inherit"
    first_value = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", first_value)
    first = _route(config)
    monkeypatch.setenv("HTTP_PROXY", "http://user:changed@other.invalid:3128")
    changed = _route(config)
    assert changed.proxy_material != first.proxy_material
    monkeypatch.setenv("HTTP_PROXY", first_value)
    same = _route(config)
    assert same.proxy_material == first.proxy_material
    assert same.fingerprint == first.fingerprint == first.profile.fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.inherited_proxy_digest)
    serialized = json.dumps(
        {field.name: getattr(first, field.name) for field in fields(first)}, default=str
    )
    assert first_value not in serialized
    assert "s3cret" not in serialized
    with _store(tmp_path) as store:
        bound = resolve_probe_route(
            config, complexity="complex", store=store, create_key=True
        )
        key = store.readiness_proxy_key()
        # The store salts the attested digest; identity stays the profile's.
        assert bound.proxy_material != first.proxy_material
        assert bound.fingerprint == first.fingerprint
        dump = "\n".join(store._conn.iterdump())
        assert first_value not in dump
        assert "s3cret" not in dump
    with SqliteRunStore(str(tmp_path / "dispatch.db"), read_only=True) as store:
        assert store.readiness_proxy_key() == key
        assert (
            resolve_probe_route(config, complexity="complex", store=store).fingerprint
            == bound.fingerprint
        )
    with SqliteRunStore(str(tmp_path / "other.db"), project_root=tmp_path) as store:
        assert store.readiness_proxy_key() != store.read_ledger_id()
        assert store.readiness_proxy_key() == key
        assert (
            resolve_probe_route(config, complexity="complex", store=store).fingerprint
            == bound.fingerprint
        )


@pytest.mark.parametrize("field", ["reply", "reported_model", "detail"])
@pytest.mark.parametrize("padding", ["", "x" * 65], ids=["whole", "boundary"])
def test_probe_credentials_are_redacted_before_settlement(
    tmp_path, monkeypatch, _access_ok, field, padding
):
    from dataclasses import asdict

    from kew import readiness_probe as probe

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    route = replace(_contained_route(), proxy_environment="inherit")
    launch = ProbeLaunch(
        argv=("docker", "run"),
        env={},
        cwd=tmp_path,
        workspace=tmp_path / "probe",
        container_name="contained-probe",
        runtime="docker",
    )
    execution = ProbeExecution(
        **{
            "exit_code": 0,
            "reply": "OK",
            field: padding + secret,
            "timed_out": field == "detail",
        }
    )
    with (
        _store(tmp_path) as store,
        patch("kew.readiness_probe.resolve_probe_route", return_value=route),
        patch("kew.readiness_probe.build_probe_launch", return_value=launch),
        patch("kew.readiness_probe.execute_probe", return_value=execution),
        patch("kew.readiness_probe._settle", wraps=probe._settle) as settle,
        patch.object(
            store, "settle_readiness_probe", wraps=store.settle_readiness_probe
        ) as persist,
    ):
        result = confirm_route_live(store, _config())
        stored = store.get_readiness_probe(result.probe_id)
        assert stored is not None
        passed = settle.call_args.args[2]
        assert getattr(passed, field) == padding + "<HTTP_PROXY redacted>"
        assert "s3cret" not in json.dumps(asdict(passed))
        expected = padding + "<HTTP_PROXY redacted>"
        if field == "reply":
            expected = f"expected exactly 'OK', got {expected[:80]!r}"
        assert expected in result.detail
        assert persist.call_args.kwargs["detail"] == stored.detail == result.detail
        assert "s3cret" not in json.dumps(asdict(stored), default=str)
        assert "s3cret" not in json.dumps(asdict(result), default=str)


@pytest.mark.parametrize("stage", ["build_probe_launch", "execute_probe"])
def test_probe_exception_details_are_redacted_before_settlement(
    tmp_path, monkeypatch, _access_ok, stage
):
    from kew import readiness_probe as probe

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    with (
        _store(tmp_path) as store,
        patch(f"kew.readiness_probe.{stage}", side_effect=RuntimeError(secret)),
        patch("kew.readiness_probe._settle", wraps=probe._settle) as settle,
    ):
        result = confirm_route_live(store, _config(), complexity="complex")
        assert "s3cret" not in settle.call_args.args[2].detail
        stored = store.get_readiness_probe(result.probe_id)
        assert stored is not None
        assert "s3cret" not in stored.detail
        assert "s3cret" not in result.detail


def test_probe_parser_redacts_reply_and_reported_model(monkeypatch):
    from kew.readiness_probe import _read_probe_output

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    lines = _ok_reply_lines(model=secret)
    lines[0] = lines[0].replace('"OK"', json.dumps("Proxy unavailable: " + secret))
    execution = _read_probe_output(_route().runner, lines, 0)
    assert execution.reply == "Proxy unavailable: <HTTP_PROXY redacted>"
    assert execution.reported_model == "<HTTP_PROXY redacted>"


def test_live_confirmation_redacts_diagnostic_details(monkeypatch):
    from kew.readiness_probe import _refused

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    result = _refused(_route(), "Proxy unavailable: " + secret)
    assert result.detail == "Proxy unavailable: <HTTP_PROXY redacted>"


def test_static_checks_never_imply_live_validity(tmp_path: Path) -> None:
    """Every static fact can resolve and the route is still UNVERIFIED."""
    route = _route()
    assert route.profile.schema_version == 1  # the static profile composed fine

    with _store(tmp_path) as store:
        status, probe = route_live_status(store, route.fingerprint)

    assert status is LiveStatus.UNVERIFIED
    assert probe is None


def test_one_confirmed_call_becomes_reusable_evidence(
    tmp_path: Path, _access_ok
) -> None:
    with _store(tmp_path) as store, _stub_execution(reported_cost_usd=0.004):
        first = confirm_route_live(store, _config(), complexity="complex")
        with patch("kew.readiness_probe.execute_probe") as second_call:
            second = confirm_route_live(store, _config(), complexity="complex")

        assert first.status is LiveStatus.CONFIRMED
        assert first.spent
        second_call.assert_not_called()
        assert second.status is LiveStatus.CONFIRMED
        assert second.from_evidence and not second.spent
        # One call, one spend row, and no customer identity on it.
        spend = [
            run for run in store.list_all() if run.run_kind == READINESS_PROBE_RUN_KIND
        ]
        assert len(spend) == 1
        assert spend[0].issue_num == 0


def test_a_concurrent_caller_launches_at_most_one_provider_call(
    tmp_path: Path, _access_ok
) -> None:
    with _store(tmp_path) as store:
        route = _route()
        store.lease_readiness_probe(
            probe_id="readiness-probe-inflight",
            fingerprint=route.fingerprint,
            planned_cost=ProbeCost.planned(PROBE_CEILING_USD, "test"),
        )
        with patch("kew.readiness_probe.execute_probe") as never:
            result = confirm_route_live(store, _config(), complexity="complex")

    assert result.status is LiveStatus.REFUSED
    never.assert_not_called()


def test_lease_age_alone_does_not_authorize_another_call(
    tmp_path: Path, _access_ok
) -> None:
    """A lease that merely aged out is a CRASH, not a free slot.

    Reconciliation makes it terminal, but the container or process group it may
    have left behind is still unaccounted for — so the next confirm refuses
    until someone actually releases them and says so.
    """
    route = _route()
    started = datetime.now(UTC) - timedelta(hours=2)
    with _store(tmp_path) as store:
        store.lease_readiness_probe(
            probe_id="readiness-probe-crashed",
            fingerprint=route.fingerprint,
            planned_cost=ProbeCost.planned(PROBE_CEILING_USD, "test"),
            now=started,
        )
        store.attach_probe_execution("readiness-probe-crashed", pgid=0)

        with patch("kew.readiness_probe.execute_probe") as never:
            refused = confirm_route_live(store, _config(), complexity="complex")
        assert refused.status is LiveStatus.REFUSED
        assert "cleanup" in refused.detail
        never.assert_not_called()

        crashed = store.get_readiness_probe("readiness-probe-crashed")
        assert crashed is not None
        assert crashed.state is ProbeState.ABANDONED
        assert not crashed.cleanup_acknowledged
        # The crashed attempt still cost the declared ceiling — it did not vanish.
        assert crashed.recorded_cost_usd == pytest.approx(PROBE_CEILING_USD)

        with _stub_execution(reported_cost_usd=0.004):
            allowed = confirm_route_live(
                store, _config(), complexity="complex", reclaim=True
            )
        assert allowed.status is LiveStatus.CONFIRMED
        assert allowed.spent


def test_reclaiming_releases_resources_and_acknowledges_them(
    tmp_path: Path,
) -> None:
    route = _route()
    started = datetime.now(UTC) - timedelta(hours=2)
    with _store(tmp_path) as store:
        store.lease_readiness_probe(
            probe_id="readiness-probe-crashed",
            fingerprint=route.fingerprint,
            planned_cost=ProbeCost.planned(PROBE_CEILING_USD, "test"),
            now=started,
        )
        store.attach_probe_execution(
            "readiness-probe-crashed", container_name="kew-readiness-probe-crashed"
        )
        store.reconcile_readiness_probes()
        assert stranded_probes(store, route.fingerprint)

        with patch("kew.readiness_probe.subprocess_utils.run") as killer:
            reclaimed = reclaim_stranded_probes(
                store, fingerprint=route.fingerprint, runtime="docker"
            )

        assert reclaimed == ["readiness-probe-crashed"]
        assert killer.call_args[0][0] == [
            "docker",
            "kill",
            "kew-readiness-probe-crashed",
        ]
        assert stranded_probes(store, route.fingerprint) == []


def test_a_failed_probe_is_evidence_that_still_costs_what_it_cost(
    tmp_path: Path, _access_ok
) -> None:
    with (
        _store(tmp_path) as store,
        _stub_execution(reply="I cannot do that", reported_cost_usd=0.006),
    ):
        result = confirm_route_live(store, _config(), complexity="complex")

        assert result.status is LiveStatus.REFUTED
        assert result.spent
        probe = store.get_readiness_probe(result.probe_id or "")
        assert probe is not None
        assert probe.state is ProbeState.FAILED
        assert probe.cleanup_acknowledged
        assert probe.recorded_cost_usd == pytest.approx(0.006)


def test_an_unexpected_executor_error_settles_the_lease_at_the_ceiling(
    tmp_path: Path, _access_ok
) -> None:
    """A failure outside the known (OSError/ValueError/SubprocessError) set must
    not leak the lease to the reconciler: it settles in-process at the ceiling,
    the same terminal, acknowledged shape a known failure gets."""
    with (
        _store(tmp_path) as store,
        patch(
            "kew.readiness_probe.execute_probe",
            side_effect=RuntimeError("an unexpected boom mid-call"),
        ),
    ):
        result = confirm_route_live(store, _config(), complexity="complex")

        assert result.status is LiveStatus.REFUTED
        assert result.spent
        probe = store.get_readiness_probe(result.probe_id or "")
        assert probe is not None
        assert probe.state is ProbeState.FAILED
        # Terminalized in-process — acknowledged and off the stranded list —
        # rather than left LEASED for the reconciler to expire.
        assert probe.cleanup_acknowledged
        assert not stranded_probes(store, _route().fingerprint)
        # The cost is honestly unknown (not a measured $0), and the money row
        # bills the declared ceiling rather than vanishing — the I06 fallback.
        assert probe.recorded_cost_certainty == "unavailable"
        money = [
            run for run in store.list_all() if run.run_kind == READINESS_PROBE_RUN_KIND
        ]
        assert len(money) == 1
        assert money[0].estimated_cost_usd == pytest.approx(PROBE_CEILING_USD)


def test_a_newer_failure_supersedes_validity_while_history_is_retained(
    tmp_path: Path, _access_ok
) -> None:
    route = _route()
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    with _store(tmp_path) as store:
        with _stub_execution(reported_cost_usd=0.003):
            confirm_route_live(store, _config(), complexity="complex", now=earlier)
        assert route_live_status(store, route.fingerprint)[0] is LiveStatus.CONFIRMED

        with _stub_execution(reply="timeout", reported_cost_usd=0.003):
            newer = confirm_route_live(
                store, _config(), complexity="complex", force=True
            )

        assert newer.status is LiveStatus.REFUTED
        assert route_live_status(store, route.fingerprint)[0] is LiveStatus.REFUTED
        # Both probes are retained: superseding is not deleting.
        history = store.list_readiness_probes(fingerprint=route.fingerprint)
        assert [probe.state for probe in history] == [
            ProbeState.SUCCEEDED,
            ProbeState.FAILED,
        ]


def test_a_probe_writes_no_customer_run_epic_or_publication_row(
    tmp_path: Path, _access_ok
) -> None:
    with _store(tmp_path) as store, _stub_execution(reported_cost_usd=0.004):
        confirm_route_live(store, _config(), complexity="complex")

        customer = [
            run for run in store.list_all() if run.run_kind != READINESS_PROBE_RUN_KIND
        ]
        assert customer == []


@pytest.mark.policy
def test_no_read_only_surface_can_reach_the_spending_module() -> None:
    """Web, TUI and the service layer must not be able to trigger a charge.

    The resolution half — "which leg would a launch cross?" — spends nothing and
    IS reachable, from ``kew.probe_route`` (#2302). That is the point of the
    split: the dispatch launchpad resolves a route on every read, and this
    module, which can take a lease and make a call, stays behind one door.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kew"
    edge = re.compile(r"^\s*(?:from|import)\s+kew\.readiness_probe\b", re.MULTILINE)
    importers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if edge.search(path.read_text(encoding="utf-8"))
    }

    assert importers == {"cli/doctor.py"}, importers


# ---------------------------------------------------------------------------
# Task 19 — the contained boundary, which is the production one by default
# ---------------------------------------------------------------------------


def _contained_route() -> ProbeRoute:
    from dataclasses import replace

    from kew.sandbox import SandboxDecision

    host = _route()
    contained = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent:latest",
    )
    return replace(host, sandbox=contained, network="allowlist")


def test_a_contained_probe_launches_the_mapped_image_with_a_killable_name() -> None:
    launch = build_probe_launch(_contained_route(), probe_id="readiness-probe-abc")
    try:
        argv = list(launch.argv)

        assert launch.container_name == "kew-readiness-probe-abc"
        assert argv[argv.index("--name") + 1] == launch.container_name
        assert argv[argv.index("-v") + 1] == f"{launch.workspace}:/work"
        assert argv[argv.index("--max-turns") + 1] == "1"
        # The prompt travels as the mounted handoff file, exactly as a dispatch's
        # does — never as argv, which the container never sees.
        assert "-p" not in argv
        assert (launch.workspace / ".kew-prompt.txt").read_text() == PROBE_PROMPT
    finally:
        for path in sorted(launch.workspace.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        launch.workspace.rmdir()


def test_an_unresolvable_image_refuses_the_probe_before_any_charge() -> None:
    """A mutable tag alone is never evidence of what would run."""
    from kew.sandbox import (
        ContainedImageUnresolvedError,
        ImageIdStatus,
        ImageResolution,
        SandboxDecision,
    )

    contained = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent:latest",
    )
    with (
        patch("kew.sandbox.resolve_effective_sandbox", return_value=contained),
        patch(
            "kew.sandbox.inspect_image_id",
            return_value=ImageResolution(ImageIdStatus.ABSENT),
        ),
        pytest.raises(ContainedImageUnresolvedError),
    ):
        resolve_probe_route(_config(), complexity="complex")


def test_a_probe_that_never_starts_is_not_billed_at_the_ceiling(
    tmp_path: Path, _access_ok
) -> None:
    """Only a call that could have reached a provider may cost the ceiling."""
    with (
        _store(tmp_path) as store,
        patch(
            "kew.readiness_probe.build_probe_launch",
            side_effect=OSError("no space for a workspace"),
        ),
        patch("kew.readiness_probe.execute_probe") as never,
    ):
        result = confirm_route_live(store, _config(), complexity="complex")

        assert result.status is LiveStatus.REFUTED
        never.assert_not_called()
        probe = store.get_readiness_probe(result.probe_id or "")
        assert probe is not None
        assert probe.state is ProbeState.FAILED
        assert probe.recorded_cost_usd == 0.0
        assert probe.recorded_cost_certainty == Provenance.MEASURED.value
        # And the lease is closed, so nothing reconciles it at $0.02 later.
        assert store.reconcile_readiness_probes() == []


def test_a_runner_kew_cannot_bound_is_refused_before_it_is_leased(
    tmp_path: Path, _access_ok
) -> None:
    with (
        _store(tmp_path) as store,
        patch(
            "kew.readiness_probe.probe_bound", side_effect=LiveProbeUnsupported("no")
        ),
        patch("kew.readiness_probe.execute_probe") as never,
    ):
        result = confirm_route_live(store, _config(), complexity="complex")

        assert result.status is LiveStatus.REFUSED
        never.assert_not_called()
        assert store.list_readiness_probes() == []


def test_the_probe_refuses_a_gateway_runtime_rather_than_argv_ifying_it() -> None:
    """The probe launches a container command line, so it needs one to exist.

    `_contained_launch` builds the probe's container through
    `sandbox.build_run_argv`; under `runtime = "openshell"` that spelled
    `openshell run --rm --init --pull=never ...` at a CLI with no `run`
    subcommand. The probe resolves its boundary with `gateway_launch=False` so
    the refusal arrives from the same call that already reports an unresolvable
    boundary (PR #2401 review).
    """
    from kew.sandbox import REFUSAL_OPENSHELL_UNAVAILABLE, SandboxRuntimeUnavailable

    config = DispatchConfig.model_validate(
        {
            "agent": {"model": _MODEL},
            "sandbox": {
                "enabled": True,
                "runtime": "openshell",
                "image": "worker:pinned",
                "openshell": {"providers": {"claude-code": "kew-claude-code"}},
            },
        }
    )
    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "host-secret"}, clear=True),
        patch("kew.sandbox.detect_runtime_cached", return_value="openshell"),
        # The egress-certification gate (#2319) runs in front of the launch
        # gate under test; opened so this asserts the launch shape.
        patch(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        ),
        pytest.raises(SandboxRuntimeUnavailable) as refused,
    ):
        resolve_probe_route(config, complexity="complex")
    assert refused.value.reason == REFUSAL_OPENSHELL_UNAVAILABLE
    assert "command line" in str(refused.value)


@pytest.mark.parametrize("recorded", ["cidfile", "prepared", "none"])
def test_the_cap_stops_a_contained_probe_only_by_its_recorded_id(
    tmp_path: Path, recorded: str
) -> None:
    """A contained probe past its cap is killed by the id its launch recorded:
    the cidfile ``docker run`` wrote, or the prepared worker's id. With none
    recorded the container was never started, and nothing is sent (#1963)."""
    from support.fake_docker import LaunchDocker

    from kew.runners.claude_code import ClaudeCodeRunner

    fake = LaunchDocker(tmp_path / "runtime")
    subprocess_utils.TOOL_PATHS["docker"] = str(fake.path)
    name = "kew-readiness-probe-0123456789ab"
    ours = fake.add(name, {}, status="running")
    cidfile = tmp_path / "probe.cid"
    if recorded == "cidfile":
        cidfile.write_text(ours)
    call = _fake_call(tmp_path, "import time; time.sleep(30)")
    launch = replace(
        call,
        argv=(*call.argv, "--cidfile", str(cidfile)),
        container_name=name,
        runtime="docker",
        container_id=ours if recorded == "prepared" else None,
    )

    execution = execute_probe(launch, ClaudeCodeRunner(), timeout_seconds=0.5)

    assert execution.timed_out
    if recorded == "none":
        assert fake.calls("kill") == []
        assert fake.containers()[ours]["status"] == "running"
    else:
        assert fake.calls("kill") == [["kill", ours]]
        assert fake.containers()[ours]["status"] == "exited"
