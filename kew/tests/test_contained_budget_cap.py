"""A contained run honours its per-run cost cap (#1462).

Two halves. The claude container argv carries ``--max-budget-usd`` exactly as
the host argv does, with a runtime override winning over the config default.
And the contained stream stops the container — through its handle, never the
local runtime client — on the same budget and circuit-breaker trip a host run
uses, then records the stop on the run's result and row.

The container is faked at the launch boundary (``dispatch_core._launch_container``)
by a handle that streams usage-bearing stream-json and records stop requests;
publication runs for real against a bare origin, with only ``gh`` faked at the
process boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from support.container_runtime import image_resolves_to
from support.gh_fakes import GhProcessBoundary

from kew import dispatch_core, subprocess_utils
from kew.config import DispatchConfig, SandboxConfig
from kew.dispatch_sandbox import (
    MAX_RECORDED_COST_USD,
    ContainedCostGovernor,
    RunnerBudgetLatch,
    build_agent_container_argv,
    classify_runner_budget_stop,
    config_with_run_cap,
    sandbox_log_fold,
)
from kew.pricing import compute_cost_from_tokens
from kew.run_store import RunRecord, SqliteRunStore
from kew.runners.registry import get_runner
from kew.sandbox import KillStatus
from kew.tracking import DispatchResult
from kew.worktree import write_checkout_generation

_MODEL = "claude-sonnet-4-6"
_TOKENS_PER_TURN = 250_000
_TURNS = 4


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """No runtime resolves the tag in a test; the runtime's image inspection
    gives the fail-closed image gate (#1550 F1) a stable immutable ID."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox.detect_runtime_cached", lambda _cfg: "/usr/bin/docker"
    )
    image_resolves_to(monkeypatch, "sha256:" + "f" * 64)


def _turn_cost() -> float:
    return compute_cost_from_tokens(
        model=_MODEL, input_tokens=_TOKENS_PER_TURN, output_tokens=0
    )


def _turn() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": _MODEL,
                "content": [{"type": "text", "text": "working"}],
                "usage": {"input_tokens": _TOKENS_PER_TURN, "output_tokens": 0},
            },
        }
    )


def _result(
    turns: int = _TURNS, *, subtype: str = "success", cost: float | None = None
) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "success",
            "total_cost_usd": _turn_cost() * turns if cost is None else cost,
            "usage": {"input_tokens": _TOKENS_PER_TURN * turns, "output_tokens": 0},
        }
    )


_INIT = json.dumps({"type": "system", "subtype": "init", "model": _MODEL})
_COMPACTION = json.dumps(
    {
        "type": "system",
        "subtype": "compact_boundary",
        "compact_metadata": {"trigger": "auto"},
    }
)


def _stream() -> list[str]:
    """Usage-bearing claude stream-json turns, then a terminal result."""
    return [_INIT, *[_turn()] * _TURNS, _result()]


class _FakeContainer:
    """A container handle: streams its lines until a kill is accepted.

    ``kill_answers`` is what the runtime says to each kill request, in order
    (accepted once they run out); ``exit_after_kill`` is the exit a killed
    container reports — a wrapper that swallows the signal can still say 0.
    """

    def __init__(
        self,
        lines: list[str],
        exit_code: int,
        *,
        kill_answers: tuple[KillStatus, ...] = (),
        exit_after_kill: int = 137,
    ) -> None:
        self._lines = lines
        self._exit_code = exit_code
        self._kill_answers = list(kill_answers)
        self._exit_after_kill = exit_after_kill
        self.kills = 0
        self.accepted = False
        self.lines_read = 0

    def __iter__(self):
        for line in self._lines:
            if self.accepted:
                return
            self.lines_read += 1
            yield line + "\n"

    def kill(self) -> KillStatus:
        self.kills += 1
        status = (
            self._kill_answers.pop(0) if self._kill_answers else KillStatus.ACCEPTED
        )
        self.accepted = status is KillStatus.ACCEPTED
        return status

    def wait(self) -> int:
        return self._exit_after_kill if self.kills else self._exit_code


def _dispatch(
    tmp_path,
    store,
    monkeypatch,
    governor,
    *,
    exit_code=1,
    lines=None,
    container=None,
    checkout=None,
    config=None,
    publication_base_sha="",
    checkout_generation=None,
    runner="claude-code",
):
    from kew import run_credentials

    monkeypatch.setattr(
        run_credentials, "direct_openrouter_route", lambda *a, **k: False
    )
    # The contained launch authenticates with an env credential, by name.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-oauth-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    if checkout is None:
        checkout = tmp_path / "checkout"
        checkout.mkdir(exist_ok=True)
    config = config or DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    if container is None:
        container = _FakeContainer(lines or _stream(), exit_code)
    launches: list[object] = []

    def launch(*args, **kwargs):
        launches.append(args)
        return container

    with (
        patch.object(
            dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
        ),
        patch.object(dispatch_core, "_launch_container", side_effect=launch),
    ):
        result = dispatch_core._run_sandboxed(
            prompt="do the thing",
            checkout_dir=checkout,
            config=config,
            image=SandboxConfig().image,
            runner=runner,
            issue_num=11,
            item_id="11",
            branch="agent/issue-11",
            title="t",
            model=_MODEL,
            store=store,
            cost_governor=governor,
            publication_base_sha=publication_base_sha,
            checkout_generation=checkout_generation,
        )
    [row] = store.list_by_issue(11)
    return result, row, container, launches


