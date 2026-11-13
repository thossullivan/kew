"""Shared test fixtures for kew."""

import atexit
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support import (
    group_leaks,
    repo_inventory,
    required_tools,
    serial_phase,
    shard,
    tree_scan,
)

# -- Hermetic log dir: no test may touch the repo-local dispatch.db --
#
# ~20 CLI/integration tests invoke the CLI with a bare ``DispatchConfig()``,
# whose default ``logging.dir`` is the CWD-relative ``./logs/dispatch`` — i.e.
# the *repo-local* ``logs/dispatch/dispatch.db`` when the suite runs from
# ``kew/``. That made the suite depend on filesystem state left by *previous*
# runs of *other* code: a newer branch's schema migration auto-upgraded the DB,
# then every older branch died on ``SchemaTooNewError`` (SFD-134 / kew #784).
#
# We redirect the *default* log dir via ``config._DEFAULT_LOG_DIR_OVERRIDE`` —
# not ``KEW_LOG_DIR``. KEW_LOG_DIR is an env *override* that load_config layers
# on top of any kew.toml value, so setting it would clobber the tests that
# deliberately configure their own ``logging.dir`` (health, approve). The
# override is a pure default: it only takes effect when nothing else set the
# dir, so those tests keep their configured dir.
#
# The session-wide baseline is set at *import time* (before test modules are
# collected) so decorator-evaluated configs — ``@patch(..., return_value=
# DispatchConfig())`` builds the config when the decorator runs, not when the
# test does — are already pointed off-repo. The per-test autouse fixture then
# narrows each test to its own ``tmp_path``.
import kew.config as _kew_config
import kew.repo_host as _kew_repo_host
import kew.subprocess_utils as _kew_subprocess_utils
import kew.work_dirs as _kew_work_dirs

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_DISPATCH_DB = _REPO_ROOT / "logs" / "dispatch" / "dispatch.db"
_SESSION_LOG_DIR = tempfile.mkdtemp(prefix="kew-test-logs-")
_SESSION_WORKTREE_DIR = tempfile.mkdtemp(prefix="kew-test-worktrees-")
_kew_config._DEFAULT_LOG_DIR_OVERRIDE = _SESSION_LOG_DIR
_kew_config._DEFAULT_WORKTREE_BASE_OVERRIDE = _SESSION_WORKTREE_DIR

# Created at import, before any fixture can redirect the temp root, so it is the
# one directory the suite must remove for itself. It never was: 1,613 of these
# had accumulated on the CI runner, one per session since the fixture landed.
atexit.register(shutil.rmtree, _SESSION_LOG_DIR, True)
atexit.register(shutil.rmtree, _SESSION_WORKTREE_DIR, True)


# -- No operator secret is readable from a test process (#2197) --
#
# A failing mock assertion prints the call the mock actually received, and the
# container launcher passes the *process* environment straight through
# (``launch_environment(os.environ, ...)``). So a one-line regression in
# ``popen.assert_called_once_with(argv, env=ANY, ...)`` printed the operator's
# live LINEAR_API_KEY, SSH_AUTH_SOCK and the rest of the host environment into
# a full-gate pytest log on 2026-09-29.
#
# Nothing in this suite needs a real credential: every test that wants one sets
# its own with ``monkeypatch.setenv``, which layers on top of this removal. So
# the whole class is removed once, for the whole run, and what a failure can
# print is then fixture values only.
#
# Removal happens at import, before collection, so even a collection-time
# error cannot print them. The session-scoped fixture below re-applies it and
# is where a reader looks for the rule; a test that wants a credential still
# sets its own, because that happens later and is scoped to the test.

_SECRET_ENV_PREFIXES = ("GH_", "GITHUB_", "LINEAR_", "ANTHROPIC_", "OPENROUTER_")
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
_SECRET_ENV_NAMES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})


def is_secret_env_name(name: str) -> bool:
    """Whether an environment variable name is one the suite must never hold."""
    return (
        name in _SECRET_ENV_NAMES
        or name.startswith(_SECRET_ENV_PREFIXES)
        or name.endswith(_SECRET_ENV_SUFFIXES)
    )


# -- ...except the one round that must sweep for the operator's own value --
#
# The AC7 live OpenShell round (``tests/test_openshell_review.py``) asserts that
# no real credential reached the sandbox it created. The only real value it can
# look for is the operator's own — and the removal above had already deleted
# exactly the names ``runner_credential_env_vars("claude-code")`` declares by
# the time the round ran, so its sweep iterated an empty set and could not fail
# (PR #2520 review). The values are therefore held back as they are removed,
# for that round to read.
#
# Gated on the same opt-in as the round itself: for every ordinary run nothing
# is held and the #2197 guarantee above is unweakened. Even for the live run
# the hold is as narrow as the round needs (#2638):
#
# - only the names the round sweeps for, not every secret-looking variable the
#   operator's shell exports — a LINEAR_API_KEY is nothing the sweep looks for;
# - reachable only through the ``live_round_credentials`` fixture, which takes
#   the values OUT of this module when the round starts and clears its own copy
#   when the round ends — there is no accessor a module can import and call;
# - dropped at the end of collection when no collected test requests that
#   fixture, so a live-opted run that deselects the round holds nothing at all.
#
# Never back in ``os.environ``, so they still cannot reach a container through
# ``launch_environment(os.environ)`` or a printed mock call.
_LIVE_ROUND_OPT_IN = "KEW_LIVE_OPENSHELL"
_LIVE_ROUND_FIXTURE = "live_round_credentials"
_held_for_live_round: dict[str, str] = {}


