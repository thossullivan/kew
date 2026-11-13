"""O1 (#892): enforcement resolves effective config = kew.toml + DB overrides.

Live incident 2026-07-20: a ``budget.weekly_usd`` override raised via the web
/config surface was ignored by the dispatch budget preflight, which read the
stale kew.toml value and refused every node. The three enforcement entry points
— the direct CLI dispatch path, the epic driver, and the detached
``_dispatch-node`` child — must all resolve config through the one shared
resolver, ``load_config(store=store)`` (precedence: env > DB > kew.toml >
defaults).
"""

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from kew.budget import BudgetExceeded, check_budget
from kew.config import DispatchConfig, load_config
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def _local_project_boundary(tmp_path):
    """Keep temporary projects outside the operator's managed ledger."""
    (tmp_path / ".git").mkdir()


def _spend(store: SqliteRunStore, usd: float, *, run_id: str, when: datetime) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=when,
            estimated_cost_usd=usd,
        )
    )


def test_db_override_governs_weekly_budget_preflight(tmp_path):
    """A ``config_overrides`` weekly_usd row governs the preflight, not the file.

    This is the live-incident regression: file says $600, override says $1500,
    and $700 of spend is recorded. The file value would refuse; the effective
    (override-applied) value must admit.
    """
    toml = tmp_path / "kew.toml"
    # Per-issue / daily headroom so weekly is the sole binding cap under test.
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 600.0\n"
    )
    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 700.0, run_id="r-1", when=now)

        # File-only config: $700 spend breaches the $600 file cap.
        file_cfg = load_config(config_path=toml)
        assert file_cfg.budget.weekly_usd == 600.0
        with pytest.raises(BudgetExceeded) as exc:
            check_budget(store, file_cfg.budget, 1, now=now)
        assert exc.value.which_limit == "weekly"

        # Raise the cap to $1500 via a DB override (the /config surface path).
        store.set_config_override(
            field="budget.weekly_usd",
            value_json="1500.0",
            file_value_json="600.0",
            set_at=now.isoformat(),
            set_by="test",
        )

        # The SHARED resolver applies the override; $700 now passes the preflight.
        eff_cfg = load_config(config_path=toml, store=store)
        assert eff_cfg.budget.weekly_usd == 1500.0
        status = check_budget(store, eff_cfg.budget, 1, now=now)
        assert status.weekly_limit_usd == 1500.0


def test_delegation_dispatch_preflight_resolves_config_with_store(tmp_path):
    """A delegation dispatch preflight resolves config through the SHARED resolver.

    Assign-to-kew delegation (D1/#920) is the foundation the rest of the epic
    builds on; its budget preflight must honor a DB override exactly as the
    epic-driver and ``_dispatch-node`` paths already do. File says $600, a DB
    override raises it to $1500, and $700 of spend is recorded: a file-only read
    would refuse the run (weekly breach); the shared resolver admits it.

    This fails if ``delegation_budget_preflight`` is ever wired to read file-only
    config (``load_config`` without ``store=``) and passes only when it threads
    the store through.
    """
    from kew.budget import delegation_budget_preflight

    toml = tmp_path / "kew.toml"
    # Per-issue / daily headroom so weekly is the sole binding cap under test.
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 600.0\n"
    )
    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend(store, 700.0, run_id="r-1", when=now)

        # Sanity: file-only config would refuse — $700 spend breaches the $600 cap.
        file_cfg = load_config(config_path=toml)
        with pytest.raises(BudgetExceeded) as exc:
            check_budget(store, file_cfg.budget, 1, now=now)
        assert exc.value.which_limit == "weekly"

        # Raise the cap to $1500 via a DB override (the /config surface path).
        store.set_config_override(
            field="budget.weekly_usd",
            value_json="1500.0",
            file_value_json="600.0",
            set_at=now.isoformat(),
            set_by="test",
        )

        # The delegation preflight goes through load_config(store=store), so the
        # override applies and $700 now passes the shared budget guard.
        status = delegation_budget_preflight(store, 1, config_path=toml, now=now)
        assert status.weekly_limit_usd == 1500.0


