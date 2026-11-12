"""A test steps one sleeper, not the process's ``time.sleep`` (#3176).

``monkeypatch.setattr("kew.run_store.time.sleep", hook)`` reads like a patch
of one module, but ``kew.run_store.time`` is the stdlib ``time`` module, so it
replaces ``time.sleep`` for every caller in the process. A second store's
busy-retry, or any other thread that backed off, re-entered the hook after the
state it set up had moved on, and xdist order decided whether the test passed.

Two rules, read off the syntax tree of every Python file under ``tests/``:

- no patch of ``<kew module>.time.sleep``, in any spelling: a string target
  (``monkeypatch.setattr``, ``patch`` as a call, decorator or context manager,
  ``mocker.patch``), or the module object (``setattr``/``patch.object`` on
  ``kew_module.time``, with the module named through any import alias), and
  whatever the ``monkeypatch`` fixture is called. Patch the sleeper's own hook
  (a store's ``busy_sleep``) instead. A bare stdlib ``"time.sleep"`` target
  is not this rule's: it says what it patches.
- no ``assert`` inside a hook installed over a sleep (any patched name that
  ends in ``sleep``), whether the hook is the patch's value, its
  ``side_effect=``, a lambda that calls the asserting function, or a callable
  handed to a wrapper such as ``OneShotBackoff(...)``. The code under test
  calls the hook from its retry loop, often on another thread, where a failed
  assert reads as that code's error or is lost with the thread. Record the
  outcome; assert in the test body.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from support.suite_sources import suite_source, suite_sources

_KEW_TIME_SLEEP = re.compile(r"^kew(?:\.\w+)*\.time\.sleep$")
_HOOK_KEYWORDS = ("new", "value", "side_effect")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# The sites on main when the rule landed, keyed by file, the function the
# patch sits in (no line numbers, so an edit above a site does not move it)
# and the reason. They are not rewritten here: most patch a sleeper that takes
# no injected hook yet. The rule refuses any site not listed, and an entry
# that no longer matches fails too. THIS LIST ONLY SHRINKS: a site that is
# fixed comes off it, and nothing is ever added to it.
_KNOWN_SITES = frozenset(
    {
        # conftest's suite-wide no-op of the selector's re-query wait.
        (
            "conftest.py",
            "_no_selector_requery_sleep",
            "patches process-wide kew.selector.time.sleep",
        ),
        (
            "test_cli.py",
            "test_fleet_claim_failure_exits_two",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_dispatch_core.py",
            "TestWorkerKilledBehavior.test_killed_run_skips_retry_loop",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_dispatch_core.py",
            "test_bounded_retry_schedule_keeps_deadlines_and_shared_spend",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_drive.py",
            "test_a_stop_during_the_wait_ends_the_drive_without_another_round",
            "assert in sleep, a hook over kew.cli._sleep",
        ),
        (
            "test_drive_host_load.py",
            "test_a_round_with_no_epic_to_tick_still_counts_a_calm_read",
            "assert in sleep, a hook over kew.cli._sleep",
        ),
        (
            "test_drive_host_load.py",
            "test_an_overload_that_begins_during_sleep_holds_a_request_queued_then",
            "assert in sleep, a hook over kew.cli._sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestExactGateMembership.test_a_malformed_provider_payload_returns_through_the_gate",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestExactGateMembership.test_a_number_too_large_for_int_returns_through_the_gate",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestExactGateMembership.test_an_external_boundary_node_does_not_satisfy_the_gate",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestPostCreationGate.test_incomplete_graph_warns_without_vacuous_validate",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestPostCreationGate.test_partial_adoption_exits_nonzero_and_names_only_the_lost_node",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestPostCreationGate.test_provider_read_failure_is_not_reported_as_index_lag",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestPostCreationGate.test_retries_until_graph_complete",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestReadBackFailureModes.test_a_non_runtime_read_error_returns_through_the_gate",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_epic_plan_run.py",
            "TestReadBackFailureModes.test_a_success_then_failures_is_a_read_failure_not_index_lag",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_liveness_owner.py",
            "test_dispatch_stop_intent_prevents_retry_before_finalization",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_liveness_owner.py",
            "test_retry_refusal_preserves_pending_completion",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_proxy_round11.py",
            "test_terminal_category_collision_does_not_retry",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_round26_claim_windows.py",
            "test_round26_failed_launch_evidence_preserves_retention_guard",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_round26_claim_windows.py",
            "test_round26_inner_finalization_preserves_ownership",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_round26_claim_windows.py",
            "test_round26_persistent_retention_keeps_pending_until_reconcile",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_round26_claim_windows.py",
            "test_round26_retention_retries_until_durable",
            "patches process-wide kew.dispatch_core.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestFleetWorkerOutcomes.test_over_cap_refusal_is_reselected_without_empty_queue_exit",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSchedulerDuplicateGuard.test_skips_issue_when_claim_fails",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSchedulerDuplicateGuard.test_skips_issue_when_remote_branch_exists",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSchedulerSkipBackoff.test_skip_backoff_advances_empty_streak_and_exits",
            "assert in stop_loop, a hook over kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSchedulerSkipBackoff.test_skip_backoff_advances_empty_streak_and_exits",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSchedulerStalePendingCleanup.test_run_loop_repeats_stale_pending_cleanup",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_bounded_capacity_exit_waits_while_own_children_run",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_durable_full_waits_log_holders_once_and_preserve_empty_streak",
            "assert in update_holders, a hook over kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_durable_full_waits_log_holders_once_and_preserve_empty_streak",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_issue_hidden_by_pending_run_is_reported_as_held",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_local_capacity_waits_still_honor_fleet_stop_and_breaker",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_local_children_fill_capacity_without_store_access",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_loop_exits_after_bounded_wait_on_a_fleet_held_by_others",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_over_cap_wait_follows_fleet_stop_check",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_pending_probe_runs_once_per_row_per_loop_pass",
            "assert in next_pass, a hook over kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_pending_probe_runs_once_per_row_per_loop_pass",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_pending_row_for_unready_issue_is_not_reported_as_held",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_preview_reaches_selection_when_durable_fleet_is_full",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_scheduler_preserves_aged_unprobeable_and_dead_pending",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_teaching_message_still_prints_after_a_held_first_pass",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "TestSelectExcludesInFlight.test_two_worker_loop_excludes_a_pending_issue_and_uses_one_slot",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "test_drain_retains_potentially_stranded_claims",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "test_round_five_claim_failure_stops_reselection",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "test_round_seven_submission_failures_survive_drain",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "test_round_six_startup_hold_cleanup_retains_failures",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler.py",
            "test_round_ten_selection_failure_drains_active_workers",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_scheduler_resolve.py",
            "TestResolveDispatch.test_dispatches_resolve_for_conflicting_pr",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_w42_round15.py",
            "test_round15_capacity_failure_drains_without_new_claims",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_w42_round15.py",
            "test_round15_capacity_recovery_resumes_claiming",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_w42_round15.py",
            "test_round15_persistent_capacity_failure_exits_two",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_w42_round15.py",
            "test_round15_recovered_loop_claims_on_the_next_pass",
            "patches process-wide kew.scheduler.time.sleep",
        ),
        (
            "test_w42_round18.py",
            "post_creation_command",
            "patches process-wide kew.graph.plan_run.time.sleep",
        ),
        (
            "test_w42_round24.py",
            "test_round24_double_failure_preserves_interruption_and_reaps",
            "patches process-wide kew.dispatch_admission.time.sleep",
        ),
        (
            "test_w42_round24.py",
            "test_round24_finalization_recovers_after_journal_failure",
            "patches process-wide kew.dispatch_admission.time.sleep",
        ),
        (
            "test_w42_round24.py",
            "test_round24_journal_retains_settled_evidence",
            "patches process-wide kew.dispatch_admission.time.sleep",
        ),
        (
            "test_w42_round24.py",
            "test_round24_transient_journal_failure_retries_and_reaps",
            "patches process-wide kew.dispatch_admission.time.sleep",
        ),
    }
)


@dataclass(frozen=True)
class Violation:
    """One broken rule: where it sits and why, plus its line for the message."""

    line: int
    where: str
    reason: str

    def __str__(self) -> str:
        return f"{self.line}: {self.where}: {self.reason}"


@dataclass
class _Scope:
    """The names one function (or the module) binds, for resolving a hook."""

    parent: _Scope | None
    defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = field(
        default_factory=dict
    )
    values: dict[str, list[ast.expr]] = field(default_factory=dict)

    def lookup(
        self, name: str
    ) -> tuple[list[ast.FunctionDef | ast.AsyncFunctionDef], list[ast.expr]]:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.defs or name in scope.values:
                return scope.defs.get(name, []), scope.values.get(name, [])
            scope = scope.parent
        return [], []


def _local_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node of *body*, not descending into a nested scope's body."""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _bind(scope: _Scope, body: list[ast.stmt]) -> None:
    for node in _local_nodes(body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scope.defs.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope.values.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign | ast.NamedExpr)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            scope.values.setdefault(node.target.id, []).append(node.value)


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """The dotted module (or attribute) each imported name stands for."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    # `mod = importlib.import_module("kew.x")` and `t = kew_module.time`.
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and _terminal(value.func) == "import_module"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            aliases[node.targets[0].id] = value.args[0].value
            continue
        dotted = _dotted(value, aliases)
        if dotted is not None and dotted.startswith("kew."):
            aliases.setdefault(node.targets[0].id, dotted)
    return aliases


def _terminal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword(call: ast.Call, *names: str) -> list[ast.expr]:
    return [kw.value for kw in call.keywords if kw.arg in names]


def _object_target(obj: ast.expr, attr: str | None, aliases: dict[str, str]) -> str:
    base = _dotted(obj, aliases) or ast.unparse(obj)
    return f"{base}.{attr}"


def _patch(
    call: ast.Call, aliases: dict[str, str]
) -> tuple[str, list[ast.expr]] | None:
    """``(what the call patches, the hooks it installs)``, or None if no patch.

    Every spelling: ``<anything>.setattr`` and the builtin ``setattr``,
    ``patch`` (bare, ``mock.patch``, ``mocker.patch``) and ``patch.object``.
    """
    func, args = call.func, call.args
    name = _terminal(func)
    if (
        name == "object"
        and isinstance(func, ast.Attribute)
        and _terminal(func.value) == "patch"
    ):
        attr = _string(args[1]) if len(args) > 1 else None
        if not args or attr is None:
            return None
        hooks = args[2:3] + _keyword(call, *_HOOK_KEYWORDS)
        return _object_target(args[0], attr, aliases), hooks
    if name == "patch":
        target = _string(args[0]) if args else None
        target = target or next(
            (_string(value) for value in _keyword(call, "target")), None
        )
        if target is None:
            return None
        return target, args[1:2] + _keyword(call, *_HOOK_KEYWORDS)
    if name == "setattr":
        if args and _string(args[0]) is not None:
            return str(_string(args[0])), args[1:2] + _keyword(call, "value")
        attr = _string(args[1]) if len(args) > 1 else None
        if attr is None:
            return None
        return _object_target(args[0], attr, aliases), args[2:3] + _keyword(
            call, "value"
        )
    return None


def _hook_functions(
    hook: ast.expr, scope: _Scope, seen: set[int]
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda]:
    """Every function a hook runs: itself, what its lambda calls, what it wraps."""
    if id(hook) in seen:
        return
    seen.add(id(hook))
    if isinstance(hook, ast.Name):
        defs, values = scope.lookup(hook.id)
        for definition in defs:
            if id(definition) not in seen:
                seen.add(id(definition))
                yield definition
        for value in values:
            yield from _hook_functions(value, scope, seen)
    elif isinstance(hook, ast.Lambda):
        yield hook
        for node in ast.walk(hook.body):
            if isinstance(node, ast.Name):
                yield from _hook_functions(node, scope, seen)
    elif isinstance(hook, ast.Call):
        # A wrapper (``OneShotBackoff(action)``, ``partial(f)``) runs what it
        # is handed when the hook is called.
        for argument in [*hook.args, *(kw.value for kw in hook.keywords)]:
            yield from _hook_functions(argument, scope, seen)


class _Checker:
    def __init__(self, tree: ast.Module) -> None:
        self.aliases = _module_aliases(tree)
        self.found: list[Violation] = []
        module = _Scope(parent=None)
        _bind(module, tree.body)
        for statement in tree.body:
            self._visit(statement, module, [])

    def _visit(self, node: ast.AST, scope: _Scope, names: list[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            inner = [*names, node.name]
            # Decorators run in the enclosing scope, but patch the function.
            for decorator in node.decorator_list:
                self._visit(decorator, scope, inner)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self._visit(default, scope, names)
            body = _Scope(parent=scope)
            _bind(body, node.body)
            for statement in node.body:
                self._visit(statement, body, inner)
            return
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                self._visit(decorator, scope, [*names, node.name])
            # A method resolves names past its class, in the enclosing scope.
            for statement in node.body:
                self._visit(statement, scope, [*names, node.name])
            return
        if isinstance(node, ast.Call):
            self._check(node, scope, ".".join(names) or "<module>")
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope, names)

    def _check(self, call: ast.Call, scope: _Scope, where: str) -> None:
        patched = _patch(call, self.aliases)
        if patched is None:
            return
        target, hooks = patched
        if _KEW_TIME_SLEEP.match(target):
            self.found.append(
                Violation(call.lineno, where, f"patches process-wide {target}")
            )
        if not target.rsplit(".", 1)[-1].endswith("sleep"):
            return
        seen: set[int] = set()
        for hook in hooks:
            for function in _hook_functions(hook, scope, seen):
                if any(isinstance(n, ast.Assert) for n in ast.walk(function)):
                    named = getattr(function, "name", "<lambda>")
                    self.found.append(
                        Violation(
                            call.lineno,
                            where,
                            f"assert in {named}, a hook over {target}",
                        )
                    )


def sleep_patch_violations(source: str) -> list[Violation]:
    """Every rule broken in one module's *source*."""
    return _Checker(ast.parse(source)).found