def _seed_bucket(store: SqliteRunStore, cost: float) -> None:
    """Five normally-completed dispatches: an established breaker bucket."""
    for i in range(5):
        store.save(
            RunRecord(
                run_id=f"history-{i}",
                issue_num=100 + i,
                branch=f"agent/issue-{100 + i}",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                log_file=Path("/dev/null"),
                model=_MODEL,
                complexity="medium",
                estimated_cost_usd=cost,
            )
        )


def _governor_for(store: SqliteRunStore, config: DispatchConfig):
    ctx = SimpleNamespace(
        config=config, store=store, complexity="medium", run_kind="dispatch"
    )
    early = SimpleNamespace(runner="claude-code", model=_MODEL)
    return dispatch_core._contained_cost_governor(ctx, early)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, test-local repository
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "kew tests",
            "GIT_AUTHOR_EMAIL": "tests@kew.invalid",
            "GIT_COMMITTER_NAME": "kew tests",
            "GIT_COMMITTER_EMAIL": "tests@kew.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    ).stdout


@pytest.fixture
def committed_checkout(tmp_path: Path, monkeypatch) -> Path:
    """A contained clone whose agent COMMITTED its work: a clean tree one
    commit ahead of ``origin/main``, the shape a contained agent leaves."""
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet", "-b", "agent/issue-11")
    (checkout / "README.md").write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "--quiet", "-m", "base")
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    (checkout / "fix.py").write_text("print('work')\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "--quiet", "-m", "the agent's work")
    return checkout


_PR_URL = "https://x.invalid/pull/7"
_PR_CREATE = ["gh", "api", "--method", "POST", "repos/o/r/pulls"]


class _Forge:
    """GitHub behind ``gh``: the REST create-PR answers with a PR, every other
    argv (labels, the report comment) with an empty success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        stdout = "[]"
        if cmd[:5] == _PR_CREATE:
            stdout = json.dumps({"number": 7, "html_url": _PR_URL})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def created_prs(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if cmd[:5] == _PR_CREATE]

    def labels_added(self, issue: int) -> list[str]:
        """Every label a ``gh issue edit`` on *issue* added, in order."""
        added: list[str] = []
        for cmd in self.calls:
            if cmd[:4] == ["gh", "issue", "edit", str(issue)]:
                added += [
                    value
                    for flag, value in zip(cmd, cmd[1:], strict=False)
                    if flag == "--add-label"
                ]
        return added

    def comments(self, issue: int) -> list[str]:
        """The body of every ``gh issue comment`` on *issue*."""
        return [
            cmd[cmd.index("--body") + 1]
            for cmd in self.calls
            if cmd[:4] == ["gh", "issue", "comment", str(issue)]
        ]


@dataclass
class _Project:
    """A real bare origin, the operator's clone and the run's checkout holding
    the agent's edit, with only the forge behind ``gh`` faked."""

    checkout: Path
    base: str
    origin: Path
    forge: _Forge

    @staticmethod
    def config() -> DispatchConfig:
        config = DispatchConfig()
        config.project.repo = "o/r"
        # Checkout publication pushes with git alone; the canonical default
        # extracts the tree in a container (#1218 admits it with this posture).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        return config

    def remote_branches(self) -> list[str]:
        refs = _git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads")
        return [ref for ref in refs.splitlines() if ref != "refs/heads/main"]


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> _Project:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)
    origin = tmp_path / "origin.git"
    operator = tmp_path / "operator"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(operator))
    (operator / "widget.py").write_text("def render(x):\n    raise ValueError\n")
    _git(operator, "add", ".")
    _git(operator, "commit", "-qm", "base")
    _git(operator, "push", "-q", "origin", "main")
    base = _git(operator, "rev-parse", "HEAD").strip()
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "checkout", "-q", "-b", "agent/issue-11")
    (checkout / "widget.py").write_text("def render(x):\n    return None\n")
    monkeypatch.setenv("KEW_PROJECT", str(operator))
    monkeypatch.delenv("KEW_DETACHED_PUBLICATION_TOKEN", raising=False)
    return _Project(checkout, base, origin, _forge_behind_gh(monkeypatch))


def _forge_behind_gh(monkeypatch) -> _Forge:
    """Install a :class:`_Forge` at the ``gh`` process boundary."""
    forge = _Forge()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    return forge


# --------------------------------------------------------------------------
# The argv half.
# --------------------------------------------------------------------------


def test_contained_claude_args_carry_the_run_cap() -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = 7.5
    runner = get_runner("claude-code")

    args = runner.container_args(config=config, model=_MODEL)
    host = runner.build_command(prompt="p", config=config, env={}, model=_MODEL)

    cap = args.index("--max-budget-usd")
    assert args[cap + 1] == "7.5"
    # The same position as the host argv: after the agent flags, before --model.
    assert args[: cap + 2] == host[1 : cap + 3]
    assert args.index("--model") == cap + 2


def test_runtime_override_cap_reaches_the_container_args(tmp_path: Path) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = 7.5

    argv = build_agent_container_argv(
        config_with_run_cap(config, 40.0),
        image="kew-agent:test",
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="11",
        model=_MODEL,
        inference_transport=None,
    )

    cap = argv.index("--max-budget-usd")
    assert argv[cap + 1] == "40.0"
    assert "7.5" not in argv
    # The caller's config keeps its own default.
    assert config.agent.max_budget_usd == 7.5
    assert config_with_run_cap(config, None) is config


