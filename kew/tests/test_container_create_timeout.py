"""A container ``create`` that times out on a loaded host is retried (#3103).

The runtime here is a real executable on ``TOOL_PATHS``: kew's own process
call times it out and kills it, exactly as it does a slow ``docker``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kew import sandbox, subprocess_utils
from kew.config import DispatchConfig, RetryConfig
from kew.dispatch_core import run_tracked_agent
from kew.dispatch_sandbox import (
    _record_sandbox_start,
    container_launch_failure,
    prepare_registered_container,
)
from kew.run_store import SqliteRunStore
from kew.sandbox import ContainerCreateTimedOut
from kew.tracking import (
    CONTAINER_NEVER_STARTED_CATEGORIES,
    STARTUP_REFUSED_CATEGORIES,
    startup_refused,
)

_FAKE_DOCKER = """#!{python}
import json, sys, time
from pathlib import Path

STATE = Path({state!r})
state = json.loads(STATE.read_text())
args = sys.argv[1:]
verb, target = args[0], args[-1]


def save():
    STATE.write_text(json.dumps(state))


def find(target):
    for cid, c in state["containers"].items():
        if target in (cid, c["name"]):
            return cid
    return None


if verb == "create":
    name = args[args.index("--name") + 1]
    cidfile = Path(args[args.index("--cidfile") + 1])
    if cidfile.exists():
        sys.stderr.write(f"Container ID file found: {{cidfile}}\\n")
        sys.exit(125)
    state["creates"] += 1
    if find(name) is not None:
        save()
        sys.stderr.write(f'Conflict. The container name "/{{name}}" is in use\\n')
        sys.exit(1)
    labels = dict(
        args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label"
    )
    cid = format(state["creates"], "064x")
    cidfile.write_text("")
    hang = state["creates"] <= state["timeouts"]
    if not hang or state["leave"]:
        state["containers"][cid] = {{"name": name, "labels": labels}}
    save()
    if hang:
        time.sleep(60)
    cidfile.write_text(cid)
    print(cid)
elif verb == "info":
    fields = len(args[args.index("--format") + 1].split())
    print(" ".join(["engine-1"] * fields))
elif verb == "image":
    print(json.dumps([{{"Id": "sha256:" + "f" * 64}}]))
elif find(target) is None:
    sys.stderr.write(f"Error: No such container: {{target}}\\n")
    sys.exit(1)
elif verb == "inspect":
    cid = find(target)
    if args[args.index("--format") + 1] == "{{{{.Id}}}}":
        print(cid)
    else:
        labels = json.dumps(state["containers"][cid]["labels"])
        print(f"{{cid}} created 0 {{labels}}")
elif verb == "rm":
    del state["containers"][find(target)]
    save()
    print(target)
else:
    sys.exit(2)
"""


class FakeRuntime:
    """A ``docker`` whose first *timeouts* creates hang past kew's limit."""

    def __init__(self, root: Path, *, timeouts: int, leave: bool) -> None:
        self.state = root / "docker-state.json"
        self.state.write_text(
            json.dumps(
                {"creates": 0, "timeouts": timeouts, "leave": leave, "containers": {}}
            )
        )
        self.path = root / "docker"
        self.path.write_text(
            _FAKE_DOCKER.format(python=sys.executable, state=str(self.state))
        )
        self.path.chmod(0o755)

    def read(self) -> dict[str, object]:
        return dict(json.loads(self.state.read_text()))


@pytest.fixture
def runtime_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    # A short limit stands in for a loaded host's slow create; no backoff wait.
    monkeypatch.setattr(sandbox, "RUNTIME_CONTROL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr("kew.dispatch_sandbox.CREATE_TIMEOUT_BACKOFF_SECONDS", 0.0)

    def make(*, timeouts: int, leave: bool = False) -> FakeRuntime:
        fake = FakeRuntime(tmp_path, timeouts=timeouts, leave=leave)
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(fake.path))
        return fake

    return make


def _store(root: Path) -> SqliteRunStore:
    root.mkdir(parents=True, exist_ok=True)
    return SqliteRunStore(str(root / "runs.db"), project_root=root)


def _admit(store: SqliteRunStore, tmp_path: Path) -> str:
    return _record_sandbox_start(
        store,
        issue_num=42,
        item_id="42",
        branch="agent/42",
        title="test",
        model="test",
        runner_name="claude-code",
        log_file=tmp_path / "run.log",
        repo="o/r",
        worker_cap=4,
    )


def _argv(tmp_path: Path) -> list[str]:
    cidfile = tmp_path / "run.cid"
    return ["docker", "run", "--name", "kew-42", "--cidfile", str(cidfile), "image"]


def _retries(store: SqliteRunStore) -> list[int]:
    """The attempts recorded as retried, oldest first."""
    return sorted(
        int(event.payload["attempt"])
        for event in store.list_audit_events(event="container_create_retried")
        if isinstance(event.payload, dict)
    )


def test_a_create_that_times_out_once_is_retried_and_the_run_launches(
    tmp_path: Path, runtime_factory: object
) -> None:
    fake = runtime_factory(timeouts=1)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        execution = prepare_registered_container(_argv(tmp_path), store, run_id)

        state = fake.read()
        assert state["creates"] == 2
        assert list(state["containers"]) == [execution.container_id]  # type: ignore[call-overload]
        assert _retries(store) == [1]
        record = store.get(run_id)
        assert record is not None
        assert record.container_id == execution.container_id
        assert record.error_category in (None, "")
        assert store.review_rounds_in_cycle("42") == []