def test_delegation_registered_as_run_kind():
    """``delegation`` is registered as a run_kind bucket alongside the others.

    The report run-kind breakdown orders it deterministically (not alphabetically
    after the known kinds), proving the D1 run_kind seam is wired consistently.
    """
    from kew.budget import DELEGATION_RUN_KIND
    from kew.report_export import _RUN_KIND_ORDER

    assert DELEGATION_RUN_KIND == "delegation"
    assert "delegation" in _RUN_KIND_ORDER
    # Ordering contract: delegation sits immediately after planning.
    planning_idx = _RUN_KIND_ORDER.index("planning")
    assert _RUN_KIND_ORDER.index("delegation") == planning_idx + 1


def _wiring_patches(monkeypatch):
    """Patch cli.load_config to record whether callers pass ``store=``."""
    calls: list[dict] = []

    real_load_config = load_config

    def _tracking_load_config(config_path=None, *, store=None):
        calls.append({"store": store})
        # Return a plain default config; the resolver itself is unit-tested
        # elsewhere. Here we only assert the store is threaded through.
        cfg = DispatchConfig()
        # Loops run on the checkout escape hatch until #1844; canonical refuses.
        cfg.publication.publish = "checkout"
        cfg.sandbox.dangerously_skip_containment = True
        return cfg

    monkeypatch.setattr("kew.cli.load_config", _tracking_load_config)
    return calls, real_load_config


def test_dispatch_node_child_resolves_config_with_store(monkeypatch, tmp_path):
    """The detached ``_dispatch-node`` child resolves config WITH the store.

    Before O1 the child called ``load_config()`` with no store, silently
    dropping every DB override. Assert at least one resolution threads the
    store through so overrides reach the child's budget preflight.
    """
    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    calls, _ = _wiring_patches(monkeypatch)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.provider_from_config", lambda config: "github")
    monkeypatch.setattr("kew.cli.validate_item_id", lambda item, provider: item)
    monkeypatch.setattr("kew.cli.build_notifier", lambda config: None)

    captured: dict = {}

    def _fake_select(*, config, force_issue):
        return {"number": force_issue, "title": "t", "body": "b"}

    def _fake_execute(**kwargs):
        from kew.overlap import DispatchOutcome

        captured["config"] = kwargs["config"]
        # The child reads the outcome's exit code and exits with it (#1658), so
        # the double has to return a real outcome, not None.
        return DispatchOutcome(exit_code=0, changed_files=[])

    monkeypatch.setattr("kew.cli.select_next_issue", _fake_select)
    monkeypatch.setattr("kew.cli.execute_dispatch", _fake_execute)

    result = runner.invoke(app, ["epic", "_dispatch-node", "epic-x", "1"])
    assert result.exit_code == 0, result.output
    assert any(c["store"] is not None for c in calls), (
        "epic _dispatch-node must resolve config with store= so DB overrides "
        "govern the child's budget preflight"
    )