# --------------------------------------------------------------------------
# The kill half.
# --------------------------------------------------------------------------


def test_contained_run_past_its_cap_is_stopped_and_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    cap = _turn_cost() * 1.5
    config = DispatchConfig()
    config.agent.max_budget_usd = cap
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        result, row, container, launches = _dispatch(
            tmp_path, store, monkeypatch, governor
        )
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert container.kills == 1
    assert result.cost_stop_kill == "accepted"
    # The audit trail says what the runtime answered the stop.
    assert stopped.payload["error_category"] == "budget_stop"
    assert "kill accepted" in stopped.payload["reason"]
    assert len(launches) == 1  # no retry started
    # Stopped at the second turn: init + two turns were read, no more.
    assert container.lines_read == 3
    assert result.budget_exceeded is True
    # The host's graceful budget stop (#859): the checkout is kept, the item
    # fails, and the row says so, as ``_classify_attempt`` records a host stop.
    assert result.error_category == "budget_stop"
    assert result.estimated_cost_usd == pytest.approx(_turn_cost() * 2)
    assert row.status == "budget_stop"
    assert row.error_category == "budget_stop"
    assert row.estimated_cost_usd == pytest.approx(_turn_cost() * 2)
    assert row.input_tokens == _TOKENS_PER_TURN * 2


def test_contained_run_past_the_breaker_threshold_is_stopped_as_cost_anomaly(
    tmp_path: Path, monkeypatch
) -> None:
    config = DispatchConfig()
    config.breaker.compaction_allowance_factor = 0.0
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        # A median of 0.9 turns, factor 3: the third turn crosses the
        # threshold, far below the default $25 run cap.
        _seed_bucket(store, _turn_cost() * 0.9)
        governor = _governor_for(store, config)
        assert governor.anomaly_threshold_usd == pytest.approx(_turn_cost() * 2.7)
        result, row, container, launches = _dispatch(
            tmp_path, store, monkeypatch, governor
        )
        [breaker] = store.list_audit_events(event="circuit_breaker")

    assert container.kills == 1
    assert len(launches) == 1
    assert result.budget_exceeded is False
    assert result.error_category == "circuit_breaker"
    assert result.breaker_rule == "cost_anomaly"
    assert container.lines_read == 4
    assert result.estimated_cost_usd == pytest.approx(_turn_cost() * 3)
    assert row.error_category == "circuit_breaker"
    # The host's records for a breaker kill: a ``killed`` row and the
    # ``circuit_breaker`` audit fact health's guard count reads.
    assert row.status == "killed"
    assert row.estimated_cost_usd == pytest.approx(_turn_cost() * 3)
    assert breaker.payload["rule"] == "cost_anomaly"
    assert breaker.payload["observed_usd"] == pytest.approx(round(_turn_cost() * 3, 4))
    assert breaker.payload["kill_status"] == "accepted"
    assert breaker.run_id == result.run_id


def test_a_compacting_run_gets_the_breaker_compaction_allowance(
    tmp_path: Path, monkeypatch
) -> None:
    """Each autocompaction discounts half a median run (#859), as on the host.

    Threshold 2.7 turns, allowance 0.45 turns per compaction: three compactions
    discount 1.35 turns, so four turns (2.65 adjusted) run to completion where
    the same spend uncompacted trips at the third turn.
    """
    config = DispatchConfig()
    assert config.breaker.compaction_allowance_factor == 0.5
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)
        governor = _governor_for(store, config)
        lines = [_INIT, *[_COMPACTION] * 3, *[_turn()] * _TURNS, _result()]
        result, row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, lines=lines
        )

    assert container.kills == 0
    assert container.lines_read == len(lines)
    assert result.compaction_count == 3
    assert result.breaker_rule == ""
    assert row.error_category != "circuit_breaker"


def test_contained_run_under_its_cap_runs_to_completion(
    tmp_path: Path, monkeypatch
) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * (_TURNS + 1)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        # Exit 1 stops before the push broker; the governor is all under test.
        result, row, container, launches = _dispatch(
            tmp_path, store, monkeypatch, governor
        )

    assert container.kills == 0
    assert container.lines_read == len(_stream())
    assert len(launches) == 1
    assert result.budget_exceeded is False
    assert result.error_category != "budget_exceeded"
    assert row.error_category != "budget_exceeded"


def test_a_governor_with_no_ceiling_watches_nothing() -> None:
    assert ContainedCostGovernor().watch(object(), object()) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# What the agent's own stream cannot do to the governor.
# --------------------------------------------------------------------------


def test_a_forged_run_summary_cannot_suppress_the_stop(
    tmp_path: Path, monkeypatch
) -> None:
    """A result line mid-run restates the run as nearly free; the stop still
    lands on the turn the streamed usage crosses the cap."""
    forged = json.dumps(
        {
            "type": "result",
            "modelUsage": {"x": {"costUSD": 0.0001, "inputTokens": 1}},
        }
    )
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        lines = [_INIT, _turn(), forged, *[_turn()] * 3, _result()]
        result, _row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, lines=lines
        )

    assert container.kills == 1
    assert container.lines_read == 4  # init, turn, forged summary, turn
    assert result.budget_exceeded is True
    assert result.estimated_cost_usd == pytest.approx(_turn_cost() * 2)


