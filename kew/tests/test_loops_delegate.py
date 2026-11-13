"""Node I2 (#507): loop-dispatched RunRecords must record their worktree branch.

Loop-dispatched runs previously stored an empty ``RunRecord.branch``, so
``sync_outcomes`` could not link a loop's spend to its PR. These tests pin the
branch value threaded into ``run_tracked_agent`` (which writes it onto the
persisted ``RunRecord``) and guard the run/resume parity with the worktree.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.config import DispatchConfig
from kew.loops.delegate import (
    DelegateDriver,
    _loop_branch,
)
from kew.loops.verification import (
    HOST_OWNED_VERIFICATION_POSTURE,
    GateVerdict,
    HostVerificationAdmission,
)
from kew.publication_canonical import CanonicalPublication
from kew.run_store import LoopRecord, SqliteRunStore
from kew.tracking import DispatchResult


class _FakeGate:
    def __init__(self, verdict: GateVerdict) -> None:
        self._verdict = verdict

    def evaluate(
        self,
        *,
        worktree,
        base_ref,
        changed_paths,
        agent_written_checkout=False,
        dangerously_skip_containment=False,
        published_sha=None,
        publication_base_sha=None,
        publication_remote=None,
        extra_guarded_paths=(),
    ) -> GateVerdict:
        return self._verdict


def _admitted_config() -> DispatchConfig:
    return DispatchConfig()


def _declare_native_agent(monkeypatch) -> None:
    """Admit the loop agent under ``enabled = true`` for verification tests.

    A loop agent on the host is refused under ``enabled = true`` unless its
    runner contains itself (#1977). These tests are about verification, so
    they state that the agent's runner is natively contained.
    """

    def native(runner: str) -> str:
        assert runner == "claude-code"
        return "native"

    monkeypatch.setattr("kew.loops.delegate.runner_native_containment", native)


def _write_event_stream(event_file: Path) -> None:
    lines = [
        {
            "ts": 1000.0,
            "type": "started",
            "runner": "claude-code",
            "model": "claude-sonnet-4-6",
            "issue": 7,
        },
        {"ts": 1010.0, "type": "step", "input": 100, "output": 20, "cost_usd": 0.01},
        {
            "ts": 1030.0,
            "type": "finished",
            "exit_code": 0,
            "total_tokens": 120,
            "cost_usd": 0.01,
        },
    ]
    event_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _loop() -> LoopRecord:
    return LoopRecord(
        loop_id="L1",
        source="issue",
        goal_text="fix #7",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
        issue_num=7,
        repo="thossullivan/kew",
        budget_usd=5.0,
        wall_clock_secs=600,
        max_steps=200,
    )


def _patch_run(monkeypatch, tmp_path) -> dict:
    """Stub run_tracked_agent, capturing its kwargs (incl. ``branch``)."""
    from kew import worktree
    from kew.runners.registry import ResolvedRoute

    (tmp_path / ".git").mkdir(exist_ok=True)
    generation = worktree.write_checkout_generation(tmp_path)
    event_file = tmp_path / "run.events.jsonl"
    _write_event_stream(event_file)
    captured: dict = {}

    def _fake_run_tracked_agent(**kwargs):
        captured.update(kwargs)
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            event_file=event_file,
            error_category="",
            run_id="real-run-id",
        )

    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", _fake_run_tracked_agent)

    class _Runner:
        name = "claude-code"
        loop_style = "native"
        required_credentials = ()

    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda config, complexity=None: ResolvedRoute(
            runner=_Runner(), model="claude-sonnet-4-6"
        ),
    )

    def prepare(loop, config, **kwargs):
        kwargs["binding"].checkout_generation = generation
        return tmp_path

    monkeypatch.setattr("kew.loops.delegate._prepare_worktree", prepare)
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **k: "PROMPT")
    return captured


def _patch_run_real_prompt(monkeypatch, tmp_path) -> dict:
    """Like ``_patch_run`` but keeps ``build_prompt`` real so tests can assert on
    the rendered prompt, and routes the shared repo-map seam to a sentinel."""
    from kew.prompt import build_prompt as real_build_prompt

    captured = _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr("kew.loops.delegate.build_prompt", real_build_prompt)
    monkeypatch.setattr(
        "kew.dispatch_core.generate_repo_map",
        lambda *a, **k: "SYMBOL_MAP_SENTINEL",
    )
    return captured


def _run_loop(config, monkeypatch, tmp_path) -> dict:
    captured = _patch_run_real_prompt(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    ).run(loop)
    return captured


def test_loop_prompt_includes_repo_map(monkeypatch, tmp_path):
    """Task 5 / spec D5 (R3-7): the loop path delivers the repo map through the
    shared ``build_repo_map`` seam (it previously built prompts with no map)."""
    config = _admitted_config()
    assert config.prompt.include_repo_map  # default on
    captured = _run_loop(config, monkeypatch, tmp_path)
    assert "SYMBOL_MAP_SENTINEL" in captured["prompt"]
    assert "## Repo Map" in captured["prompt"]


def test_loop_prompt_omits_repo_map_when_disabled(monkeypatch, tmp_path):
    """The loop path honors ``include_repo_map = false`` — same seam as host."""
    config = _admitted_config()
    config.prompt.include_repo_map = False
    captured = _run_loop(config, monkeypatch, tmp_path)
    assert "SYMBOL_MAP_SENTINEL" not in captured["prompt"]
    assert "## Repo Map" not in captured["prompt"]


def test_canonical_loop_prompt_assigns_publication_to_the_host(monkeypatch, tmp_path):
    captured = _run_loop(_admitted_config(), monkeypatch, tmp_path)

    assert "NEVER commit or push any branch" in captured["prompt"]
    assert "Push and create PR" not in captured["prompt"]


def test_canonical_loop_publishes_before_gating_with_recorded_paths(
    monkeypatch, tmp_path
):
    from kew import dispatch_core

    _patch_run(monkeypatch, tmp_path)
    base = "a" * 40
    publication = CanonicalPublication(
        commit_sha="b" * 40,
        tree_sha="c" * 40,
        parent_shas=(base,),
        changed_paths=("src/from-publication.py",),
        flags=(),
    )
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(dispatch_core, "_trusted_dispatch_base", lambda _c: base)
    monkeypatch.setattr(
        dispatch_core,
        "_bind_publication_base",
        lambda *args, **kwargs: calls.append(("base", kwargs)),
    )

    def _publish(*args, **kwargs):
        calls.append(("publish", kwargs))
        callback = kwargs.get("on_publication")
        if callback is not None:
            callback(publication)
        return "https://example.invalid/pr/7"

    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", _publish)

    class _PublicationGate:
        def evaluate(self, **kwargs):
            calls.append(("gate", kwargs))
            return GateVerdict(passed=True, evidence="ok", sabotage=False)

    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    DelegateDriver(store, config=_admitted_config(), gate=_PublicationGate()).run(loop)

    assert [name for name, _detail in calls] == ["base", "publish", "gate"]
    gate_call = calls[-1][1]
    assert isinstance(gate_call, dict)
    assert gate_call["changed_paths"] == ["src/from-publication.py"]
    assert gate_call["published_sha"] == publication.commit_sha
    assert gate_call["agent_written_checkout"] is True


def test_canonical_command_gate_requires_uncontained_admission(
    monkeypatch, tmp_path, capsys
):
    calls: list[str] = []
    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda *_args, **_kwargs: calls.append("route"),
    )
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda *_args, **_kwargs: calls.append("worktree"),
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    result = DelegateDriver(config=DispatchConfig(), store=store).run(loop)

    assert result.status == "needs_human"
    assert result.status_reason is not None
    assert "#1444" in result.status_reason
    assert calls == []
    assert "#1444" in capsys.readouterr().out


def test_loop_run_records_branch(monkeypatch, tmp_path):
    """The loop's run threads a non-empty branch matching its worktree branch."""
    captured = _patch_run(monkeypatch, tmp_path)
    config = _admitted_config()
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    driver = DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    driver.run(loop)

    branch = captured["branch"]
    assert branch  # non-empty (the bug: this used to be "")
    # Equals the worktree branch setup_worktree/reuse_or_restore_worktree derive.
    assert branch == f"{config.branches.prefix}{loop.issue_num}"


def test_loop_branch_matches_worktree_formula(monkeypatch, tmp_path):
    """_loop_branch stays in lockstep with the worktree branch derivation."""
    config = _admitted_config()
    loop = _loop()
    assert _loop_branch(loop, config) == f"{config.branches.prefix}{loop.issue_num}"

    # And what the run actually passes agrees with the helper (no drift).
    captured = _patch_run(monkeypatch, tmp_path)
    store = SqliteRunStore(":memory:")
    store.save_loop(loop)
    DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    ).run(loop)
    assert captured["branch"] == _loop_branch(loop, config)


