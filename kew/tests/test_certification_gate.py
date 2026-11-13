"""The E15 post-merge certification-ready gate (P1.3, #1617).

P1.2 gave the release an instrument that can run the deterministic pack and,
separately, record an approved candidate. This is the wiring that makes those
two halves one gate — and the properties worth pinning are the ones that keep
the wiring from claiming more than the pack proved:

* **A clean pack records exactly one envelope; a failed pack records none.**
  The gate is fail-closed in the strong sense: the refusals that cost nothing
  (no operator, no reviewed reason, no audit binding) happen *before* the pack
  is spent, and a pack that did not pass green over the WHOLE pack leaves the
  carrier exactly as it found it.
* **A re-run converges.** Idempotency is keyed on the candidate fingerprint —
  what was proven — not on the envelope digest, which covers the instant and
  the operator and would let an honest second run append a duplicate.
* **It stops at certification-ready.** The gate reaches a recorded
  ``certification-ready`` status and never a journey certificate;
  ``production-certified`` stays gated behind Phase 2's promoted broker and
  live suite, which this gate cannot run.

The script is loaded by path for the reason ``test_certification_conformance``
gives: ``scripts/`` is not an importable package, and making it one would put
release tooling on the library's import surface.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kew import subprocess_utils
from kew.certificate_ref import (
    KIND_CERTIFICATE,
    KIND_READY,
    read_manifest,
    ref_head,
    resolve_certificate,
)
from kew.run_store import SqliteRunStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="the carrier is a git ref, as it is in the image",
)

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "certification_conformance.py"
)
SUBJECT = "e" * 40
OTHER_SUBJECT = "f" * 40
NOW = "2026-08-24T12:00:00+00:00"
LATER = "2026-08-25T09:30:00+00:00"
REASON = "reviewed: the whole pack passed on this exact merged SHA"
PARTIAL_SCOPE = "tests/conformance/test_two_node_dependency.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("certification_conformance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cc = _load()


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = shutil.which("git")
    assert resolved is not None  # the module-level skipif already guarantees it
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


@pytest.fixture(autouse=True)
def _no_ambient_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEW_CERTIFICATION_ACTOR", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "kew@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "kew"], check=True)
    return path


@pytest.fixture
def store_path(tmp_path: Path) -> str:
    path = str(tmp_path / "dispatch.db")
    SqliteRunStore(path).close()
    return path


class _Pack:
    """A stand-in for the real pack run: records every time it was spent."""

    def __init__(self, result: str = "passed") -> None:
        self.result = result
        self.runs: list[str] = []

    def __call__(self, scope: str) -> dict[str, Any]:
        self.runs.append(scope)
        return {
            "scope": scope,
            "result": self.result,
            "exit_code": 0 if self.result == "passed" else 1,
            "counts": {"passed": 212} if self.result == "passed" else {"failed": 3},
            "command": "python -m pytest -q --no-header " + scope,
        }


@pytest.fixture
def pack(monkeypatch: pytest.MonkeyPatch) -> _Pack:
    """The pack is stubbed: this file tests the WIRING, not the pack."""
    runner = _Pack()
    monkeypatch.setattr(cc, "_run_pack", runner)
    return runner


def test_the_cli_seeds_its_own_git_allowlist_in_a_clean_process(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The program doc runs the gate as a standalone ``python scripts/...``
    invocation -- a fresh process with an empty subprocess allowlist, not the
    seeded one every other test rides on. ``main()`` must preflight the git
    allowlist itself, or the carrier's first git read raises an uncaught
    "not in allowlist" before any work is done (#1617)."""
    monkeypatch.setattr(subprocess_utils, "TOOL_PATHS", {})
    assert "git" not in subprocess_utils.TOOL_PATHS

    rc = _status(repo)  # `status` reaches the carrier's git read

    assert "git" in subprocess_utils.TOOL_PATHS  # main() self-seeded the allowlist
    assert rc in (0, 1)  # a real verdict, not an allowlist crash


def _gate(
    repo: Path,
    store_path: str | None,
    *,
    subject: str = SUBJECT,
    reason: str = REASON,
    actor: str | None = "release-operator",
    now: str = NOW,
    extra: tuple[str, ...] = (),
) -> int:
    argv = [
        "gate",
        "--subject",
        subject,
        "--reason",
        reason,
        "--repo",
        str(repo),
        "--now",
        now,
        *extra,
    ]
    if store_path is not None:
        argv += ["--store", store_path]
    if actor is not None:
        argv += ["--actor", actor]
    return int(cc.main(argv))