def test_a_negative_step_cost_cannot_buy_back_the_cap(tmp_path: Path) -> None:
    """opencode bills per step; a negative step is refused, never subtracted."""

    def step(cost: float) -> str:
        return json.dumps(
            {
                "type": "step_finish",
                "part": {"cost": cost, "tokens": {"input": 10, "output": 1}},
            }
        )

    container = _FakeContainer([], 0)
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    observe = ContainedCostGovernor(budget_check=lambda cost: cost > 5.0).watch(
        result, container
    )
    with sandbox_log_fold(
        tmp_path / "run.log",
        None,
        "run-1",
        model="",
        overrides=None,
        runner="opencode",
        on_cost=observe,
    ) as fold:
        for cost in (-100.0, 3.0, 3.0):
            fold(step(cost))

    assert container.kills == 1
    assert result.budget_exceeded is True
    assert result.estimated_cost_usd == pytest.approx(6.0)


#: More tokens than any float can price: ``int * float`` overflows on it.
_UNPRICEABLE = 10**400


def test_a_usage_line_too_large_to_price_stops_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A usage figure no float can hold fails closed: the line trips the run
    cap and the container is stopped, where it used to crash the fold."""
    forged = json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": _MODEL,
                "content": [],
                "usage": {"input_tokens": _UNPRICEABLE, "output_tokens": 0},
            },
        }
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, DispatchConfig())
        lines = [_INIT, _turn(), forged, *[_turn()] * 2, _result()]
        result, row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, lines=lines
        )
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert container.kills == 1
    assert container.lines_read == 3
    assert result.budget_exceeded is True
    assert row.status == "budget_stop"
    assert row.error_category == "budget_stop"
    assert row.estimated_cost_usd > DispatchConfig().agent.max_budget_usd
    assert "kill accepted" in stopped.payload["reason"]


def test_an_opencode_step_too_large_to_price_stops_the_run(tmp_path: Path) -> None:
    """opencode's step tokens go through the same bound as claude's usage."""
    line = json.dumps(
        {
            "type": "step_finish",
            "part": {"tokens": {"input": _UNPRICEABLE, "output": 0}},
        }
    )
    container = _FakeContainer([], 0)
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    observe = ContainedCostGovernor(budget_check=lambda cost: cost > 25.0).watch(
        result, container
    )
    with sandbox_log_fold(
        tmp_path / "run.log",
        None,
        "run-1",
        model=_MODEL,
        overrides=None,
        runner="opencode",
        on_cost=observe,
    ) as fold:
        fold(line)

    assert container.kills == 1
    assert result.budget_exceeded is True


def _step(cost: float) -> str:
    """An opencode step that bills *cost* natively."""
    return json.dumps(
        {
            "type": "step_finish",
            "part": {"cost": cost, "tokens": {"input": 10, "output": 1}},
        }
    )


_HUGE_USAGE = json.dumps(
    {
        "type": "assistant",
        "message": {
            "model": _MODEL,
            "content": [],
            "usage": {"input_tokens": 10**15, "output_tokens": 0},
        },
    }
)


@pytest.mark.parametrize(
    ("runner", "lines", "overrides"),
    [
        # Two finite native step costs whose sum is infinite.
        ("opencode", [_step(1e308), _step(1e308)], {}),
        # A usage line whose price overflows to infinity at the configured rate.
        ("claude-code", [_INIT, _HUGE_USAGE], {_MODEL: [1e300, 1e300]}),
    ],
    ids=["native-step-costs", "overflowing-token-price"],
)
def test_an_absurd_cost_stops_the_run_and_audits_as_strict_json(
    tmp_path: Path, monkeypatch, runner: str, lines: list[str], overrides: dict
) -> None:
    """The run is still stopped, and the breaker fact, the stop reason and the
    row carry a bounded, finite cost that strict JSON readers (the console,
    jq) accept, never ``Infinity``."""
    config = DispatchConfig()
    config.pricing.overrides = overrides
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)
        # Anomaly rule only, so the circuit breaker is what stops the run.
        governor = replace(
            _without_backoff(_governor_for(store, config)), budget_check=None
        )
        container = _FakeContainer(lines, 1, kill_answers=(KillStatus.REFUSED,))
        result, row, _, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            governor,
            container=container,
            config=config,
            runner=runner,
        )
        [breaker] = store.list_audit_events(event="circuit_breaker")
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert container.kills >= 1
    assert result.breaker_rule == "cost_anomaly"
    assert row.status == "killed"
    payload = json.loads(json.dumps(breaker.payload, allow_nan=False))
    assert 0 < payload["observed_usd"] <= MAX_RECORDED_COST_USD
    assert 0 < row.estimated_cost_usd <= MAX_RECORDED_COST_USD
    json.loads(json.dumps(stopped.payload, allow_nan=False))
    assert "inf" not in stopped.payload["reason"]
    assert f"${MAX_RECORDED_COST_USD:.2f}" in stopped.payload["reason"]


def test_forged_compaction_lines_cannot_switch_off_the_anomaly_stop(
    tmp_path: Path, monkeypatch
) -> None:
    """Fifty compaction markers would discount 22.5 median-ish turns; only a
    bounded number count, so a run well past the threshold is still stopped."""
    config = DispatchConfig()
    assert config.breaker.compaction_allowance_factor == 0.5
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)  # threshold: 2.7 turns
        governor = _governor_for(store, config)
        lines = [_INIT, *[_COMPACTION] * 50, *[_turn()] * 8, _result(turns=8)]
        result, row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, lines=lines
        )
        breakers = store.list_audit_events(event="circuit_breaker")

    assert container.kills == 1
    assert result.breaker_rule == "cost_anomaly"
    assert row.status == "killed"
    assert [event.payload["rule"] for event in breakers] == ["cost_anomaly"]


def test_a_forged_summary_after_the_stop_cannot_lower_the_recorded_cost(
    tmp_path: Path, monkeypatch
) -> None:
    """After the trip the agent writes a result line restating the run as
    nearly free; the stopped run's row keeps the cost it was stopped at."""
    forged = json.dumps(
        {"type": "result", "modelUsage": {"x": {"costUSD": 0.0001, "inputTokens": 1}}}
    )
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _FakeContainer(
        [_INIT, _turn(), _turn(), forged], 1, kill_answers=(KillStatus.REFUSED,)
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _without_backoff(_governor_for(store, config))
        result, row, _, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, container=container
        )

    assert container.lines_read == 4  # the forged summary was read
    assert result.budget_exceeded is True
    assert row.status == "budget_stop"
    assert row.estimated_cost_usd == pytest.approx(_turn_cost() * 2)