def _live_round_swept_names() -> frozenset[str]:
    """The variables the AC7 round sweeps its sandbox for: claude-code's own."""
    from kew.sandbox import runner_credential_env_vars

    return frozenset(runner_credential_env_vars("claude-code"))


def _scrub_secret_env() -> list[str]:
    """Remove every secret-looking variable from ``os.environ``; name them."""
    removed = [name for name in os.environ if is_secret_env_name(name)]
    held = (
        _live_round_swept_names()
        if os.environ.get(_LIVE_ROUND_OPT_IN) == "1"
        else frozenset()
    )
    for name in removed:
        if name in held:
            _held_for_live_round[name] = os.environ[name]
        del os.environ[name]
    return removed


def _release_live_round_credentials() -> dict[str, str]:
    """Move the held values out of this module, leaving nothing behind."""
    taken = dict(_held_for_live_round)
    _held_for_live_round.clear()
    return taken


@pytest.fixture
def live_round_credentials() -> Generator[dict[str, str]]:
    """The operator credentials removed from a live-round run's environment.

    Empty unless this run opted in to the live OpenShell round, which is the
    only requester: see the comment above. The first request takes the values;
    any later one gets an empty mapping, which the round refuses rather than
    sweeping for nothing.
    """
    taken = _release_live_round_credentials()
    try:
        yield taken
    finally:
        taken.clear()


def pytest_collection_finish(session: pytest.Session) -> None:
    """Drop the live-round hold when nothing collected will ever take it."""
    if not any(
        _LIVE_ROUND_FIXTURE in getattr(item, "fixturenames", ())
        for item in session.items
    ):
        _release_live_round_credentials().clear()


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Whatever the round did not take is not kept past the session either."""
    _release_live_round_credentials().clear()


_scrub_secret_env()


@pytest.fixture(scope="session", autouse=True)
def _worker_owns_its_group() -> None:
    """Give each xdist worker a process group of its own, from its first test.

    Admission does this anyway (``exclusive_owner_pid``), but only on the
    worker an admission test lands on, and only from then on. Until then the
    worker shares its group with the controller and every sibling, so the
    guard below would read their children as this test's. Doing it up front
    makes the guard's group this worker's alone, whatever order the tests run
    in. Only workers: a plain run may own the terminal, and leaving the
    foreground group would stop it at its first read.
    """
    if os.environ.get("PYTEST_XDIST_WORKER") and os.getpgrp() != os.getpid():
        os.setpgid(0, 0)


@pytest.fixture(autouse=True)
def _no_child_left_in_the_group(_worker_owns_its_group: None) -> Generator[None]:
    """Fail the test that leaves a live process in this worker's group (#2563).

    An admission refuses while any non-descendant shares the dispatch group, so
    an orphan one test leaves behind fails whichever admission tests later land
    on the same worker — a different set each run. Failing here names the test
    and the process that leaked, and killing it keeps the next test clean.

    Defined ahead of every other autouse fixture so that it is set up before
    ``monkeypatch`` and torn down after it: its reads then run against the real
    ``os`` and ``/proc``, not a test's fakes of them.
    """
    before = group_leaks.group_members()
    yield
    leaked = group_leaks.left_behind(before)
    if leaked:
        group_leaks.reap(leaked)
        pytest.fail(
            "left a live process in the worker's process group, which every "
            "later admission on this worker refuses as a foreign peer: "
            f"{group_leaks.describe(leaked)}. Start it in its own session, and "
            "kill and wait for it in teardown.",
            pytrace=False,
        )


@pytest.fixture(scope="session", autouse=True)
def _no_operator_secrets_in_env() -> None:
    """Keep known secret variables out of ``os.environ`` for the whole run."""
    _scrub_secret_env()


# -- No test reaches the operator's gh (#3069) --
#
# ``preflight_tools()`` registers whatever ``gh`` is on PATH, so a test that
# drives a real tick without faking the forge shells the operator's
# authenticated ``gh``. Review-stage tests did exactly that: they posted a
# commit status and a PR comment to GitHub for the placeholder repo ``o/r``,
# which 404s, and the best-effort publishers swallowed the failure, so the
# tests passed. A fixture naming a real repo would have written to it.
#
# The ``gh`` on PATH when the session starts is the operator's. A test fakes
# the forge by replacing the ``run``/``Popen`` it calls or by putting its own
# stub ``gh`` on PATH; anything that still launches the session's ``gh`` is
# refused before it spawns, and the test fails naming itself. ``shutil.which``
# is read here, at import, before a fixture can wrap it.
_real_which = shutil.which
_SESSION_GH = _real_which("gh")
_OPERATOR_GH = os.path.realpath(_SESSION_GH) if _SESSION_GH else None


def _launch_target(args: Any, executable: Any, env: Any) -> str | None:
    """The executable a ``Popen`` is about to start, as a real path."""
    target = ""
    if executable is not None:
        target = os.fsdecode(executable)
    elif isinstance(args, str | bytes | os.PathLike):
        with contextlib.suppress(ValueError, IndexError):
            target = shlex.split(os.fsdecode(args))[0]
    elif argv := list(args):
        target = os.fsdecode(argv[0])
    if not target:
        return None
    if os.sep not in target:
        search = env.get("PATH", env.get(b"PATH")) if env else None
        found = _real_which(target, path=os.fsdecode(search) if search else None)
        if found is None:
            return None
        target = found
    return os.path.realpath(target)


@pytest.fixture(autouse=True)
def _no_operator_gh(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[None]:
    """Refuse, and fail the test for, any launch of the session's real ``gh``."""
    if _OPERATOR_GH is None:
        yield
        return
    refused: list[str] = []
    launch = subprocess.Popen._execute_child  # type: ignore[attr-defined]

    def _guarded(self: Any, args: Any, executable: Any, *rest: Any) -> Any:
        # rest: preexec_fn, close_fds, pass_fds, cwd, env, ...
        if _launch_target(args, executable, rest[4]) == _OPERATOR_GH:
            argv = [args] if isinstance(args, str | bytes) else list(args)
            refused.append(" ".join(["gh", *map(os.fsdecode, argv[1:5])]))
            raise PermissionError(
                f"{request.node.nodeid} launched the operator's gh "
                f"({_OPERATOR_GH}): {args!r}. Fake the forge: replace the "
                "run/Popen it calls, or put a stub gh on PATH."
            )
        return launch(self, args, executable, *rest)

    monkeypatch.setattr(subprocess.Popen, "_execute_child", _guarded)
    yield
    if refused:
        pytest.fail(
            f"{request.node.nodeid} launched the operator's gh "
            f"({_OPERATOR_GH}) {len(refused)} time(s), refused: "
            f"{'; '.join(refused)}. Tests reach GitHub only through a fake: "
            "replace the run/Popen the code calls, or put a stub gh on PATH.",
            pytrace=False,
        )