def test_a_timed_out_create_that_left_a_container_is_removed_before_the_retry(
    tmp_path: Path, runtime_factory: object
) -> None:
    fake = runtime_factory(timeouts=1, leave=True)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        execution = prepare_registered_container(_argv(tmp_path), store, run_id)

        state = fake.read()
        # The leftover went before the retry, so the retry met no name conflict.
        assert state["creates"] == 2
        assert list(state["containers"]) == [execution.container_id]  # type: ignore[call-overload]
        assert execution.container_id == format(2, "064x")
        removed = store.list_audit_events(event="stale_container_removed")
        assert [event.payload["container_id"] for event in removed] == [  # type: ignore[index]
            format(1, "064x")
        ]
        assert _retries(store) == [1]


def test_a_create_that_always_times_out_fails_as_a_runtime_timeout(
    tmp_path: Path, runtime_factory: object
) -> None:
    fake = runtime_factory(timeouts=99)  # type: ignore[operator]
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerCreateTimedOut, match="too slow") as raised:
            prepare_registered_container(_argv(tmp_path), store, run_id)

        assert raised.value.category == "runtime_timeout"
        assert raised.value.attempts == 3
        assert fake.read()["creates"] == 3
        assert _retries(store) == [1, 2]
        # Like any failed create, the daemon may still have made it.
        evidence = container_launch_failure(raised.value)
        assert evidence is not None
        assert evidence.cleanup_confirmed is False


def test_a_tracked_run_whose_create_always_times_out_spends_no_round(
    tmp_path: Path, runtime_factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_factory(timeouts=99)  # type: ignore[operator]
    monkeypatch.setitem(sandbox._RUNTIME_CACHE, "docker", "docker")
    runner = MagicMock()
    runner.name = "claude-code"
    runner.container_args.return_value = []
    config = DispatchConfig(retry=RetryConfig(max_attempts=1, backoff_base_seconds=0))
    with _store(tmp_path / "project") as store:
        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            runner=runner,
            model="m",
            contained_image="image",
            store=store,
            run_kind="adversarial_review",
        )

        assert result.error_category == "runtime_timeout"
        assert "too slow" in (result.stop_reason or "")
        (row,) = store.list_by_issue(42)
        assert (row.status, row.error_category) == ("failure", "runtime_timeout")
        assert store.review_rounds_in_cycle("42") == []


def test_a_create_that_times_out_three_times_is_a_startup_refusal(
    tmp_path: Path, runtime_factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: the tracked run three timed-out creates leave behind is a startup
    refusal — the one predicate a CI fix settles its repair on and ``kew
    review`` reads as an agent that never started (the review half, through
    ``kew review`` itself, is in ``test_review_checkout_retention.py``)."""
    fake = runtime_factory(timeouts=99)  # type: ignore[operator]
    monkeypatch.setitem(sandbox._RUNTIME_CACHE, "docker", "docker")
    runner = MagicMock()
    runner.name = "claude-code"
    runner.container_args.return_value = []
    config = DispatchConfig(retry=RetryConfig(max_attempts=1, backoff_base_seconds=0))
    with _store(tmp_path / "project") as store:
        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            runner=runner,
            model="m",
            contained_image="image",
            store=store,
            run_kind="adversarial_review",
        )

        assert fake.read()["creates"] == 3
        assert result.error_category == "runtime_timeout"
        assert result.launched is False
        assert startup_refused(result)


def test_create_retries_stop_at_the_attempts_deadline(
    tmp_path: Path, runtime_factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: the retries stop at the attempt's *deadline*, never past it. The
    clock is pinned and moves only by the backoff waits: with a deadline of 10 s
    and a 4 s first backoff, one retry fits and the next (8 s wait plus a
    create that again times out) would end past the bound."""
    fake = runtime_factory(timeouts=99)  # type: ignore[operator]
    now = [0.0]
    slept: list[float] = []

    def _wait(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("kew.dispatch_sandbox.launch_clock", lambda: now[0])
    monkeypatch.setattr("kew.dispatch_sandbox.launch_wait", _wait)
    monkeypatch.setattr("kew.dispatch_sandbox.CREATE_TIMEOUT_BACKOFF_SECONDS", 4.0)
    with _store(tmp_path / "project") as store:
        run_id = _admit(store, tmp_path)
        with pytest.raises(ContainerCreateTimedOut) as raised:
            prepare_registered_container(_argv(tmp_path), store, run_id, deadline=10.0)

        assert raised.value.attempts == 2
        assert fake.read()["creates"] == 2
        assert slept == [4.0]
        # The last create ran to its own 2 s timeout after the one wait.
        assert now[0] + 2.0 <= 10.0
        assert _retries(store) == [1]


def test_one_set_of_pre_launch_categories_means_never_started_on_every_lane() -> None:
    """#3186: the categories ``ci_fix`` and ``kew review`` settle as startup
    refusals are exactly the ones the review stage holds as "the reviewer
    never started", and the prompt-outcome ledger counts none of them."""
    from kew.graph.driver.review_stage import _PRE_LAUNCH_HOLDS
    from kew.run_store_prompt_profiles import NOT_LAUNCHED_CATEGORIES

    assert set(_PRE_LAUNCH_HOLDS) == STARTUP_REFUSED_CATEGORIES
    assert "runtime_timeout" in CONTAINER_NEVER_STARTED_CATEGORIES
    assert CONTAINER_NEVER_STARTED_CATEGORIES <= STARTUP_REFUSED_CATEGORIES
    assert STARTUP_REFUSED_CATEGORIES <= NOT_LAUNCHED_CATEGORIES