class _StreamBreaksAfter(_FakeContainer):
    """A container whose agent acknowledged startup and streamed its lines,
    then whose pipe failed mid-run."""

    def observe_workload_start(self) -> bool:
        return True

    def __iter__(self):
        yield from super().__iter__()
        raise OSError("container stream broke")


def test_a_stream_that_fails_after_the_run_cap_trip_keeps_the_stop(
    tmp_path: Path, monkeypatch
) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _StreamBreaksAfter(
        [_INIT, _turn(), _turn(), _turn()], 1, kill_answers=(KillStatus.REFUSED,) * 9
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _without_backoff(_governor_for(store, config))
        with pytest.raises(OSError, match="stream broke"):
            _dispatch(tmp_path, store, monkeypatch, governor, container=container)
        [row] = store.list_by_issue(11)
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert container.kills >= 1
    assert row.status == "budget_stop"
    assert row.error_category == "budget_stop"
    # Every turn the refused kill let through is on the row.
    assert row.estimated_cost_usd == pytest.approx(_turn_cost() * 3)
    assert "budget_stop" in stopped.payload["reason"]
    assert "kill refused" in stopped.payload["reason"]


def test_a_stream_that_fails_after_the_anomaly_trip_keeps_the_breaker_fact(
    tmp_path: Path, monkeypatch
) -> None:
    config = DispatchConfig()
    config.breaker.compaction_allowance_factor = 0.0
    container = _StreamBreaksAfter(
        [_INIT, *[_turn()] * 4], 1, kill_answers=(KillStatus.REFUSED,) * 9
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)  # threshold: 2.7 turns
        governor = _without_backoff(_governor_for(store, config))
        with pytest.raises(OSError, match="stream broke"):
            _dispatch(tmp_path, store, monkeypatch, governor, container=container)
        [row] = store.list_by_issue(11)
        [breaker] = store.list_audit_events(event="circuit_breaker")

    assert row.status == "killed"
    assert row.error_category == "circuit_breaker"
    assert breaker.payload["rule"] == "cost_anomaly"
    assert breaker.payload["kill_status"] == "refused"
    assert breaker.payload["observed_usd"] == pytest.approx(round(_turn_cost() * 3, 4))


def test_a_finished_run_is_not_stopped_by_its_own_result_line(
    tmp_path: Path, monkeypatch
) -> None:
    """The run's closing summary bills more than the streamed estimate; the run
    has already ended, so (as on the host) the summary trips nothing."""
    config = DispatchConfig()
    config.breaker.compaction_allowance_factor = 0.0
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)  # threshold: 2.7 turns
        governor = _governor_for(store, config)
        lines = [_INIT, _turn(), _turn(), _result(turns=2, cost=_turn_cost() * 5)]
        result, row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, lines=lines
        )

    assert container.kills == 0
    assert result.breaker_rule == ""
    assert result.error_category != "circuit_breaker"
    assert row.status != "killed"


def test_a_line_that_ends_the_run_does_not_trip_it(tmp_path: Path) -> None:
    """codex reports a turn's usage on the same ``turn.completed`` line that
    ends the run: that usage counts, but a finished run is not stopped."""
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": _TOKENS_PER_TURN, "output_tokens": 0},
        }
    )
    container = _FakeContainer([], 0)
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    observe = ContainedCostGovernor(
        budget_check=lambda cost: cost > _turn_cost() * 0.5
    ).watch(result, container)
    with sandbox_log_fold(
        tmp_path / "run.log",
        None,
        "run-1",
        model=_MODEL,
        overrides=None,
        runner="codex",
        on_cost=observe,
    ) as fold:
        fold(line)

    assert container.kills == 0
    assert result.budget_exceeded is False


def test_spending_after_a_terminal_event_is_judged_and_stops_the_run(
    tmp_path: Path,
) -> None:
    """#1798, contained: only the line that ends the run is spared. A turn
    after a result line proves the run had not finished, so its crossing stops
    the container as any other crossing does."""
    container = _FakeContainer([], 0)
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    observe = ContainedCostGovernor(anomaly_threshold_usd=_turn_cost() * 0.5).watch(
        result, container
    )
    with sandbox_log_fold(
        tmp_path / "run.log",
        None,
        "run-1",
        model=_MODEL,
        overrides=None,
        runner="claude-code",
        on_cost=observe,
    ) as fold:
        fold(_result(turns=0, cost=0.0))
        fold(_turn())

    assert container.kills == 1
    assert result.breaker_rule == "cost_anomaly"
    assert result.error_category == "circuit_breaker"
    assert result.breaker_stopped is True