#: What the forge answers ``gh repo view --json url`` with, for the host only.
_FORGE_REPO_VIEW = '{"url": "https://github.com/owner/repo"}\n'


@pytest.fixture(autouse=True)
def _repo_host_reads_no_operator_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer :func:`kew.repo_host.repo_host`'s ``gh repo view`` as the forge
    would, where it would otherwise launch the operator's ``gh``.

    ``kew epic status`` and ``kew graph status`` resolve the forge host from
    the checkout's ``origin``, and fall back to ``gh repo view`` when origin
    names no forge, as in the bundle clone a local verify runs in. Which test
    got there first was down to xdist order, because the answer is cached for
    the process. Here the cache starts empty for every test, ``git remote
    get-url`` stays real, and only a launch of the session's own ``gh`` —
    the one :func:`_no_operator_gh` refuses — is answered instead. A stub
    ``gh`` a test puts on PATH, a ``run_fn`` it passes, or a ``run`` it
    patches still decides, so a test of a ``gh`` failure still sees one.
    """
    monkeypatch.setattr(_kew_repo_host, "_REPO_HOST_BY_ROOT", {})
    if _OPERATOR_GH is None:
        return
    launch = _kew_repo_host.run

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "repo", "view"]:
            with contextlib.suppress(ValueError):
                resolved = _kew_subprocess_utils.resolve_cmd(cmd)[0]
                if os.path.realpath(resolved) == _OPERATOR_GH:
                    return subprocess.CompletedProcess(cmd, 0, _FORGE_REPO_VIEW, "")
        return launch(cmd, **kwargs)

    monkeypatch.setattr(_kew_repo_host, "run", _run)


@pytest.fixture(autouse=True)
def _hermetic_readiness_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep operator secrets out of the real config directory during tests."""
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(tmp_path / "readiness.key"))