def test_epic_driver_resolves_config_with_store(monkeypatch, tmp_path):
    """The epic driver (`kew epic advance`) resolves config WITH the store.

    The driver-side budget-hold reconciliation reads ``config.budget.*``; it is
    only correct if the config it was handed already has DB overrides applied.
    """
    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    calls, _ = _wiring_patches(monkeypatch)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._log_project_banner", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._epic_repo", lambda config: "owner/repo")

    captured: dict = {}

    def _fake_sources(config, *, repo, epic_prefix, store):
        return (object(), object(), None)

    def _fake_advance(name, *, config, **kwargs):
        captured["config"] = config
        from kew.graph.driver.result import TickResult

        return TickResult.__new__(TickResult)

    monkeypatch.setattr("kew.cli._epic_driver_sources", _fake_sources)
    monkeypatch.setattr("kew.cli.advance", _fake_advance)
    monkeypatch.setattr("kew.cli._print_tick_result", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._epic_lock_path", lambda config, name: tmp_path / "l")

    result = runner.invoke(app, ["epic", "advance", "epic-x"])
    assert result.exit_code == 0, result.output
    assert any(c["store"] is not None for c in calls), (
        "kew epic advance must resolve config with store= so DB overrides "
        "govern the driver's budget-hold reconciliation"
    )


# ---------------------------------------------------------------------------
# R1 (#902): every dispatch kind resolves effective config = file + DB override
# ---------------------------------------------------------------------------
#
# The enumeration guard below parametrizes over every budget/enforcement
# dispatch kind. Each kind, once it opens the run store, MUST re-resolve config
# through ``load_config(store=store)`` so a ``config_overrides`` row governs the
# preflight — not the stale kew.toml value. ``load_config`` is doubled with a
# resolver-faithful stub: it raises ``weekly_usd`` ONLY when handed a store,
# mimicking the DB override. A call site that drops the store hands the file
# value straight to its dispatch boundary, and the boundary-capture assertion
# fails — so a future call site can't silently slip back to the file value.

FILE_WEEKLY_USD = 600.0
OVERRIDE_WEEKLY_USD = 1500.0


def _override_tracking_load_config(calls):
    """A ``load_config`` double that applies the DB override (raising
    ``weekly_usd``) only when a ``store=`` is threaded through."""

    def _tracking(config_path=None, *, store=None):
        calls.append({"store": store})
        cfg = DispatchConfig()
        cfg.publication.publish = "checkout"  # loops refuse canonical (#1844)
        cfg.sandbox.dangerously_skip_containment = True
        cfg.budget.weekly_usd = (
            OVERRIDE_WEEKLY_USD if store is not None else FILE_WEEKLY_USD
        )
        return cfg

    return _tracking


@contextlib.contextmanager
def _fake_store_cm(_db_path):
    yield MagicMock(name="run_store")


def _capture_boundary(captured, *, ret=0):
    def _fn(*args, **kwargs):
        captured["config"] = kwargs.get("config", args[0] if args else None)
        return ret

    return _fn


def _patch_dispatch_one(monkeypatch, captured):
    from kew.cli import DispatchCommandResult

    monkeypatch.setattr(
        "kew.cli._dispatch_one",
        _capture_boundary(captured, ret=DispatchCommandResult("executed")),
    )


def _patch_review(monkeypatch, captured):
    monkeypatch.setattr(
        "kew.cli.dispatch_review",
        _capture_boundary(captured, ret=SimpleNamespace(kind="executed", exit_code=0)),
    )


def _patch_resolve(monkeypatch, captured):
    # cli.resolve calls ``_resolve_module.dispatch_resolve``; patch the module attr.
    monkeypatch.setattr(
        "kew.resolve.dispatch_resolve",
        _capture_boundary(captured, ret=SimpleNamespace(kind="executed", exit_code=0)),
    )


def _patch_ci_fix(monkeypatch, captured):
    monkeypatch.setattr("kew.cli.dispatch_ci_fix", _capture_boundary(captured))


def _patch_loop(monkeypatch, captured):
    monkeypatch.setattr(
        "kew.cli.select_next_issue",
        lambda **k: {"number": 1, "title": "t", "body": "b", "labels": []},
    )
    monkeypatch.setattr(
        "kew.cli.resolve_runner",
        lambda config, complexity=None: SimpleNamespace(
            runner=SimpleNamespace(name="claude", loop_style="native"), model=None
        ),
    )
    monkeypatch.setattr(
        "kew.loops.delegate.build_loop_from_issue",
        lambda *a, **k: SimpleNamespace(
            loop_id="L1", budget_usd=0.0, status="pending", status_reason=None
        ),
    )

    class _FakeDriver:
        launched = True

        def __init__(self, store, *, config):
            captured["config"] = config

        def run(self, loop, *, route=None):
            return SimpleNamespace(loop_id="L1", status="achieved", status_reason=None)

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver", _FakeDriver)


def _patch_run_drain(monkeypatch, captured):
    class _FakeScheduler:
        def __init__(self, *, config, store):
            captured["config"] = config
            captured["store"] = store

        def run_loop(self, **kwargs):
            return SimpleNamespace(claim_failures=((42, "fetch_failed"),))

    monkeypatch.setattr("kew.cli.DispatchScheduler", _FakeScheduler)


def _patch_loop_resume(monkeypatch, captured):
    monkeypatch.setattr("kew.cli.preflight_tools", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.bind_policy_context", lambda *a, **k: None)

    class _FakeDriver:
        launched = True

        def __init__(self, store, *, config):
            captured["config"] = config

        def resume(self, *a, **k):
            return SimpleNamespace(loop_id="L1", status="achieved", status_reason=None)

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver", _FakeDriver)


# argv + boundary-patcher for every enforcement dispatch kind.
DISPATCH_KIND_CASES = {
    "dispatch": (["dispatch", "1"], _patch_dispatch_one),
    "run": (["run"], _patch_dispatch_one),
    "run-drain": (["run", "--drain"], _patch_run_drain),
    "review": (["review", "1"], _patch_review),
    "resolve": (["resolve", "1"], _patch_resolve),
    "ci-fix": (["ci-fix", "run-123"], _patch_ci_fix),
    "loop": (["dispatch", "1", "--loop"], _patch_loop),
    "loop-resume": (["loop", "resume", "L1", "--guidance", "g"], _patch_loop_resume),
}


@pytest.mark.parametrize("kind", sorted(DISPATCH_KIND_CASES))
def test_every_dispatch_kind_resolves_config_with_store(kind, monkeypatch, tmp_path):
    """Each enforcement dispatch kind re-resolves config WITH the store.

    A DB override raises ``weekly_usd`` above the file value; the kind's
    dispatch boundary must receive the override value, proving the preflight
    honors it rather than the stale file value.
    """
    from kew.cli import app

    argv, patcher = DISPATCH_KIND_CASES[kind]

    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    captured: dict = {}
    monkeypatch.setattr("kew.cli.load_config", _override_tracking_load_config(calls))
    monkeypatch.setattr("kew.cli._open_run_store", _fake_store_cm)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._emit_staleness_warnings", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._log_budget_banner", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.build_notifier", lambda config: None)
    patcher(monkeypatch, captured)

    result = runner.invoke(app, argv)
    assert result.exit_code == (2 if kind == "run-drain" else 0), result.output

    assert any(c["store"] is not None for c in calls), (
        f"`kew {' '.join(argv)}` must resolve config with store= so DB overrides "
        f"govern its budget preflight"
    )
    assert captured.get("config") is not None, "dispatch boundary was never reached"
    assert captured["config"].budget.weekly_usd == OVERRIDE_WEEKLY_USD, (
        f"kind {kind!r} handed the stale file weekly_usd to its dispatch boundary; "
        f"it must re-resolve load_config(store=store) so the DB override wins"
    )


def test_tui_command_resolves_config_with_store(monkeypatch, tmp_path):
    """`kew tui` hands the store-resolved config to the dashboard.

    TUI action handlers pre-check budget via ``app.config.budget``; that config
    is only correct if it already has DB overrides applied. The ``tui`` command
    must re-resolve ``load_config(store=store)`` before constructing the app.
    """
    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    captured: dict = {}
    monkeypatch.setattr("kew.cli.load_config", _override_tracking_load_config(calls))
    monkeypatch.setattr("kew.cli._open_run_store", _fake_store_cm)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)

    class _FakeApp:
        def __init__(self, *, config, db_path, project_root):
            captured["config"] = config

        def run(self):
            return None

    monkeypatch.setattr("kew.tui.app.KewApp", _FakeApp)

    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0, result.output

    assert any(c["store"] is not None for c in calls), (
        "kew tui must resolve config with store= so DB overrides govern the "
        "TUI's budget pre-checks"
    )
    assert captured.get("config") is not None, "KewApp was never constructed"
    assert captured["config"].budget.weekly_usd == OVERRIDE_WEEKLY_USD


# ---------------------------------------------------------------------------
# #908: repo auto-detect must survive the store re-resolve
# ---------------------------------------------------------------------------
#
# After #905 every dispatch kind re-resolves config via load_config(store=store).
# That creates a fresh DispatchConfig with empty project.repo, losing what
# _detect_repo set on the first load.  The fix re-calls _detect_repo after
# every store re-resolve so the dispatch boundary receives the detected slug.

DETECTED_REPO = "owner/detected-repo"


def _repo_tracking_load_config(calls):
    """Always returns a DispatchConfig with empty project.repo (simulates auto-detect)."""  # noqa: E501

    def _tracking(config_path=None, *, store=None):
        calls.append({"store": store})
        cfg = DispatchConfig()
        cfg.publication.publish = "checkout"  # loops refuse canonical (#1844)
        cfg.sandbox.dangerously_skip_containment = True
        return cfg

    return _tracking


def _fake_detect_repo(config: "DispatchConfig") -> None:
    config.project.repo = DETECTED_REPO


@pytest.mark.parametrize("kind", sorted(DISPATCH_KIND_CASES))
def test_every_dispatch_kind_preserves_repo_after_store_resolve(
    kind, monkeypatch, tmp_path
):
    """After store re-resolve each dispatch kind must re-detect the repo slug.

    load_config(store=store) creates a fresh config with empty project.repo,
    dropping what _detect_repo set on the first load.  _detect_repo must be
    called again after every re-resolve so the dispatch boundary is never handed
    an empty slug (which produces ``gh api 'repos//...'`` failures).
    """
    from kew.cli import app

    argv, patcher = DISPATCH_KIND_CASES[kind]

    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    captured: dict = {}
    monkeypatch.setattr("kew.cli.load_config", _repo_tracking_load_config(calls))
    monkeypatch.setattr("kew.cli._open_run_store", _fake_store_cm)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", _fake_detect_repo)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._emit_staleness_warnings", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._log_budget_banner", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.build_notifier", lambda config: None)
    patcher(monkeypatch, captured)

    result = runner.invoke(app, argv)
    assert result.exit_code == (2 if kind == "run-drain" else 0), result.output

    assert captured.get("config") is not None, "dispatch boundary was never reached"
    assert captured["config"].project.repo == DETECTED_REPO, (
        f"kind {kind!r} handed an empty project.repo to its dispatch boundary; "
        f"_detect_repo must be called after load_config(store=store) so the "
        f"auto-detected slug survives the re-resolve"
    )


def test_tui_command_preserves_repo_after_store_resolve(monkeypatch, tmp_path):
    """`kew tui` must re-detect the repo after the store re-resolve.

    The TUI uses config.project.repo for gh API calls; an empty slug produces
    ``gh api 'repos//...'`` errors.
    """
    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("kew.cli.load_config", _repo_tracking_load_config([]))
    monkeypatch.setattr("kew.cli._open_run_store", _fake_store_cm)
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", _fake_detect_repo)

    class _FakeApp:
        def __init__(self, *, config, db_path, project_root):
            captured["config"] = config

        def run(self):
            return None

    monkeypatch.setattr("kew.tui.app.KewApp", _FakeApp)

    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0, result.output

    assert captured.get("config") is not None, "KewApp was never constructed"
    assert captured["config"].project.repo == DETECTED_REPO, (
        "kew tui handed an empty project.repo to KewApp; _detect_repo must be "
        "called after load_config(store=store)"
    )