def _status(repo: Path, *, subject: str = SUBJECT, extra: tuple[str, ...] = ()) -> int:
    return int(cc.main(["status", "--subject", subject, "--repo", str(repo), *extra]))


def _report(capsys: Any) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def _entries(repo: Path, subject: str = SUBJECT) -> tuple[Any, ...]:
    manifest = read_manifest(repo, subject)
    return manifest.entries if manifest is not None else ()


class TestTheGateRecordsOnlyWhatThePackProved:
    """One clean pack, one envelope. A failure writes nothing at all."""

    def test_a_clean_pack_records_exactly_one_ready_envelope(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        report = _report(capsys)
        assert report["recorded"] is True
        assert report["converged"] is False
        assert report["status"] == "certification-ready"
        entries = _entries(repo)
        assert len(entries) == 1
        assert entries[0].kind == KIND_READY
        assert entries[0].envelope.actor == "release-operator"

    def test_the_gate_runs_the_whole_pack_not_a_slice(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        assert pack.runs == [cc.PACK_SCOPE]

    def test_the_recorded_envelope_is_bound_into_the_audit_chain(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        store = SqliteRunStore(store_path, read_only=True)
        try:
            events = store.list_audit_events(limit=None)
        finally:
            store.close()
        assert len([e for e in events if e.event == "certificate_appended"]) == 1

    def test_a_failing_pack_records_nothing(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        pack.result = "failed"
        assert _gate(repo, store_path) == 1
        report = _report(capsys)
        assert report["recorded"] is False
        assert report["status"] is None
        assert report["envelope"] is None
        assert read_manifest(repo, SUBJECT) is None
        assert ref_head(repo) is None

    def test_a_failing_pack_still_reports_what_it_ran(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        pack.result = "failed"
        assert _gate(repo, store_path) == 1
        report = _report(capsys)
        assert report["pack"]["result"] == "failed"
        assert report["pack"]["scope"] == cc.PACK_SCOPE
        assert "did not pass" in report["detail"]

    def test_a_pack_slice_that_passed_is_not_the_pack_and_records_nothing(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        """A green subset proves a subset. The gate refuses to record it."""
        assert _gate(repo, store_path, extra=("--scope", PARTIAL_SCOPE)) == 1
        assert read_manifest(repo, SUBJECT) is None


class TestTheGateConverges:
    """Re-running the gate is safe: it records once, then agrees with itself."""

    def test_rerunning_records_no_second_envelope(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        first = _report(capsys)["envelope"]["digest"]
        assert _gate(repo, store_path) == 0
        second = _report(capsys)
        assert second["recorded"] is False
        assert second["converged"] is True
        assert second["status"] == "certification-ready"
        assert second["envelope"]["digest"] == first
        assert len(_entries(repo)) == 1

    def test_convergence_survives_a_new_instant_and_a_new_operator(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        """The same evidence is the same evidence, recorded once.

        The envelope digest covers the instant and the operator, so keying
        convergence on it would let an honest second run append a duplicate.
        """
        assert _gate(repo, store_path) == 0
        capsys.readouterr()
        assert _gate(repo, store_path, now=LATER, actor="second-operator") == 0
        report = _report(capsys)
        assert report["converged"] is True
        entries = _entries(repo)
        assert len(entries) == 1
        assert entries[0].envelope.actor == "release-operator"

    def test_the_audit_chain_records_one_append_across_re_runs(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        assert _gate(repo, store_path, now=LATER) == 0
        store = SqliteRunStore(store_path, read_only=True)
        try:
            events = store.list_audit_events(limit=None)
        finally:
            store.close()
        assert len([e for e in events if e.event == "certificate_appended"]) == 1

    def test_a_different_subject_is_separate_evidence(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        assert _gate(repo, store_path, subject=OTHER_SUBJECT) == 0
        assert len(_entries(repo)) == 1
        assert len(_entries(repo, OTHER_SUBJECT)) == 1


class TestTheGateFailsClosedBeforeItSpendsAnything:
    """What cannot be recorded is refused before the pack is even run."""

    def test_an_unresolved_operator_records_nothing_and_runs_no_pack(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path, actor=None) == 1
        assert pack.runs == []
        assert read_manifest(repo, SUBJECT) is None

    def test_an_unreviewed_reason_records_nothing_and_runs_no_pack(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path, reason="ok") == 1
        assert pack.runs == []
        assert read_manifest(repo, SUBJECT) is None

    def test_no_audit_binding_records_nothing_and_runs_no_pack(
        self, repo: Path, pack: _Pack
    ) -> None:
        assert _gate(repo, None) == 1
        assert pack.runs == []
        assert read_manifest(repo, SUBJECT) is None

    def test_the_operator_may_come_from_the_environment(
        self,
        repo: Path,
        store_path: str,
        pack: _Pack,
        monkeypatch: pytest.MonkeyPatch,
        capsys: Any,
    ) -> None:
        monkeypatch.setenv("KEW_CERTIFICATION_ACTOR", "release-operator")
        assert _gate(repo, store_path, actor=None) == 0
        assert _report(capsys)["envelope"]["actor"] == "release-operator"


class TestTheGateStopsAtCertificationReady:
    """Recording build acceptance is the whole reach of this gate."""

    def test_the_gate_never_writes_a_journey_certificate(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        kinds = {entry.kind for entry in _entries(repo)}
        assert kinds == {KIND_READY}
        assert KIND_CERTIFICATE not in kinds

    def test_the_recorded_envelope_never_resolves_as_certified(
        self, repo: Path, store_path: str, pack: _Pack
    ) -> None:
        assert _gate(repo, store_path) == 0
        store = SqliteRunStore(store_path, read_only=True)
        try:
            rows = store.list_audit_events(limit=None)
        finally:
            store.close()
        reading = resolve_certificate(repo, SUBJECT, audit_rows=rows)
        assert reading.state != "certified"

    def test_the_report_keeps_production_certified_gated_behind_phase_2(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        report = _report(capsys)
        assert report["production_certified"] is False
        assert "broker" in report["gated_on"]
        assert "live" in report["gated_on"]

    def test_the_gate_verifies_the_exact_envelope_it_recorded(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        report = _report(capsys)
        verification = report["verification"]
        assert verification["tail"]["digest"] == report["envelope"]["digest"]
        assert verification["tail"]["kind"] == KIND_READY
        assert verification["reading"]["state"] != "certified"

    def test_the_candidate_can_be_preserved_for_the_review_trail(
        self, repo: Path, store_path: str, pack: _Pack, tmp_path: Path
    ) -> None:
        out = tmp_path / "candidate.json"
        assert _gate(repo, store_path, extra=("--candidate-out", str(out))) == 0
        document = json.loads(out.read_text(encoding="utf-8"))
        assert document["claim"] == KIND_READY
        assert document["evidence_label"] == "contract"


class TestTheReleaseGateItemIsSatisfiable:
    """`certification-ready` is reached only when the envelope is attached."""

    def test_the_status_is_not_reached_before_the_gate_runs(
        self, repo: Path, capsys: Any
    ) -> None:
        assert _status(repo) == 1
        report = _report(capsys)
        assert report["status"] is None
        assert "no approved" in report["detail"]

    def test_the_status_is_reached_once_the_envelope_is_attached(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        recorded = _report(capsys)["envelope"]["digest"]
        assert _status(repo) == 0
        report = _report(capsys)
        assert report["status"] == "certification-ready"
        assert report["envelope"]["digest"] == recorded

    def test_the_status_can_be_pinned_to_the_exact_evidence(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        fingerprint = _report(capsys)["fingerprint"]
        assert _status(repo, extra=("--fingerprint", fingerprint)) == 0
        capsys.readouterr()
        assert _status(repo, extra=("--fingerprint", "cc1:nothing-proved-this")) == 1

    def test_another_subject_does_not_inherit_the_status(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        capsys.readouterr()
        assert _status(repo, subject=OTHER_SUBJECT) == 1
        assert _report(capsys)["status"] is None

    def test_the_status_never_claims_production_certified(
        self, repo: Path, store_path: str, pack: _Pack, capsys: Any
    ) -> None:
        assert _gate(repo, store_path) == 0
        capsys.readouterr()
        assert _status(repo) == 0
        report = _report(capsys)
        assert report["production_certified"] is False
        assert report["status"] == "certification-ready"