@pytest.fixture(autouse=True)
def _hermetic_host_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep per-host locks in the test's own directory, never the operator's.

    The lock directory is per operator user, not per project (#2627), so two
    tests addressing the same fake instance would otherwise contend for one
    real lock file under the home directory.
    """
    monkeypatch.setenv("KEW_HOST_LOCK_DIR", str(tmp_path / "host-locks"))


@pytest.fixture(autouse=True)
def _hermetic_work_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the trees kew makes out of the operator's ``~/.local/state/kew``.

    Beside ``tmp_path``, not in it, so a test that lists ``tmp_path`` sees
    only what it made itself. The configured value is pinned too, so a test
    that replaces ``os.environ`` wholesale still lands in the same root.
    """
    root = str(tmp_path_factory.mktemp("kew-work-root"))
    monkeypatch.setenv("KEW_WORK_ROOT", root)
    monkeypatch.setattr(_kew_work_dirs, "_configured", root)
    monkeypatch.setattr(_kew_work_dirs, "_ledger", None)


@pytest.fixture(autouse=True)
def _hermetic_codex_login(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the publication scan at a placeholder Codex login, never the host's.

    A native run's scan needs a readable login (round 12); without this the
    scan would read the operator's ``~/.codex/auth.json``. The login sits
    beside ``tmp_path``, not in it, so tests that list ``tmp_path`` see nothing.
    """
    home = tmp_path_factory.mktemp("hermetic-codex-home")
    (home / "auth.json").write_text(
        '{"tokens": {"access_token": "kew-placeholder-hermetic-codex-0123"}}'
    )
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.fixture(autouse=True)
def _hermetic_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point each test's default kew log dir at an isolated tmp dir.

    Narrows the session-wide baseline so tests don't share a dispatch.db.
    Tests that build the config at *decorator* time still fall back to the
    session dir set at import — both are outside the repo. Tests that set an
    explicit ``logging.dir`` (kew.toml or ``cfg.logging.dir = ...``) are
    unaffected: this is a default, not an override.
    """
    monkeypatch.setattr(
        _kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", str(tmp_path / "kew-logs")
    )


@pytest.fixture(autouse=True)
def _hermetic_worktree_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point each test's default checkout root at its own temporary directory."""
    monkeypatch.setattr(
        _kew_config,
        "_DEFAULT_WORKTREE_BASE_OVERRIDE",
        str(tmp_path / "kew-worktrees"),
    )


@pytest.fixture(autouse=True)
def _hermetic_temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own temp root, so a bare ``mkdtemp`` cannot leak.

    kew's two checkout primitives — ``materialize_reviewer_worktree`` and
    ``dispatch_core._materialize_checkout`` — call ``tempfile.mkdtemp`` with no
    ``dir=``, so they land in the *machine's* temp root. Production removes
    them (``review_stage`` in a ``finally``, the dispatch path on completion);
    tests call the primitives directly and hand the result to no one, so
    nothing ever removed theirs. Six days of CI left 3,284 ``kew-review-*`` and
    333 ``kew-sandbox-*`` clones on the runner, growing by ten per suite run
    (#1480).

    Redirecting rather than policing is deliberate. A guard that deletes stray
    ``kew-review-*`` from the machine's temp root would, on this host, be able
    to delete a *live* reviewer checkout belonging to a concurrent real
    dispatch — the CI runner and the dev machine are the same box. Pointing the
    suite's temp root at ``tmp_path`` means there is nothing to police: the
    clones land where pytest already bounds retention, and each one is
    attributable to the test that made it.

    Only Python's ``tempfile.tempdir`` is moved, not ``TMPDIR`` — every creator
    in the leak goes through ``tempfile``, and rewriting the environment would
    redirect subprocesses (git, docker) that were never part of the problem.
    """
    temp_root = tmp_path / "tmp"
    temp_root.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))


@pytest.fixture(autouse=True)
def _hermetic_cli_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep managed CLI registries and audit anchors out of the real checkout."""
    monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _restore_cli_project_env() -> Generator[None]:
    """Do not let Typer's ``--project`` callback leak across CLI tests."""
    original = os.environ.get("KEW_PROJECT")
    yield
    if original is None:
        os.environ.pop("KEW_PROJECT", None)
    else:
        os.environ["KEW_PROJECT"] = original


@pytest.fixture(autouse=True)
def _restore_cwd() -> Generator[None]:
    """Restore the working directory after every test.

    A test that changes cwd without restoring (a bare ``os.chdir`` on an error
    path, or a helper that forgets to reset) leaks it to every later test in the
    session, and cwd-sensitive paths then resolve the wrong repo. Snapshot and
    restore here so no test can leak cwd regardless of its own discipline. This
    is a backstop for that class only: the #1631 flake was a different leak,
    the executable allowlist, handled by ``_hermetic_tool_allowlist`` below.
    """
    cwd = os.getcwd()
    yield
    with contextlib.suppress(OSError):
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _hermetic_tool_allowlist() -> Generator[None]:
    """Snapshot and restore the subprocess allowlist around every test (#1631).

    ``kew.subprocess_utils.TOOL_PATHS`` is the process-global executable
    allowlist. It starts empty; ``preflight_tools()`` fills it with the real
    ``gh`` / ``git`` / runner binaries, and nothing ever empties it. A test that
    reaches a real preflight (``tests/test_certification_*.py`` run
    ``scripts/certification_conformance.py::main`` in-process) therefore leaves
    the real ``gh`` registered for every later test in the session.

    That was the #1631 flake. With ``gh`` unlisted, ``run(["gh", ...])`` raises
    before spawning anything, so ``check_pr_exists`` raises and an exit-0 host
    dispatch is marked complete. With the real ``gh`` leaked in, the same call
    is a live ``gh api`` lookup against the checkout's origin; it finds no PR,
    returns ``None``, and dispatch brokers the mocked worktree -- exit 1.

    Restore in place rather than rebinding: importers hold the dict itself
    (``from kew.subprocess_utils import TOOL_PATHS``).
    """
    from kew.subprocess_utils import TOOL_PATHS

    saved = dict(TOOL_PATHS)
    yield
    TOOL_PATHS.clear()
    TOOL_PATHS.update(saved)


@pytest.fixture
def real_ci_approval_revoke() -> None:
    """Opt out of the suite's stubbed CI-approval revoke."""


# Every module that binds kew.dispatch_core.revoke_ci_approval by name.
_CI_APPROVAL_REVOKE_BINDINGS = (
    "kew.dispatch_core",
    "kew.review",
    "kew.ci_fix",
    "kew.resolve",
)


@pytest.fixture(autouse=True)
def _hermetic_ci_approval_revoke(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the pre-push CI-approval revoke (#2033) off GitHub.

    Every host push to an agent PR branch first asks GitHub for the branch's
    open PRs and removes the approval label. That is a live REST call, and it
    fails closed, so a publication test that does not care about approval
    would fail on it. Its own tests opt out with ``real_ci_approval_revoke``.
    """
    if "real_ci_approval_revoke" in request.fixturenames:
        return
    import importlib

    for name in _CI_APPROVAL_REVOKE_BINDINGS:
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "revoke_ci_approval", lambda *_a, **_k: ())


@pytest.fixture
def real_daemon_cpu_probe() -> None:
    """Opt out of the suite's stubbed container CPU-cap probe."""


@pytest.fixture(autouse=True)
def _hermetic_daemon_cpu_probe(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the container CPU-cap probe off the host daemon.

    ``resource_limit_args`` asks the runtime for its CPU count once per launch
    (#1817). A test that scripts ``subprocess_utils.run`` with an ordered
    sequence must not see that extra call, and no test may depend on the
    host's daemon. Four is the ceiling, so the argv is what it was before the
    probe existed. The clamp's own tests opt out with ``real_daemon_cpu_probe``.
    """
    if "real_daemon_cpu_probe" in request.fixturenames:
        return

    from kew import sandbox

    monkeypatch.setattr(sandbox, "_daemon_cpu_count", lambda runtime: 4)


@pytest.fixture
def real_runner_preflight() -> None:
    """Opt out of the suite's installed-runner baseline."""


@pytest.fixture(autouse=True)
def _hermetic_runner_binaries(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make preflight discovery independent from tools installed on the host."""
    if "real_runner_preflight" in request.fixturenames:
        return

    from kew import subprocess_utils

    fake_runner = Path(_SESSION_LOG_DIR) / "kew-test-runner"
    fake_runner.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'codex-cli 999.0.0'\n"
        'elif [ "$1" = "auth" ]; then\n'
        "  printf '%s\\n' '{\"loggedIn\": true}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_runner.chmod(0o755)

    runner_binaries = {
        binary for binary, _hint in subprocess_utils.RUNNER_REQUIREMENTS.values()
    }
    real_which = subprocess_utils.shutil.which

    def _which(command: str, mode: int = os.F_OK | os.X_OK, path: str | None = None):
        if command in runner_binaries:
            return str(fake_runner)
        found = real_which(command, mode=mode, path=path)
        # The stripped-PATH regression run hides Homebrew's gh on macOS.
        # Preserve any test-provided gh, then fill only the absent host tool.
        if command == "gh" and found is None:
            return str(fake_runner)
        return found

    monkeypatch.setattr(subprocess_utils.shutil, "which", _which)


@pytest.fixture(autouse=True)
def _guard_repo_dispatch_db() -> Generator[None]:
    """Fail (and self-heal) if a test opens the repo-local dispatch.db.

    Regression guard for the hermetic-log-dir fix: if a new code path resolves
    the default log dir without honoring KEW_LOG_DIR, this catches it
    deterministically instead of silently poisoning the checkout for the next
    cross-schema run.
    """
    preexisting = _REPO_DISPATCH_DB.exists()
    yield
    if _REPO_DISPATCH_DB.exists() and not preexisting:
        _REPO_DISPATCH_DB.unlink(missing_ok=True)  # remove the poison
        pytest.fail(
            "A test opened the repo-local logs/dispatch/dispatch.db instead of "
            "an isolated tmp dir. Point the CLI at KEW_LOG_DIR / a tmp_path log "
            f"dir (see conftest._hermetic_log_dir). Removed: {_REPO_DISPATCH_DB}"
        )


# -- Project-wide audit context reset (autouse) --


@pytest.fixture(autouse=True)
def _suppress_staleness_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppress the startup staleness nudge during tests."""
    monkeypatch.setenv("KEW_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _reset_github_linkage_probe() -> Generator[None]:
    """Never let one test's linkage probe verdict decide another test's path.

    The GitHub-native capability probe is cached per process (once per driver
    run, by design). In a test session that cache is shared state: a test that
    primes it "available" would send an unrelated test down the native path
    with a fake `gh` that knows nothing about it.
    """
    from kew.graph.adapters.github_native import reset_linkage_probe_cache

    reset_linkage_probe_cache()
    yield
    reset_linkage_probe_cache()


@pytest.fixture(autouse=True)
def _no_selector_requery_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the selector's zero-result re-query sleep in tests.

    ``select_next_issue`` waits ~5s before its single re-query to absorb
    GitHub label-write lag. Tests that don't inject their own ``sleep``
    would otherwise pay that real wall-clock cost on every empty-queue path.
    """
    monkeypatch.setattr("kew.selector.time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _default_no_sandbox_runtime(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default the sandbox runtime probe to 'not found' across the suite.

    sandbox.enabled now defaults to "auto" (#654): a real docker/OrbStack on
    the test host would otherwise flip every execute_dispatch test onto the
    sandboxed path. Force detection to None so the effective default is the
    host path. Tests that exercise the sandbox override
    kew.sandbox.detect_runtime_cached (or resolve_effective_sandbox) inside
    their own patch context. Skipped for test_sandbox, which drives the real
    detection helpers directly.

    The suite ALSO declares the uncontained posture explicitly, because kew no
    longer downgrades to the host on its own (D1): a dispatch test with no
    runtime is now refused, exactly as a user's would be. Nothing here is
    launching a real agent, so running "uncontained" is honest — but it has to
    be *stated*, and stating it is the point. Deliberately not done by making
    `dangerously_skip_containment` default to True in the config: that would
    make the suite green while every real install refused, which is precisely
    the class of divergence this project keeps getting bitten by.
    """
    if request.module.__name__.endswith("test_sandbox"):
        return
    import kew.sandbox as sandbox
    from kew.config import SandboxConfig

    sandbox.reset_runtime_cache()
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: None)

    original = sandbox.resolve_effective_sandbox

    def _uncontained_by_default(
        config: SandboxConfig,
        *,
        runner: str,
        store: object | None = None,
        direct: bool = True,
        **route,
    ):
        # ONLY the environment default is adjusted. A test that says
        # `enabled = False` or `enabled = True` is making a statement about
        # posture, and this fixture must not answer it for them — that would
        # hide the very behaviour such a test exists to check. The narrow case
        # is the implicit one: a config nobody configured, on a host the
        # fixture has already told there is no runtime.
        #
        # ``store`` is forwarded verbatim: it is the ledger the OpenShell
        # egress gate reads its certification from, and swallowing it here
        # would turn every contained OpenShell test into a refusal for a
        # reason the test never set up (#2319). ``direct`` is forwarded the
        # same way: it decides whether a per-run OpenShell provider is
        # attested (#2318).
        #
        # ``**route`` forwards the caller's own route facts (``gateway_launch``)
        # untouched: this fixture adjusts the host default and nothing else, and
        # dropping one of them would answer a question the caller asked — the
        # remediation flows pass `gateway_launch=False` and must keep getting
        # the refusal that fact earns.
        if config.enabled != "auto" or config.dangerously_skip_containment:
            return original(config, runner=runner, store=store, direct=direct, **route)
        return original(
            config.model_copy(update={"dangerously_skip_containment": True}),
            runner=runner,
            store=store,
            direct=direct,
            **route,
        )

    # Patch every binding, not just the definition: importers did
    # `from kew.sandbox import resolve_effective_sandbox`, so their module
    # attribute is already bound and would not see a patch on kew.sandbox.
    monkeypatch.setattr(sandbox, "resolve_effective_sandbox", _uncontained_by_default)
    for module_name in ("kew.dispatch_core", "kew.dispatch_sandbox"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "resolve_effective_sandbox"):
            monkeypatch.setattr(
                module, "resolve_effective_sandbox", _uncontained_by_default
            )
    if request.module.__name__.endswith("test_loop_containment"):
        # That module checks the real loop admission against default configs.
        return
    _declare_uncontained_loops(monkeypatch)


def _declare_uncontained_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """State the uncontained loop posture for configs nobody configured (#1977).

    A loop's agent runs on the host, so an implicit ``auto`` config is refused
    exactly as a user's would be. The same narrow default as dispatch above:
    only the admission sees the flag, never the config the test built, so the
    loop's verification admission still answers for the test's own setting.
    """
    import kew.loops.delegate as delegate
    from kew.config import DispatchConfig

    original = delegate.resolve_loop_agent_posture

    def _loop_uncontained_by_default(config: DispatchConfig, *, runner: str) -> str:
        sandbox_config = config.sandbox
        if (
            sandbox_config.enabled != "auto"
            or sandbox_config.dangerously_skip_containment
        ):
            return original(config, runner=runner)
        return original(
            config.model_copy(
                update={
                    "sandbox": sandbox_config.model_copy(
                        update={"dangerously_skip_containment": True}
                    )
                }
            ),
            runner=runner,
        )

    monkeypatch.setattr(
        delegate, "resolve_loop_agent_posture", _loop_uncontained_by_default
    )


@pytest.fixture(autouse=True)
def _reset_audit_context():
    """Reset module-global audit state before and after every test.

    CLI commands (approve, deny) call bind_policy_context() which sets
    _audit_policy_hash, _audit_policy_snapshot, and _audit_repo as process-wide
    globals.  Without this fixture, state leaks across test ordering and would
    flip test_pending_save_carries_envelope red under -k filters or sharding.
    """
    import kew.audit as audit

    audit.set_audit_context(repo=None)
    yield
    audit.set_audit_context(repo=None)


# -- Event factories for log/TUI tests --


def make_started_event(ts: float = 1711000000.0) -> dict:
    return {
        "ts": ts,
        "type": "started",
        "runner": "claude-code",
        "model": "opus-4",
    }


def make_activity_event(msg: str, ts: float = 1711000010.0) -> dict:
    return {"ts": ts, "type": "activity", "msg": msg}


def make_tool_use_event(
    tool: str = "Read",
    target: str = "src/main.py",
    ts: float = 1711000020.0,
) -> dict:
    return {"ts": ts, "type": "tool_use", "tool": tool, "target": target}


def make_tokens_event(
    inp: int = 1000,
    out: int = 200,
    cost: float = 0.05,
    ts: float = 1711000030.0,
) -> dict:
    return {
        "ts": ts,
        "type": "tokens",
        "input": inp,
        "output": out,
        "cost_usd": cost,
    }


def make_finished_event(
    exit_code: int = 0,
    cost: float = 0.12,
    ts: float = 1711000060.0,
) -> dict:
    return {
        "ts": ts,
        "type": "finished",
        "exit_code": exit_code,
        "cost_usd": cost,
    }


@pytest.fixture
def inbox_client():
    """Factory fixture: inbox_client(app, *, loopback_peer=False, tls=False).

    Sets base_url so the Host header is 127.0.0.1:8765 (accepted by
    HostHeaderMiddleware).  When loopback_peer=True, injects a loopback ASGI
    scope client so _peer_is_loopback() returns True.  The default (False)
    simulates a non-loopback client; pass loopback_peer=True explicitly when
    the test needs the loopback-peer free-read path.

    tls=True makes the client speak https.  Exposed mode sets `Secure` on the
    session cookies (W1.6), and a cookie jar will not send a Secure cookie back
    over http — so a test that logs in under expose and then expects an
    authenticated read must model the browser's side of the TLS proxy, not
    kew's plaintext hop behind it.
    """

    def factory(app, *, loopback_peer: bool = False, tls: bool = False):
        scheme = "https" if tls else "http"
        kwargs: dict = {"base_url": f"{scheme}://127.0.0.1:8765"}
        if loopback_peer:
            kwargs["client"] = ("127.0.0.1", 50000)
        return TestClient(app, **kwargs)

    return factory


class MockSelector:
    """Fake selector that always reports ready so readline() proceeds immediately."""

    def register(self, fileobj, events, data=None):
        pass

    def unregister(self, fileobj):
        pass

    def select(self, timeout=None):
        return [True]  # non-empty → "ready"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _guard_mock_filesystem_paths() -> Generator[None]:
    """Fail (and self-heal) if a mock leaked into a real filesystem path (#1464).

    ``MagicMock`` satisfies ``os.PathLike``, and its ``__fspath__`` returns a
    plausible RELATIVE path (``MagicMock/<mock name>/<id>``) instead of raising.
    So handing a bare mock to path-building code does not fail — it silently
    redirects real writes under the process CWD. Two tests were creating an
    actual ``dispatch.db`` in whatever directory the suite ran from, including
    the operator's checkout.

    Per-test rather than per-session, matching ``_guard_repo_dispatch_db``: the
    useful output is *which* test leaked, not that some test did.

    The previous response to this was a ``MagicMock/`` line in ``.gitignore``,
    which hid the evidence and let it keep happening — so the fix has to be
    louder than the suppression it replaces.
    """
    leaked = Path("MagicMock")
    preexisting = leaked.exists()
    yield
    if not leaked.exists() or preexisting:
        return
    shutil.rmtree(leaked, ignore_errors=True)  # remove the poison
    pytest.fail(
        "A mock reached path-building code and wrote to the working tree: "
        f"{leaked.resolve()}. A MagicMock passed where a real path was expected "
        "does not raise — os.fspath() returns 'MagicMock/<name>/<id>' and the "
        "write lands under the CWD. Give the mock a real tmp_path value."
    )


# --- The serial phase --------------------------------------------------------
# A module that drives a Textual App through its pilot is timing sensitive by
# default: pilot.pause() returns when the message pump is idle, and under CPU
# contention the screen stack can still be empty when the assertion runs
# (#1772: "No screens on stack", seen on the self-hosted runner under load).
# xdist multiplies that contention by the worker count, so ci.yml and
# scripts/verify.sh run the suite in two phases: everything else under
# `-n auto -m "not serial"`, then `-m serial` in one process (the other
# matrix leg and the other jobs still share the host).
#
# A pilot module escapes that default only by waiting through
# tests/support/tui_wait.py (#2342), whose waits all carry a deadline and name
# what they waited for: contention makes such a wait longer, never wrong. So
# the marker is applied here from the module source, not by hand — a new pilot
# test file that pumps the app blindly cannot land in the parallel phase by
# omission, it has to call one of those waits to get there.
#
# The rule itself lives in tests/support/serial_phase.py, which reads it off
# the syntax tree: a docstring line is not an import and an unused import is
# not a wait, so the escape cannot be claimed in passing. Tests with a real
# wall-clock deadline carry @pytest.mark.serial by hand.
# tests/test_serial_phase.py pins all of that, and refuses pilot calls hidden
# in helpers or fixtures, which the probe cannot see, and a deadline-free pump
# left in a module that claims the helper.


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if serial_phase.needs_the_serial_phase(str(item.path)):
            item.add_marker(pytest.mark.serial)
    # The whole collection, markers and all, before anything deselects from
    # it: the repo-wide gates read their inventory off this instead of each
    # driving a nested whole-tree collection of its own (#2340). Recorded
    # after the marker above is applied and before the deselection below, so
    # it is what an opted-in, unfiltered run collects.
    repo_inventory.record_collection(items)
    if _skips_switchyard_image():
        needs_image = [
            item for item in items if item.get_closest_marker(SWITCHYARD_IMAGE_MARKER)
        ]
        if needs_image:
            config.hook.pytest_deselected(items=needs_image)
            items[:] = [item for item in items if item not in needs_image]
        config.stash[_SWITCHYARD_IMAGE_DESELECTED] = len(needs_image)


# --- The Switchyard image is opt-in -------------------------------------------
# The kew-switchyard image builds only from the pinned private source input,
# which exists on the private (self-hosted) runner alone. So the tests marked
# @pytest.mark.requires_switchyard_image are deselected by default — on every
# machine without that input, which is the verify VM, a hosted runner and a
# contributor's laptop — with SWITCHYARD_IMAGE_SKIP_REASON printed in the
# header and the summary. KEW_SWITCHYARD_IMAGE=1 opts in and runs them;
# ci.yml and release.yml set it exactly when the repository is private. Any
# other value is a usage error, so a typo cannot opt in or out silently.
# docs/operations.md ("Public repository runner rule") records the switch.
# tests/test_switchyard_image_skip.py pins the marked set.
SWITCHYARD_IMAGE_MARKER = "requires_switchyard_image"
SWITCHYARD_IMAGE_ENV = "KEW_SWITCHYARD_IMAGE"
SWITCHYARD_IMAGE_SKIP_REASON = (
    "the pinned Switchyard input includes unreleased upstream patches; "
    "it builds on the private runner until they are public"
)
_SWITCHYARD_IMAGE_DESELECTED = pytest.StashKey[int]()


def _skips_switchyard_image() -> bool:
    value = os.environ.get(SWITCHYARD_IMAGE_ENV, "")
    if value not in ("", "1"):
        raise pytest.UsageError(
            f"{SWITCHYARD_IMAGE_ENV} must be unset, empty or 1, got {value!r}"
        )
    return value != "1"


# --- The shard option --------------------------------------------------------
# `--shard=K/N` splits the phase across machines. Only its registration is here;
# the partition, its balance and the deselection are tests/support/shard.py.


def pytest_addoption(parser: pytest.Parser) -> None:
    """``--shard=K/N`` — run the K-th of N balanced groups (#2344).

    Lives here because pytest only honours ``pytest_addoption`` in the conftest
    at the tests root; the option's whole implementation is
    ``tests/support/shard.py``, registered as a plugin below so its deselection
    can run after pytest's ``-m`` deselection rather than before it.
    """
    parser.getgroup("kew").addoption(
        "--shard",
        dest="shard",
        default=None,
        metavar="K/N",
        help=(
            "run only the K-th of N groups, balanced by the durations recorded "
            "in tests/.test_durations.json; the N runs together are exactly the "
            "unsharded selection (#2344)"
        ),
    )


#: The name ``tests/test_shard.py`` looks the shard plugin up under.
SHARD_PLUGIN_NAME = "kew-test-shard"

# --- KEW_REQUIRE_TOOLS=1: no silent skips on a gate ---------------------------
# CI and scripts/verify.sh set it; a skip the environment caused then fails and
# names what was missing. The mode and its one allowlist of intended skips are
# tests/support/required_tools.py, registered below like the shard plugin.
REQUIRED_TOOLS_PLUGIN_NAME = "kew-required-tools"


def pytest_configure(config: pytest.Config) -> None:
    _skips_switchyard_image()
    spec = config.getoption("shard", default=None)
    if spec:
        # Before collection, not during it: a malformed --shard should stop the
        # run rather than surface as a deselection count nobody reads.
        shard.parse_shard(spec)
    if not config.pluginmanager.hasplugin(SHARD_PLUGIN_NAME):
        config.pluginmanager.register(shard, SHARD_PLUGIN_NAME)
    if not config.pluginmanager.hasplugin(REQUIRED_TOOLS_PLUGIN_NAME):
        config.pluginmanager.register(required_tools, REQUIRED_TOOLS_PLUGIN_NAME)


@pytest.fixture(autouse=True)
def _switchyard_image_skip_stays_out_of_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch steers collection and the workflow steps, never a test.

    Deselection reads it in pytest_collection_modifyitems, before any fixture
    runs. Inside a test it would change the behaviour of scan-agent-image.sh or
    a nested pytest that the test drives through a copy of os.environ, so every
    test starts without it; a test of the switch sets it explicitly.
    """
    monkeypatch.delenv(SWITCHYARD_IMAGE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _require_tools_stays_out_of_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode governs this session's skips, never a pytest a test starts.

    It is read once at configure. A nested pytest a test drives through a copy
    of os.environ would otherwise fail on a skip that is that test's subject.
    """
    monkeypatch.delenv(required_tools.ENV, raising=False)


def pytest_report_header(config: pytest.Config) -> list[str] | None:
    lines = []
    # A stray override would point every whole-tree scan at another tree; say
    # which one, so an ordinary run shows it.
    scanned = tree_scan.override_header()
    if scanned:
        lines.append(scanned)
    if _skips_switchyard_image():
        lines.append(
            f"{SWITCHYARD_IMAGE_ENV} is not 1: deselecting "
            f"@pytest.mark.{SWITCHYARD_IMAGE_MARKER} tests: "
            f"{SWITCHYARD_IMAGE_SKIP_REASON}"
        )
    return lines or None


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    # pytest hides the report header under -q, which is how most runs go; the
    # summary is printed at every verbosity.
    scanned = tree_scan.override_header()
    if scanned:
        terminalreporter.write_line(scanned)
    if not _skips_switchyard_image():
        return
    count = terminalreporter.config.stash.get(_SWITCHYARD_IMAGE_DESELECTED, None)
    shown = "" if count is None else f"{count} "
    terminalreporter.write_line(
        f"deselected {shown}@pytest.mark.{SWITCHYARD_IMAGE_MARKER} tests: "
        f"{SWITCHYARD_IMAGE_SKIP_REASON}"
    )