def _reasons(source: str) -> list[str]:
    return [f"{v.where}: {v.reason}" for v in sleep_patch_violations(source)]


# --- negative controls: every spelling is refused ---------------------------

_GLOBAL_PATCHES = {
    "monkeypatch.setattr with a string target": (
        """
def test_backoff(monkeypatch):
    monkeypatch.setattr("kew.run_store.time.sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "a monkeypatch fixture under another name": (
        """
def test_backoff(mp):
    mp.setattr("kew.run_store.time.sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "setattr on an aliased kew module's time": (
        """
import kew.run_store as rs

def test_backoff(monkeypatch):
    monkeypatch.setattr(rs.time, "sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "setattr on a module imported from kew": (
        """
from kew import run_store

def test_backoff(monkeypatch):
    monkeypatch.setattr(run_store.time, "sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "setattr on a dotted kew module's time": (
        """
import kew.run_store

def test_backoff(monkeypatch):
    monkeypatch.setattr(kew.run_store.time, "sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "the builtin setattr on a kew module's time": (
        """
from kew import run_store

def test_backoff():
    setattr(run_store.time, "sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
    "patch with a string target, called": (
        """
from unittest.mock import patch

def test_backoff():
    patcher = patch("kew.scheduler.time.sleep")
    patcher.start()
""",
        "kew.scheduler.time.sleep",
    ),
    "patch as a context manager": (
        """
from unittest import mock

def test_backoff():
    with mock.patch("kew.scheduler.time.sleep"):
        pass
""",
        "kew.scheduler.time.sleep",
    ),
    "patch as a decorator": (
        """
from unittest.mock import patch

@patch("kew.scheduler.time.sleep")
def test_backoff(sleep):
    pass
""",
        "kew.scheduler.time.sleep",
    ),
    "mocker.patch": (
        """
def test_backoff(mocker):
    mocker.patch("kew.scheduler.time.sleep")
""",
        "kew.scheduler.time.sleep",
    ),
    "patch.object on an aliased kew module's time": (
        """
from unittest.mock import patch
import kew.scheduler as scheduler

def test_backoff():
    with patch.object(scheduler.time, "sleep"):
        pass
""",
        "kew.scheduler.time.sleep",
    ),
    "a name bound to a kew module's time": (
        """
from kew import run_store

def test_backoff(monkeypatch):
    clock = run_store.time
    monkeypatch.setattr(clock, "sleep", lambda _delay: None)
""",
        "kew.run_store.time.sleep",
    ),
}


@pytest.mark.parametrize("spelling", sorted(_GLOBAL_PATCHES))
def test_every_spelling_of_a_process_wide_kew_sleep_patch_is_refused(
    spelling: str,
) -> None:
    source, target = _GLOBAL_PATCHES[spelling]
    assert _reasons(source) == [f"test_backoff: patches process-wide {target}"]


_ASSERTING_HOOKS = {
    "a function installed by monkeypatch.setattr": (
        """
def test_backoff(store, monkeypatch):
    def hook(_delay):
        assert store.ready()

    monkeypatch.setattr(store, "busy_sleep", hook)
""",
        "assert in hook, a hook over store.busy_sleep",
    ),
    "a side_effect on patch.object": (
        """
from unittest.mock import patch

def test_backoff(store):
    def guard(_delay):
        assert store.ready()

    with patch.object(store, "busy_sleep", side_effect=guard):
        store.save()
""",
        "assert in guard, a hook over store.busy_sleep",
    ),
    "a lambda that calls the asserting function": (
        """
def test_backoff(store, monkeypatch):
    def check(delay):
        assert delay < 1

    monkeypatch.setattr(store, "busy_sleep", lambda delay: check(delay))
""",
        "assert in check, a hook over store.busy_sleep",
    ),
    "an action wrapped in OneShotBackoff, by name": (
        """
def test_backoff(store, monkeypatch):
    def claim():
        assert store.claim()

    backoff = OneShotBackoff(claim)
    monkeypatch.setattr(store, "busy_sleep", backoff)
""",
        "assert in claim, a hook over store.busy_sleep",
    ),
    "an action wrapped in OneShotBackoff, inline": (
        """
def test_backoff(store, monkeypatch):
    def claim():
        assert store.claim()

    monkeypatch.setattr(store, "busy_sleep", OneShotBackoff(claim))
""",
        "assert in claim, a hook over store.busy_sleep",
    ),
    "a stdlib sleep hook": (
        """
import time

def test_backoff(monkeypatch):
    def hook(_delay):
        assert False

    monkeypatch.setattr(time, "sleep", hook)
""",
        "assert in hook, a hook over time.sleep",
    ),
}


@pytest.mark.parametrize("spelling", sorted(_ASSERTING_HOOKS))
def test_an_assert_inside_a_sleep_hook_is_refused(spelling: str) -> None:
    source, reason = _ASSERTING_HOOKS[spelling]
    assert _reasons(source) == [f"test_backoff: {reason}"]


def test_a_side_effect_over_a_kew_sleep_breaks_both_rules() -> None:
    source = """
from unittest.mock import patch

def _guard(_delay):
    assert False, "looped"

class TestLoop:
    @patch("kew.scheduler.time.sleep", side_effect=_guard)
    def test_backoff(self, sleep):
        pass
"""
    assert _reasons(source) == [
        "TestLoop.test_backoff: patches process-wide kew.scheduler.time.sleep",
        "TestLoop.test_backoff: assert in _guard, a hook over kew.scheduler.time.sleep",
    ]


def test_a_hook_resolves_to_its_own_scope_not_a_namesake() -> None:
    source = """
def test_other():
    def hook(_delay):
        assert False

def test_backoff(store, monkeypatch):
    def hook(_delay):
        return None

    monkeypatch.setattr(store, "busy_sleep", hook)
"""
    assert _reasons(source) == []


@pytest.mark.parametrize(
    "source",
    [
        # A per-store hook that records, asserted in the body.
        """
def test_backoff(store, monkeypatch):
    backoff = OneShotBackoff(store.ready)
    monkeypatch.setattr(store, "busy_sleep", backoff)
    store.save()
    assert backoff.outcomes == [True]
""",
        # A bare stdlib target names what it patches: out of this rule's scope.
        """
import time

def test_backoff(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _delay: None)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
""",
        # A kew module's own ``sleep`` binding is local to that module.
        """
def test_backoff(monkeypatch):
    monkeypatch.setattr("kew.run_store.sleep", lambda _delay: None)
""",
        # A mock with no hook over a store's own sleeper.
        """
from unittest.mock import patch

def test_backoff(store):
    with patch.object(store, "busy_sleep") as sleep:
        store.save()
    assert sleep.call_count == 1
""",
    ],
)
def test_a_local_recording_patch_passes(source: str) -> None:
    assert _reasons(source) == []


# --- the tree ----------------------------------------------------------------


@pytest.mark.policy
def test_the_rewritten_busy_retry_sites_pass() -> None:
    for name in (
        "test_run_store_epic.py",
        "test_run_store.py",
        "test_driver_review_stage.py",
    ):
        assert sleep_patch_violations(suite_source(name)) == [], name


def unlisted_and_fixed(
    sources: list[tuple[str, str]], known: frozenset[tuple[str, str, str]]
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """The sites *known* does not list, and the *known* entries no site matches."""
    found: dict[tuple[str, str, str], list[str]] = {}
    for relative, source in sources:
        for violation in sleep_patch_violations(source):
            key = (relative, violation.where, violation.reason)
            found.setdefault(key, []).append(f"{relative}:{violation}")
    unlisted = sorted(
        line for key, lines in found.items() if key not in known for line in lines
    )
    return unlisted, sorted(known - found.keys())


_KNOWN_FIXTURE = """
def test_old(monkeypatch):
    monkeypatch.setattr("kew.scheduler.time.sleep", lambda _delay: None)
"""

_KNOWN_KEY = (
    "test_old.py",
    "test_old",
    "patches process-wide kew.scheduler.time.sleep",
)


def test_a_new_site_beside_a_known_one_is_refused() -> None:
    added = _KNOWN_FIXTURE + _GLOBAL_PATCHES["mocker.patch"][0]
    assert unlisted_and_fixed([("test_old.py", added)], frozenset({_KNOWN_KEY})) == (
        ["test_old.py:6: test_backoff: patches process-wide kew.scheduler.time.sleep"],
        [],
    )


def test_a_known_site_that_was_fixed_must_leave_the_list() -> None:
    fixed = "def test_old(store, monkeypatch):\n    pass\n"
    assert unlisted_and_fixed([("test_old.py", fixed)], frozenset({_KNOWN_KEY})) == (
        [],
        [_KNOWN_KEY],
    )


def test_a_known_site_still_there_passes() -> None:
    assert unlisted_and_fixed(
        [("test_old.py", _KNOWN_FIXTURE)], frozenset({_KNOWN_KEY})
    ) == ([], [])


@pytest.mark.policy
def test_no_new_test_patches_a_process_wide_sleep_or_asserts_in_a_sleep_hook() -> None:
    unlisted, fixed = unlisted_and_fixed(list(suite_sources()), _KNOWN_SITES)
    assert unlisted == [], "\n".join(unlisted)
    assert fixed == [], f"take these fixed sites off _KNOWN_SITES: {fixed}"