def test_loop_launch_admits_fresh_run_without_epic_ref(monkeypatch, tmp_path):
    """A loop-owned publication id is not a preserved-work continuation id."""
    from kew.runners.registry import ResolvedRoute

    class _Runner:
        name = "loop-test-runner"
        loop_style = "native"
        publication_style = "host"
        required_credentials = ()

    def _agent_process_stub(**_kwargs):
        return DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            error_category="",
        )

    monkeypatch.setattr("kew.loops.delegate.build_repo_map", lambda *_a, **_k: "")
    monkeypatch.setattr("kew.loops.delegate.build_prompt", lambda **_k: "PROMPT")
    monkeypatch.setattr("kew.dispatch_core.run_agent", _agent_process_stub)
    config = _admitted_config()
    config.retry.max_attempts = 1
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    loop = _loop()
    store.save_loop(loop)
    driver = DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    assert driver._admit_agent_launch(loop, runner=_Runner.name) is None

    result = driver._launch(
        loop,
        tmp_path,
        ResolvedRoute(_Runner(), "claude-sonnet-4-6"),
        run_id="fresh-loop-run",
        prompt_body=loop.goal_text,
        verification_admission=HostVerificationAdmission(
            posture=HOST_OWNED_VERIFICATION_POSTURE,
            refusal=None,
        ),
    )

    assert result.run_id == "fresh-loop-run"
    admitted = store.get("fresh-loop-run")
    assert admitted is not None
    assert admitted.run_kind == "loop"
    identity = store.get_admission_identity("fresh-loop-run")
    assert identity is not None
    assert identity.item_id == "7"
    assert identity.epic_ref is None


