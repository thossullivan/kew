"""Real gh-CLI seam coverage for ``GitHubFactsReader`` (Task T3, #913).

Every other reader test in ``test_driver_facts.py`` injects a fake ``run``
callable — i.e. it mocks *kew's own* subprocess layer and never exercises the
allowlist resolution (``resolve_cmd``), ``preflight_tools()``, or the actual
``gh --json`` output-parsing path. That is the #435 blind spot and the exact
class of driver-surface regression that crashed the first live drive in #807:
``kew epic status`` shelled out to ``gh`` without ``preflight_tools()`` having
populated the allowlist and died with *"Unknown executable 'gh' — not in
allowlist"*.

This module closes that gap. It drives the REAL ``GitHubFactsReader`` with its
DEFAULT ``run`` (``run_with_backoff`` → ``subprocess_utils.run`` →
``resolve_cmd`` → a real ``subprocess.run``), resolving ``gh`` through a
**hermetic stub binary** placed on ``PATH``. The stub emits JSON recorded from
real ``gh issue view``/``gh pr view --json ...`` output (``tests/fixtures/gh/``),
so the parser is tested against the real response shapes with no network and no
mocks of kew's modules.

Two guarantees, together, guard the #807 status-crash class:
  * ``test_issue_facts_empty_allowlist_reproduces_807_crash`` reproduces the
    crash: the real reader, run before ``preflight_tools()``, raises the
    allowlist ``ValueError``.
  * the parsing tests prove the fixed wiring (preflight + real ``gh`` seam)
    parses real ``gh`` JSON end-to-end without crashing.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kew import subprocess_utils
from kew.graph.driver.facts import GitHubFactsReader

REPO = "o/r"
_FIXTURES = Path(__file__).parent / "fixtures" / "gh"


def _exec_capable_base(preferred: Path) -> Path:
    """Return a directory where a written script is actually executable.

    pytest's ``tmp_path`` lives under the system temp dir, which some
    sandboxes mount ``noexec`` (``os.access(..., X_OK)`` is False there even
    after ``chmod +x``) — real CI mounts it exec, so ``preferred`` is tried
    first. HOME is the documented exec-capable fallback for the sandbox image.
    """
    for base in (preferred, Path.home(), Path(tempfile.gettempdir())):
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / f".kew_exec_probe_{os.getpid()}"
            probe.write_text("#!/bin/sh\n", encoding="utf-8")
            probe.chmod(0o755)
            ok = os.access(probe, os.X_OK)
            probe.unlink()
            if ok:
                return base
        except OSError:
            continue
    return preferred


def _write_gh_stub(bin_dir: Path) -> Path:
    """Write a hermetic ``gh`` executable that replays recorded fixtures.

    Dispatches on argv exactly as the reader shells out:
      * ``gh issue view <N> --repo ... --json ...`` -> ``issue_view_<N>.json``
      * ``gh pr view <N> --repo ... --json ...``    -> ``pr_view_<N>.json``
    Exits non-zero (like real ``gh`` on a missing object) when no fixture
    matches, so an unscripted call fails loudly instead of hanging or hitting
    the network. The fixtures dir is baked into the script so the stub needs no
    environment beyond a ``python3`` on PATH (present in the CI image).
    """
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"FIXTURES = {str(_FIXTURES)!r}\n"
        "argv = sys.argv[1:]\n"
        "sub, verb = (argv[0], argv[1]) if len(argv) >= 2 else ('', '')\n"
        "number = argv[2] if len(argv) >= 3 else ''\n"
        "name = None\n"
        "if (sub, verb) == ('issue', 'view'):\n"
        "    name = f'issue_view_{number}.json'\n"
        "elif (sub, verb) == ('pr', 'view'):\n"
        "    name = f'pr_view_{number}.json'\n"
        "elif (sub, verb) == ('pr', 'list'):\n"
        "    sys.stdout.write('[]')\n"
        "    sys.exit(0)\n"
        "if name is None:\n"
        "    sys.stderr.write(f'stub gh: unscripted call: {argv!r}\\n')\n"
        "    sys.exit(2)\n"
        "path = f'{FIXTURES}/{name}'\n"
        "try:\n"
        "    with open(path) as fh:\n"
        "        sys.stdout.write(fh.read())\n"
        "except FileNotFoundError:\n"
        "    sys.stderr.write(f'stub gh: no such object (HTTP 404): {name}\\n')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.fixture(autouse=True)
def _reset_tool_paths() -> Any:
    """Empty the allowlist before each test, restore it after.

    Mutates ``TOOL_PATHS`` *in place* (not a rebind) so aliases that did
    ``from kew.subprocess_utils import TOOL_PATHS`` keep pointing at the same,
    now-empty dict — the ``test_epic_driver_gh_smoke`` pattern.
    """
    saved = dict(subprocess_utils.TOOL_PATHS)
    subprocess_utils.TOOL_PATHS.clear()
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


@pytest.fixture
def gh_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Install the hermetic ``gh`` stub on PATH and run the REAL preflight.

    After this fixture, ``shutil.which('gh')`` resolves to the stub and
    ``subprocess_utils.TOOL_PATHS['gh']`` is the stub's absolute path — the
    real allowlist wiring the #807 fix depends on, with zero mocks of kew code.
    """
    base = _exec_capable_base(tmp_path)
    bin_dir = Path(tempfile.mkdtemp(prefix="kew-gh-stub-", dir=base)) / "bin"
    bin_dir.mkdir()
    try:
        stub = _write_gh_stub(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        subprocess_utils.preflight_tools()
        assert subprocess_utils.TOOL_PATHS["gh"] == str(stub)
        yield stub
    finally:
        # tmp_path is auto-cleaned by pytest, but the HOME fallback is not.
        shutil.rmtree(bin_dir.parent, ignore_errors=True)


def test_issue_facts_empty_allowlist_reproduces_807_crash() -> None:
    """The real reader, run before preflight, dies on the empty allowlist.

    This is the #807 status-crash reproduced at the seam: a driver code path
    that shells ``gh`` without ``preflight_tools()`` having run raises the exact
    "Unknown executable 'gh' — not in allowlist" ``ValueError``.
    """
    assert subprocess_utils.TOOL_PATHS == {}
    reader = GitHubFactsReader(REPO)  # DEFAULT run == run_with_backoff (real seam)
    with pytest.raises(ValueError, match="gh"):
        reader.issue_facts(7)


def test_issue_facts_parses_real_gh_json_via_hermetic_stub(gh_seam: Path) -> None:
    """The fixed wiring parses real ``gh`` JSON end-to-end (issue -> linked PR)."""
    reader = GitHubFactsReader(REPO)  # DEFAULT run == run_with_backoff (real seam)
    facts = reader.issue_facts(7)

    assert facts.issue_num == 7
    assert facts.state == "OPEN"
    assert facts.state_reason is None
    assert facts.labels == frozenset({"enhancement", "kew:in-progress"})
    # Enriched from the linked PR (real gh nests the ref under
    # closedByPullRequestsReferences), then a second real gh call for the PR.
    assert facts.pr_number == 42
    assert facts.pr_url == "https://github.com/o/r/pull/42"
    assert facts.review_decision == "APPROVED"
    assert facts.merge_state_status == "CLEAN"
    assert facts.head_oid == "deadbeef1234deadbeef1234deadbeef12345678"
    # Last commit's committedDate, from the recorded commits array.
    assert facts.pr_pushed_at == "2026-07-01T00:00:00Z"


def test_issue_facts_no_linked_pr_via_hermetic_stub(gh_seam: Path) -> None:
    """A closed/not-planned issue with no linked PR parses to bare facts."""
    reader = GitHubFactsReader(REPO)
    facts = reader.issue_facts(5)

    assert facts.issue_num == 5
    assert facts.state == "CLOSED"
    assert facts.state_reason == "NOT_PLANNED"
    assert facts.labels == frozenset()
    assert facts.pr_number is None
    assert facts.pr_url is None
    assert facts.review_decision is None


def test_pr_facts_and_merge_gate_via_hermetic_stub(gh_seam: Path) -> None:
    """``pr_facts`` returns the parsed real JSON; the merge gate reads it green."""
    reader = GitHubFactsReader(REPO)
    pr = reader.pr_facts(42)

    assert pr["number"] == 42
    assert pr["mergeStateStatus"] == "CLEAN"
    assert pr["reviewDecision"] == "APPROVED"
    assert reader.required_checks_green("main", pr) is True


def test_gh_failure_via_hermetic_stub_yields_empty_facts(gh_seam: Path) -> None:
    """A missing object (stub exits non-zero) degrades to empty facts, no crash.

    Guards the driver surface: a 404 from ``gh`` must not raise out of the
    reader — the same graceful-degradation contract the mock tests assert, now
    proven through the real subprocess + returncode path.
    """
    reader = GitHubFactsReader(REPO)
    facts = reader.issue_facts(404)  # no issue_view_404.json fixture -> stub exit 1

    assert facts.issue_num == 404
    assert facts.state == ""
    assert facts.pr_number is None
    assert reader.pr_facts(404) == {}


def test_fixtures_are_valid_recorded_gh_json() -> None:
    """The recorded fixtures are well-formed objects with the fields the reader
    reads — a cheap guard against a corrupted/edited fixture masking drift."""
    issue = json.loads((_FIXTURES / "issue_view_7.json").read_text())
    assert {"number", "state", "labels", "closedByPullRequestsReferences"} <= set(issue)
    pr = json.loads((_FIXTURES / "pr_view_42.json").read_text())
    assert {"mergeStateStatus", "headRefOid", "commits", "reviewDecision"} <= set(pr)