def _codex_turn_completed() -> str:
    return json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": _TOKENS_PER_TURN, "output_tokens": 0},
        }
    )


def _fold_lines(
    tmp_path: Path, runner: str, lines: list[str], threshold_usd: float
) -> tuple[_FakeContainer, DispatchResult]:
    """Fold *lines* through the contained stream with the anomaly bar armed."""
    container = _FakeContainer([], 0)
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    observe = ContainedCostGovernor(anomaly_threshold_usd=threshold_usd).watch(
        result, container
    )
    with sandbox_log_fold(
        tmp_path / "run.log",
        None,
        "run-1",
        model=_MODEL,
        overrides=None,
        runner=runner,
        on_cost=observe,
    ) as fold:
        for line in lines:
            fold(line)
    return container, result


def test_every_closing_line_is_the_end_of_the_run(tmp_path: Path) -> None:
    """codex reports usage only on ``turn.completed``, the line that ends the
    run. A repeated one is that closing line again, so a crossing on it does
    not stop the container, whatever came before."""
    container, result = _fold_lines(
        tmp_path,
        "codex",
        [_codex_turn_completed(), _codex_turn_completed()],
        _turn_cost() * 1.5,
    )

    assert container.kills == 0
    assert result.error_category != "circuit_breaker"


def _pi_message_end(stop_reason: str, input_tokens: int) -> str:
    message = {
        "role": "assistant",
        "model": _MODEL,
        "content": [{"type": "text", "text": "x"}],
        "stopReason": stop_reason,
        "usage": {"input": input_tokens, "output": 0},
    }
    return json.dumps({"type": "message_end", "message": message})


def test_an_earlier_terminal_event_does_not_cost_a_finished_run_its_work(
    tmp_path: Path,
) -> None:
    """A pi run hit a provider error mid-run (a terminal ``stopReason``), then
    carried on and finished; its last line crosses the bar. That last line
    ends the run, so the container is not stopped."""
    lines = [
        _pi_message_end("toolUse", 100_000),
        _pi_message_end("error", 0),
        _pi_message_end("toolUse", 100_000),
        _pi_message_end("stop", 1_000_000),
    ]

    container, result = _fold_lines(tmp_path, "pi", lines, _turn_cost() * 2.4)

    assert container.kills == 0
    assert result.error_category != "circuit_breaker"