def test_loop_refuses_rewritten_base_before_agent_or_publication(monkeypatch, tmp_path):
    """The checkout uses the base resolved before a concurrent remote rewrite."""
    from kew import dispatch_core, worktree
    from kew.runners.registry import ResolvedRoute
    from kew.subprocess_utils import TOOL_PATHS

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "loop-checkout"
    import shutil
    import subprocess

    git_binary = shutil.which("git")
    assert git_binary is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git_binary)

    def _git(*args, cwd=seed):
        return subprocess.run(  # noqa: S603  # nosec B603, B607 -- fixture setup
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    _git("init", "--bare", "-q", "-b", "main", str(origin), cwd=tmp_path)
    _git("clone", "-q", str(origin), str(seed), cwd=tmp_path)
    _git("config", "user.name", "Test")
    _git("config", "user.email", "test@example.invalid")
    (seed / "base.txt").write_text("base\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")
    _git("push", "-q", "origin", "main")
    base = _git("rev-parse", "HEAD")

    class _Runner:
        name = "claude-code"
        loop_style = "native"
        required_credentials = ()

    route = ResolvedRoute(_Runner(), "claude-sonnet-4-6")
    monkeypatch.setattr("kew.loops.delegate.resolve_runner", lambda *_a, **_k: route)
    monkeypatch.setattr(
        "kew.loops.delegate._resolved_runner_preflight_failure", lambda *_a: None
    )
    monkeypatch.setattr(dispatch_core, "_trusted_dispatch_base", lambda _c: base)
    monkeypatch.setattr(worktree, "_repo_root", lambda: seed)

    def _setup(
        *,
        issue_num,
        config,
        item_id=None,
        provider="github",
        base_sha="",
        abandonment=None,
    ):
        del issue_num, config, item_id, provider
        _git("checkout", "-q", "--orphan", "rewrite")
        (seed / "base.txt").write_text("rewritten\n")
        _git("add", "-A")
        _git("commit", "-qm", "rewrite")
        _git("push", "-q", "--force", "origin", "rewrite:main")
        worktree._clone_checkout_on(
            checkout,
            "agent/issue-7",
            start_point="main",
            exact_sha=base_sha,
        )
        return checkout

    monkeypatch.setattr("kew.loops.delegate.setup_worktree", _setup)
    monkeypatch.setattr(
        "kew.loops.delegate.run_tracked_agent",
        lambda **_k: pytest.fail("agent must not launch"),
    )
    monkeypatch.setattr(
        dispatch_core,
        "_push_and_open_pr",
        lambda *_a, **_k: pytest.fail("publication must not start"),
    )
    store = SqliteRunStore(str(tmp_path / "loop.db"))
    loop = _loop()
    store.save_loop(loop)

    with pytest.raises(RuntimeError, match="not reachable"):
        DelegateDriver(
            config=_admitted_config(),
            store=store,
            gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
        ).run(loop)

    assert not checkout.exists()
    assert store.list_by_issue(7) == []


def test_normal_dispatch_branch_default_unchanged():
    """Guard: normal dispatch's branch path is untouched — run_tracked_agent
    still defaults ``branch`` to "" (only the loop call site was the gap)."""
    import inspect

    from kew.dispatch_core import run_tracked_agent

    sig = inspect.signature(run_tracked_agent)
    assert sig.parameters["branch"].default == ""


def test_loop_entry_records_uncontained_verification_posture(
    monkeypatch, tmp_path, capsys
):
    _patch_run(monkeypatch, tmp_path)
    config = DispatchConfig(publication={"publish": "checkout"})
    config.sandbox.dangerously_skip_containment = True
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    ).run(loop)

    output = capsys.readouterr().out
    assert "uncontained host verification" in output
    [event] = store.list_audit_events(event="loop_started")
    assert event.payload is not None
    assert event.payload["verification_posture"] == "uncontained_host_verification"