def test_a_forged_early_result_does_not_spare_the_rest_of_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A result line early in the stream claimed the run was over and nearly
    free, then 20 more turns followed. The crossing on the third turn stops
    the container where it would have stopped without the forged line, and
    the row records the cost at that stop."""
    config = DispatchConfig()
    config.breaker.compaction_allowance_factor = 0.0
    forged = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.0001,
            "usage": {"input_tokens": 1},
        }
    )
    lines = [_INIT, _turn(), forged, *[_turn()] * 20, _result(turns=21)]
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_bucket(store, _turn_cost() * 0.9)  # $0.675 median: $2.025 ceiling
        governor = _governor_for(store, config)
        result, row, container, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, exit_code=0, lines=lines
        )

    assert container.kills == 1
    assert result.error_category == "circuit_breaker"
    assert row.status == "killed"
    assert row.estimated_cost_usd == pytest.approx(_turn_cost() * 3)


# --------------------------------------------------------------------------
# A kill the runtime did not take.
# --------------------------------------------------------------------------


def _without_backoff(governor: ContainedCostGovernor) -> ContainedCostGovernor:
    return replace(governor, kill_retry_seconds=0.0)


def test_a_refused_kill_is_retried_until_the_runtime_accepts_it(
    tmp_path: Path, monkeypatch
) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _FakeContainer(
        _stream(), 1, kill_answers=(KillStatus.REFUSED, KillStatus.UNKNOWN)
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _without_backoff(_governor_for(store, config))
        result, _row, _, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, container=container
        )

    # Refused at turn two, unknown at turn three, accepted at turn four.
    assert container.kills == 3
    assert container.lines_read == 5
    assert result.cost_stop_kill == "accepted"
    assert result.budget_exceeded is True


def test_a_kill_never_accepted_is_recorded_as_such(tmp_path: Path, monkeypatch) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _FakeContainer(_stream(), 1, kill_answers=(KillStatus.REFUSED,) * 9)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _without_backoff(_governor_for(store, config))
        result, row, _, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, container=container
        )

    # One request per line from the trip to the stream's end.
    assert container.kills == len(_stream()) - 2
    assert result.cost_stop_kill == "refused"
    assert row.status == "budget_stop"


def test_kill_retries_are_spaced_out(tmp_path: Path, monkeypatch) -> None:
    """A stream of lines arriving together asks the runtime once, not per line."""
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _FakeContainer(_stream(), 1, kill_answers=(KillStatus.REFUSED,) * 9)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = replace(_governor_for(store, config), kill_retry_seconds=3600.0)
        result, _row, _, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, container=container
        )

    assert container.kills == 1
    assert result.cost_stop_kill == "refused"


def test_a_container_another_path_already_killed_counts_as_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    """A handle whose kill another path (a cap breach, a cancel) already landed
    refuses a second one; that refusal is not a container still running."""

    class _AlreadyKilled(_FakeContainer):
        kill_accepted = True

    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _AlreadyKilled(_stream(), 1, kill_answers=(KillStatus.REFUSED,) * 9)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _without_backoff(_governor_for(store, config))
        result, _row, _, _ = _dispatch(
            tmp_path, store, monkeypatch, governor, container=container
        )

    assert container.kills == 0
    assert result.cost_stop_kill == "accepted"


# --------------------------------------------------------------------------
# A cost stop never publishes; an under-cap run still does.
# --------------------------------------------------------------------------


def test_a_cost_stopped_run_that_exits_zero_is_not_published(
    tmp_path: Path, monkeypatch, project: _Project
) -> None:
    config = project.config()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    container = _FakeContainer(_stream(), 0, exit_after_kill=0)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        result, row, _, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            governor,
            container=container,
            checkout=project.checkout,
            config=config,
            publication_base_sha=project.base,
        )

    assert result.exit_code == 0
    assert project.forge.created_prs() == []
    assert project.remote_branches() == []
    assert result.published_pr_url == ""
    assert row.status == "budget_stop"


def test_a_run_that_stopped_at_its_own_budget_flag_is_not_published(
    tmp_path: Path, monkeypatch, project: _Project
) -> None:
    """No governor armed (the reviewer launcher, a remote placement): claude's
    own ``--max-budget-usd`` stop exits 0 and must still read as a cost stop."""
    config = project.config()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    lines = [_INIT, _turn(), _turn(), _result(turns=2, subtype="error_max_budget_usd")]
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        result, row, container, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            None,
            exit_code=0,
            lines=lines,
            checkout=project.checkout,
            config=config,
            publication_base_sha=project.base,
        )

    assert container.kills == 0
    assert project.forge.created_prs() == []
    assert project.remote_branches() == []
    assert result.error_category == "budget_stop"
    assert row.status == "budget_stop"


def test_a_stop_at_the_runners_own_cap_records_the_cost_it_reported(
    tmp_path: Path, monkeypatch, project: _Project
) -> None:
    """The audited reason of claude's own budget stop names what claude said
    the run cost, not the $0.00 an ungoverned result starts at."""
    config = project.config()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    lines = [_INIT, _turn(), _turn(), _result(turns=2, subtype="error_max_budget_usd")]
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        result, _row, _, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            None,
            exit_code=0,
            lines=lines,
            checkout=project.checkout,
            config=config,
            publication_base_sha=project.base,
        )
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert result.estimated_cost_usd == pytest.approx(_turn_cost() * 2)
    assert f"${_turn_cost() * 2:.2f}" in stopped.payload["reason"]


@pytest.mark.parametrize(
    "budget_result",
    [
        _result(turns=2, subtype="error_max_budget_usd"),
        # No budget subtype, but a total over the cap: the host reads it the same.
        _result(turns=2, cost=_turn_cost() * 9),
    ],
)
def test_a_line_after_the_budget_stop_cannot_unsay_it(
    tmp_path: Path, monkeypatch, project: _Project, budget_result: str
) -> None:
    """The agent can write lines after claude's own budget result; the stop it
    reported stays a stop."""
    config = project.config()
    config.agent.max_budget_usd = _turn_cost() * 1.5
    trailer = json.dumps({"type": "system"})
    lines = [_INIT, _turn(), _turn(), budget_result, trailer]
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        result, row, _, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            None,
            exit_code=0,
            lines=lines,
            checkout=project.checkout,
            config=config,
            publication_base_sha=project.base,
        )

    assert project.forge.created_prs() == []
    assert result.error_category == "budget_stop"
    assert row.status == "budget_stop"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", "budget_stop"),
        ("transient", "budget_stop"),
        ("permanent", "budget_stop"),
        ("resource", "resource"),
    ],
)
def test_a_budget_stop_replaces_only_a_generic_failure(
    tmp_path: Path, before: str, after: str
) -> None:
    config = DispatchConfig()
    config.agent.max_budget_usd = 1.0
    latch = RunnerBudgetLatch("claude-code", config)
    latch.observe(_result(turns=1, subtype="error_max_budget_usd"))
    result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "x")
    result.error_category = before

    classify_runner_budget_stop(latch, result)

    assert result.error_category == after


#: A claude result whose reported total no float can hold.
_UNREADABLE_TOTAL = (
    '{"type":"result","subtype":"success",'
    '"modelUsage":{"claude-sonnet-4-6":{"costUSD":1}},'
    '"total_cost_usd":' + "9" * 401 + "}"
)


def test_a_total_too_large_to_read_is_a_stop_at_the_runners_own_cap(
    tmp_path: Path,
) -> None:
    config = DispatchConfig()
    latch = RunnerBudgetLatch("claude-code", config)

    latch.observe(_UNREADABLE_TOTAL)

    assert latch.stopped is True


def test_a_line_the_parser_refuses_is_counted_and_later_lines_still_latch() -> None:
    """An unreadable line is recorded on the latch, not taken as no stop: the
    budget result after it still latches."""
    config = DispatchConfig()
    config.agent.max_budget_usd = 1.0
    latch = RunnerBudgetLatch("claude-code", config)

    latch.observe('{"type":"result","subtype":"success","total_cost_usd":NaN}')
    assert (latch.unreadable, latch.stopped) == (1, False)

    latch.observe(_result(turns=1, subtype="error_max_budget_usd"))
    assert (latch.unreadable, latch.stopped) == (1, True)


def test_a_contained_run_reporting_an_unreadable_total_is_budget_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    """The latch reads the total off every protocol line; a total no float
    holds fails closed to a budget stop at a bounded cost, not a crash."""
    lines = [_INIT, _turn(), _UNREADABLE_TOTAL]
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        result, row, _, _ = _dispatch(tmp_path, store, monkeypatch, None, lines=lines)
        [stopped] = store.list_audit_events(event="sandbox_execution_failed")

    assert result.error_category == "budget_stop"
    assert row.status == "budget_stop"
    assert result.estimated_cost_usd == MAX_RECORDED_COST_USD
    json.loads(json.dumps(stopped.payload, allow_nan=False))
    assert f"${MAX_RECORDED_COST_USD:.2f}" in stopped.payload["reason"]


def test_a_contained_run_narrates_each_line_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The operator console shows each turn of a contained run once: the
    runner's own budget reading must not narrate the stream a second time."""
    config = DispatchConfig()
    config.agent.max_budget_usd = _turn_cost() * (_TURNS + 1)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _dispatch(tmp_path, store, monkeypatch, _governor_for(store, config))
    captured = capsys.readouterr()
    console = captured.out + captured.err

    assert console.count("working") == _TURNS
    assert console.count("[result]") == 1