def test_loop_started_uses_resolved_verification_admission(monkeypatch, tmp_path):
    _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "kew.loops.delegate._loop_verification_admission",
        lambda _config, _gate=None: HostVerificationAdmission(
            posture=HOST_OWNED_VERIFICATION_POSTURE,
            refusal=None,
        ),
    )
    config = _admitted_config()
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    DelegateDriver(
        config=config,
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    ).run(loop)

    [event] = store.list_audit_events(event="loop_started")
    assert event.payload is not None
    assert event.payload["verification_posture"] == HOST_OWNED_VERIFICATION_POSTURE


def test_loop_refuses_uncontained_verification_before_worktree_or_agent(
    monkeypatch, capsys
):
    calls: list[str] = []
    monkeypatch.setattr(
        "kew.loops.delegate.resolve_runner",
        lambda *_args, **_kwargs: calls.append("route"),
    )
    monkeypatch.setattr(
        "kew.loops.delegate._prepare_worktree",
        lambda *_args, **_kwargs: calls.append("worktree"),
    )
    monkeypatch.setattr(
        "kew.loops.delegate.run_tracked_agent",
        lambda **_kwargs: calls.append("agent"),
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)

    result = DelegateDriver(
        config=DispatchConfig(
            publication={"publish": "checkout"}, sandbox={"enabled": True}
        ),
        store=store,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    ).run(loop)

    assert result.status == "needs_human"
    assert result.status_reason is not None
    assert "#1444" in result.status_reason
    assert calls == []
    assert "#1444" in capsys.readouterr().out
    [event] = store.list_audit_events(event="needs_human_raised")
    assert event.payload is not None
    assert "#1444" in str(event.payload["reason"])