def test_an_under_cap_run_that_exits_zero_is_published(
    tmp_path: Path, monkeypatch, project: _Project
) -> None:
    config = project.config()
    config.agent.max_budget_usd = _turn_cost() * (_TURNS + 1)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        result, row, container, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            governor,
            exit_code=0,
            checkout=project.checkout,
            config=config,
            publication_base_sha=project.base,
            checkout_generation=write_checkout_generation(project.checkout),
        )

    assert container.kills == 0
    assert len(project.forge.created_prs()) == 1
    assert project.remote_branches() == ["refs/heads/agent/issue-11"]
    assert result.published_pr_url == _PR_URL
    assert row.status == "success"


# --------------------------------------------------------------------------
# A contained cost stop keeps the work it stopped (checkout publication).
# --------------------------------------------------------------------------


def test_a_contained_cost_stop_keeps_committed_work_and_fails_the_item(
    tmp_path: Path, monkeypatch, committed_checkout: Path
) -> None:
    """The agent committed inside the container, so the tree is clean. The
    stop must not read "clean" as "nothing to keep": that completed the item
    and deleted the only clone holding the commits."""
    config = DispatchConfig()
    config.publication.publish = "checkout"
    # Checkout publication is admitted only with this posture (#1218).
    config.sandbox.dangerously_skip_containment = True
    config.agent.max_budget_usd = _turn_cost() * 1.5
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        governor = _governor_for(store, config)
        result, _row, _, _ = _dispatch(
            tmp_path,
            store,
            monkeypatch,
            governor,
            checkout=committed_checkout,
            config=config,
        )
    head = _git(committed_checkout, "rev-parse", "HEAD")
    forge = _forge_behind_gh(monkeypatch)

    outcome = dispatch_core._collect_result(
        _collect_ctx(config, result.run_id),
        result=result,
        worktree_dir=committed_checkout,
        run_sandboxed_path=True,
    )

    assert outcome.exit_code != 0
    # The item is failed with the budget-stop label, never completed.
    added = forge.labels_added(11)
    assert config.labels.agent_failed in added
    assert config.labels.budget_stop in added
    assert config.labels.pr_pending_review not in added
    assert len(forge.comments(11)) == 1
    assert _git(committed_checkout, "rev-parse", "HEAD") == head


def test_an_openshell_cost_stop_keeps_the_realized_work(
    tmp_path: Path, monkeypatch, committed_checkout: Path
) -> None:
    """On OpenShell the agent's work came back into ``<checkout>.realized``;
    the uploaded checkout is the launch snapshot. The stop commits and names
    the realized tree, never the snapshot."""
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    realized = tmp_path / "checkout.realized"
    shutil.copytree(committed_checkout, realized)
    (realized / "late.py").write_text("print('uncommitted work')\n", encoding="utf-8")
    snapshot_head = _git(committed_checkout, "rev-parse", "HEAD")
    realized_head = _git(realized, "rev-parse", "HEAD")
    result = DispatchResult(
        exit_code=137, issue_num=11, log_file=tmp_path / "run.log", run_id="run-11"
    )
    result.error_category = "budget_stop"
    result.realized_checkout = realized
    forge = _forge_behind_gh(monkeypatch)

    dispatch_core._collect_result(
        _collect_ctx(config, "run-11"),
        result=result,
        worktree_dir=committed_checkout,
        run_sandboxed_path=True,
    )

    assert _git(committed_checkout, "rev-parse", "HEAD") == snapshot_head
    assert _git(realized, "rev-parse", "HEAD") != realized_head
    assert _git(realized, "status", "--porcelain") == ""
    assert config.labels.budget_stop in forge.labels_added(11)
    [comment] = forge.comments(11)
    assert "WIP committed" in comment


def _collect_ctx(config: DispatchConfig, run_id: str) -> MagicMock:
    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.config = config
    ctx.checkout_generation = ""
    ctx.root_checkout_before = None
    ctx.issue_num = 11
    ctx.item_id = "11"
    ctx.branch = "agent/issue-11"
    ctx.issue_title = "t"
    ctx.run_id = run_id
    ctx.publication_base_sha = ""
    return ctx