def test_resume_refuses_uncontained_verification_before_checkout_work(monkeypatch):
    from kew.runners.registry import ResolvedRoute

    calls: list[str] = []

    class _NativeRunner:
        name = "claude-code"
        loop_style = "native"

    class _RecordingGate:
        def evaluate(self, **_kwargs):
            calls.append("verification")
            return GateVerdict(passed=True, evidence="ok", sabotage=False)

    resolved = ResolvedRoute(
        runner=_NativeRunner(),
        model="claude-sonnet-4-6",
    )
    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route",
        lambda *_args, **_kwargs: calls.append("route") or resolved,
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )
    monkeypatch.setattr(
        "kew.loops.delegate.reuse_or_restore_worktree",
        lambda **_kwargs: calls.append("worktree"),
    )
    monkeypatch.setattr(
        "kew.loops.delegate.run_tracked_agent",
        lambda **_kwargs: calls.append("agent"),
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    loop.status = "needs_human"
    loop.status_reason = "prior gate failed"
    store.save_loop(loop)

    config = DispatchConfig(
        publication={"publish": "checkout"}, sandbox={"enabled": True}
    )
    assert config.sandbox.dangerously_skip_containment is False
    driver = DelegateDriver(
        config=config,
        store=store,
        gate=_RecordingGate(),
    )
    with pytest.raises(ValueError, match="#1444"):
        driver.resume(loop, "retry")

    assert calls == ["route"]
    persisted = store.get_loop(loop.loop_id)
    assert persisted is not None
    assert persisted.status == "needs_human"
    assert store.list_audit_events(event="loop_resumed", limit=None) == []


@pytest.fixture(autouse=True)
def _loops_use_canonical_publication(monkeypatch):
    """Keep unit tests on canonical mode without using a real remote."""
    from kew import dispatch_core

    publication = CanonicalPublication(
        commit_sha="b" * 40,
        tree_sha="c" * 40,
        parent_shas=("a" * 40,),
        changed_paths=("src/foo.py",),
        flags=(),
    )
    monkeypatch.setattr(dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40)
    monkeypatch.setattr(dispatch_core, "_bind_publication_base", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_core, "bind_publication_lineage", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_core, "_git_push_branch", lambda *a, **k: publication)
    monkeypatch.setattr(
        "kew.loops.delegate.publication_remote",
        lambda _repo: "https://example.invalid/repo.git",
    )

    def _open(*_args, **kwargs):
        callback = kwargs.get("on_publication")
        if callback is not None:
            callback(publication)
        return "https://example.invalid/pr/7"

    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", _open)


@pytest.mark.parametrize("raw_code", [0, 137])
def test_round_four_loop_keeps_execution_without_step_events(
    monkeypatch, tmp_path, raw_code
):
    from types import SimpleNamespace

    captured = _patch_run(monkeypatch, tmp_path)
    result = DispatchResult(raw_code, 7, tmp_path / "run.log")
    result.launched = True
    result.run_id = "real-run-id"
    monkeypatch.setattr("kew.loops.delegate.run_tracked_agent", lambda **kwargs: result)
    config = DispatchConfig()
    config.sandbox.dangerously_skip_containment = True
    driver = DelegateDriver(
        SqliteRunStore(":memory:"),
        config=config,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    monkeypatch.setattr(
        driver, "_prepare_publication", lambda *a: SimpleNamespace(run_id="real-run-id")
    )
    monkeypatch.setattr(driver, "_bind_publication", lambda *a: None)
    monkeypatch.setattr(driver, "_publish_iteration", lambda *a: None)
    loop = _loop()
    driver._store.save_loop(loop)
    finished = driver.run(loop)
    assert getattr(driver, "launched", False) is True
    assert finished.status == "needs_human"
    assert finished.step_count == 0
    assert captured == {}


def _round_ten_teardown_launch(driver, loop, tmp_path, started):
    from types import SimpleNamespace
    from unittest.mock import patch

    from kew.dispatch_core import _record_retry_spawn
    from kew.loops.verification import HostVerificationAdmission

    def tracked(**kwargs):
        state = kwargs.get("launch_state")
        if started and state is not None:
            _record_retry_spawn(
                SimpleNamespace(
                    launch_state=state, store=None, contained_image=None, on_launch=None
                ),
                1234,
            )
        raise OSError("sidecar teardown failed" if started else "pre-launch failure")

    with (
        patch("kew.loops.delegate.build_repo_map", return_value=""),
        patch("kew.loops.delegate.build_prompt", return_value="work"),
        patch("kew.loops.delegate.audit.emit"),
        patch("kew.loops.delegate.run_tracked_agent", side_effect=tracked),
    ):
        assert driver._admit_agent_launch(loop, runner="claude-code") is None
        return driver._launch(
            loop,
            tmp_path,
            SimpleNamespace(runner=SimpleNamespace(name="claude-code"), model="model"),
            run_id="real-run-id",
            prompt_body="work",
            verification_admission=HostVerificationAdmission(
                refusal=None, posture="contained_verification"
            ),
        )


def test_round_ten_loop_preserves_launch_when_teardown_raises(tmp_path):
    driver = DelegateDriver(
        SqliteRunStore(":memory:"),
        config=DispatchConfig(),
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    work = tmp_path / "work.txt"
    work.write_text("agent work")
    loop = _loop()
    for started in (True, False):
        driver.launched = False
        with pytest.raises(OSError, match="teardown" if started else "pre-launch"):
            _round_ten_teardown_launch(driver, loop, tmp_path, started)
        assert driver.launched is started
        assert work.read_text() == "agent work"


@pytest.mark.parametrize("contained", [True, False])
def test_canonical_verification_records_path_and_image(
    monkeypatch, tmp_path, contained
):
    from kew import sandbox
    from kew.loops import verification

    _patch_run(monkeypatch, tmp_path)
    if contained:
        _declare_native_agent(monkeypatch)
    config = DispatchConfig(
        sandbox={"enabled": True}
        if contained
        else {"dangerously_skip_containment": True}
    )
    monkeypatch.setattr(
        sandbox,
        "detect_runtime_cached",
        lambda _config: "/bin/docker" if contained else None,
    )
    monkeypatch.setattr(
        verification,
        "require_image_id",
        lambda *a, **k: "sha256:" + "d" * 64,
        raising=False,
    )
    # For fallback, exercise the real missing-runtime refusal from require_image_id.
    if not contained:
        monkeypatch.setattr(verification, "require_image_id", sandbox.require_image_id)
    monkeypatch.setattr(
        verification.CommandCondition,
        "_check_published",
        lambda *a, **k: verification.ConditionResult(True, "recorded tail"),
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    finished = DelegateDriver(config=config, store=store).run(loop)
    assert finished.status == "achieved"
    expected = "contained_verification" if contained else "host_verification"
    for name in ("loop_started", "verification_checked"):
        [event] = store.list_audit_events(event=name)
        assert event.payload["verification_posture"] == expected
        assert event.payload["verification_image"] == (
            "sha256:" + "d" * 64 if contained else None
        )
    assert "recorded tail" in event.payload["evidence"]


def test_enabled_command_refusal_records_missing_prerequisites(monkeypatch):
    from unittest.mock import Mock

    worktree = Mock(side_effect=AssertionError("checkout must not start"))
    monkeypatch.setattr("kew.loops.delegate._prepare_worktree", worktree)
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    finished = DelegateDriver(
        config=DispatchConfig(sandbox={"enabled": True}), store=store
    ).run(loop)
    assert finished.status == "needs_human"
    events = store.list_audit_events(event="verification_checked")
    assert len(events) == 1, "refused command verification needs a posture record"
    assert events[0].payload["verification_posture"] == "refused_verification"
    assert "no container runtime" in events[0].payload["verification_refusal"]
    assert events[0].payload["verification_image"] is None
    worktree.assert_not_called()


def test_verification_admits_the_routed_runner_image(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from kew import sandbox
    from kew.loops import verification
    from kew.runners.registry import ResolvedRoute

    _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: "/bin/docker")
    inspected = Mock(return_value="sha256:" + "d" * 64)
    monkeypatch.setattr(verification, "require_image_id", inspected, raising=False)
    config = DispatchConfig(
        sandbox={"enabled": True, "images": {"opencode": "routed:tag"}}
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    driver = DelegateDriver(config=config, store=store)
    runner = type("Runner", (), {"name": "opencode", "loop_style": "native"})()
    # Stop after admission, before unrelated model/transport checks.
    monkeypatch.setattr(
        "kew.loops.delegate._require_loop_transport_admission",
        Mock(side_effect=ValueError("stop after admission")),
    )
    driver.run(loop, route=ResolvedRoute(runner=runner, model="test-model"))
    inspected.assert_called_once_with(
        "/bin/docker",
        "routed:tag",
        runner="opencode",
        refusal="command verification refused",
    )


@pytest.fixture
def contained_verification_driver(monkeypatch, tmp_path):
    """Use the real command gate and persistence with local preparation doubles."""
    import subprocess
    from types import SimpleNamespace

    from kew import sandbox
    from kew.loops import verification

    _patch_run(monkeypatch, tmp_path)
    _declare_native_agent(monkeypatch)
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: "/bin/docker")
    monkeypatch.setattr(
        verification, "require_image_id", lambda *a, **k: "sha256:" + "d" * 64
    )
    paths = {}

    def provision(path, **kwargs):
        paths["repo"] = path
        return SimpleNamespace(path=path)

    def materialize(repository, checkout, published_sha, git):
        checkout.mkdir()
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(verification, "provision", provision)
    monkeypatch.setattr(verification, "fetch_commit", lambda *a, **k: None)
    monkeypatch.setattr(verification, "_materialize_verification_checkout", materialize)
    config = DispatchConfig(
        sandbox={"enabled": True},
        commands={"verify_loop": "./verify.sh", "verify_loop_needs_writes": True},
    )
    store = SqliteRunStore(":memory:")
    loop = _loop()
    store.save_loop(loop)
    return DelegateDriver(config=config, store=store), store, loop, paths


@pytest.mark.serial
@pytest.mark.parametrize("closed_pipes", [False, True], ids=["quiet", "closed"])
@pytest.mark.parametrize("quota", ["bytes", "entries", "floor", "normal"])
def test_writable_verification_enforces_checkout_quotas(
    contained_verification_driver, monkeypatch, tmp_path, quota, closed_pipes
):
    import sys
    from types import SimpleNamespace

    from kew import sandbox, subprocess_utils

    driver, store, loop, _ = contained_verification_driver
    runtime = tmp_path / "fake-runtime"
    removed = tmp_path / "removed"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"state = Path({str(tmp_path / 'runtime-state.json')!r})\n"
        "if sys.argv[1] == 'rm':\n"
        "    assert sys.argv[-1] == 'a' * 64\n"
        f"    with open({str(removed)!r}, 'a') as log:\n"
        "        log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "    sys.exit(0)\n"
        "if sys.argv[1] == 'inspect':\n"
        "    assert sys.argv[-1] == 'a' * 64\n"
        "    print(state.read_text())\n"
        "    sys.exit(0)\n"
        "Path(sys.argv[sys.argv.index('--cidfile') + 1]).write_text('a' * 64)\n"
        "state.write_text(json.dumps({'Name': '/' + "
        "sys.argv[sys.argv.index('--name') + 1], "
        "'State': {'Status': 'exited', 'ExitCode': 0}}))\n"
        "mount = sys.argv[sys.argv.index('-v') + 1]\n"
        "assert mount.endswith(':/work:rw')\n"
        "root = Path(mount.split(':')[0])\n"
        f"if {closed_pipes!r}:\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "time.sleep(0.1)\n"
        f"for i in range({6 if quota == 'entries' else 3}):\n"
        f"    (root / str(i)).write_bytes(b'X' * {0 if quota == 'entries' else 512})\n"
        "time.sleep(1.0)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(runtime))
    # Small real files exercise the shared measurement without consuming GiB.
    monkeypatch.setattr(
        sandbox, "CHECKOUT_QUOTA_BYTES", 1024 if quota == "bytes" else 4096
    )
    monkeypatch.setattr(sandbox, "CHECKOUT_QUOTA_ENTRIES", 4)
    monkeypatch.setattr(sandbox, "HOST_FREE_SPACE_FLOOR_BYTES", 1024)
    monkeypatch.setattr(sandbox, "WATCHDOG_INTERVAL_SECONDS", 0.02)
    probes = []
    usage = sandbox.checkout_usage

    def measure(root, **kwargs):
        result = usage(root, **kwargs)
        probes.append(result)
        return result

    def disk_usage(root):
        free = 512 if quota == "floor" and (Path(root) / "0").exists() else 8192
        return SimpleNamespace(free=free)

    monkeypatch.setattr(sandbox, "checkout_usage", measure)
    monkeypatch.setattr(sandbox.shutil, "disk_usage", disk_usage)
    finished = driver.run(loop)
    [event] = store.list_audit_events(event="verification_checked")
    persisted = store.get_loop(loop.loop_id)
    assert persisted is not None
    if quota == "normal":
        assert len(probes) > 1, "writable verification must poll checkout usage"
        assert (1536, 3) in probes
        assert finished.status == persisted.status == "achieved"
        assert event.payload["passed"] is True
        # Completion is confirmed through runtime state, then the container is
        # removed once without force; the watchdog never intervened.
        [removal] = removed.read_text().splitlines()
        assert removal.split() == ["rm", "a" * 64]
    else:
        reason = {
            "bytes": "checkout exceeded 1024 bytes",
            "entries": "checkout exceeded 4 entries",
            "floor": "host free space 512 bytes below the 1024 byte floor",
        }[quota]
        assert f"checkout-quota refusal: {reason}" in persisted.status_reason
        assert reason in event.payload["evidence"]
        assert finished.status == persisted.status == "needs_human"
        assert event.payload["passed"] is False
        # The watchdog's forced removal comes first.
        forced = removed.read_text().splitlines()[0].split()
        assert forced == ["rm", "-f", "a" * 64]


@pytest.mark.parametrize(
    "surface",
    ["success", "overflow", "preparation_result", "preparation_error", "launch_error"],
)
def test_verification_persists_sanitized_host_paths(
    contained_verification_driver, monkeypatch, tmp_path, surface
):
    import subprocess

    from kew import subprocess_utils
    from kew.loops import verification

    driver, store, loop, paths = contained_verification_driver
    host_paths = []

    def message():
        host_paths[:] = [
            str(paths["repo"]),
            str(Path.home() / "operator-private"),
            str(tmp_path / "worktree-private"),
        ]
        return "\n".join(host_paths) + "\n"

    def run(*args, **kwargs):
        output = message()
        if surface == "launch_error":
            raise OSError(output)
        if surface == "overflow":
            buffers = {
                name: subprocess_utils.BoundedOutput(4096)
                for name in ("stdout", "stderr")
            }
            for buffer in buffers.values():
                buffer.append(("X" * 5000 + "\n" + output).encode())
            raise subprocess_utils.OutputBudgetExceeded("stdout", buffers)
        return subprocess.CompletedProcess([], 0, output, output)

    def materialize(*args):
        if surface == "preparation_error":
            raise OSError(message())
        return subprocess.CompletedProcess([], 1, "", message())

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", run)
    if surface.startswith("preparation"):
        monkeypatch.setattr(
            verification, "_materialize_verification_checkout", materialize
        )
    finished = driver.run(loop)
    assert finished.status == ("achieved" if surface == "success" else "needs_human")
    persisted = store.get_loop(loop.loop_id)
    assert persisted is not None
    [event] = store.list_audit_events(event="verification_checked")
    for evidence in (persisted.status_reason, event.payload["evidence"]):
        for path in host_paths:
            assert path not in evidence
            assert f"<path>/{Path(path).name}" in evidence
        assert str(Path.home()) not in evidence
        assert str(tmp_path) not in evidence
