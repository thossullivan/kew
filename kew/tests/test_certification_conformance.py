"""The two-phase candidate / approved-append boundary (P1.2, #1616).

``scripts/certification_conformance.py`` is the only thing in the tree that
writes to the certificate carrier from a deterministic run, so the properties
worth pinning here are the ones that keep it from writing more than it proved:

* **It records build acceptance, never a certificate.** Every append is a
  ``certification_ready`` envelope, and reading one back must resolve as
  ``uncertified`` — a successful round-trip through this tool can never turn
  into a journey-certificate claim.
* **It refuses on the way in.** An unresolved operator, an unreviewed reason,
  a pack that did not pass, a pack that did not run the WHOLE pack, a foreign
  document kind, and a replay are each refused with nothing written.
* **It sanitizes what it emits.** Absolute paths, credential shapes and raw
  prompt/provider content are refused rather than published, and the pack's
  own transcript never enters the document.

The script is loaded by path: ``scripts/`` is not an importable package, and
making it one would put release tooling on the library's import surface.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kew import subprocess_utils
from kew.certificate_ref import (
    CERTIFICATE_REF,
    KIND_CERTIFICATE,
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
SUBJECT = "c" * 40
OTHER_SUBJECT = "d" * 40
NOW = "2026-08-24T12:00:00+00:00"

#: A real but deliberately small slice of the pack, so the nested pytest run
#: these tests make stays fast. It is NOT the whole pack, which is exactly what
#: ``append-approved`` must refuse.
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


def _passed_pack(scope: str | None = None) -> dict[str, Any]:
    return {
        "scope": scope or cc.PACK_SCOPE,
        "result": "passed",
        "exit_code": 0,
        "counts": {"passed": 212},
        "command": "python -m pytest -q --no-header " + (scope or cc.PACK_SCOPE),
    }


def _candidate_document(**over: Any) -> dict[str, Any]:
    moment = datetime.fromisoformat(NOW)
    document = cc.build_candidate(
        subject=SUBJECT,
        lane="claude-code/github",
        started_at=moment,
        finished_at=moment,
        pack=over.pop("pack", _passed_pack()),
        audit={"head": None, "verified": False, "detail": "no ledger was read"},
    )
    document.update(over)
    return document


def _write(tmp_path: Path, document: dict[str, Any], name: str = "cand.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _append(
    tmp_path: Path,
    repo: Path,
    store_path: str,
    *,
    document: dict[str, Any] | None = None,
    reason: str = "reviewed: the whole pack passed on this exact tree",
    actor: str | None = "release-operator",
    name: str = "cand.json",
) -> int:
    argv = [
        "append-approved",
        "--candidate",
        _write(tmp_path, document or _candidate_document(), name),
        "--reason",
        reason,
        "--repo",
        str(repo),
        "--store",
        store_path,
        "--now",
        NOW,
    ]
    if actor is not None:
        argv += ["--actor", actor]
    return int(cc.main(argv))


class TestTheCandidateIsSanitized:
    """Nothing leaves this tool that a transcript could leak through."""

    @pytest.mark.parametrize(
        "leak",
        [
            {"where": "/home/someone/kew/tests"},
            {"where": "api_key=abcdef0123456789"},
            {"where": "Bearer aaaaaaaaaaaa"},
            {"where": "system_prompt"},
            {"nested": [{"deep": "/etc/passwd"}]},
            {"where": "ran at C:\\Users\\ci\\kew"},
            {"/home/someone/kew": "a leaking KEY is refused too"},
        ],
    )
    def test_a_leaking_document_is_refused_not_published(
        self, leak: dict[str, Any]
    ) -> None:
        with pytest.raises(cc.Refused, match="not sanitized"):
            cc.sanitized({"subject": SUBJECT, **leak})

    def test_a_clean_document_passes_through_unchanged(self) -> None:
        document = _candidate_document()
        assert cc.sanitized(document) == document

    def test_the_candidate_labels_itself_as_contract_evidence(self) -> None:
        document = _candidate_document()
        assert document["evidence_label"] == "contract"
        assert document["claim"] == "certification_ready"
        assert document["claim"] != KIND_CERTIFICATE

    def test_the_candidate_names_what_it_does_not_prove(self) -> None:
        exclusions = " ".join(_candidate_document()["exclusions"])
        assert "PCJ-2" in exclusions
        assert "certified" in exclusions

    def test_unpriced_is_stated_as_a_source_not_as_zero_dollars(self) -> None:
        cost = _candidate_document()["cost"]
        assert cost["source"] == "not_applicable"
        assert cost["certainty"] == "exact"

    def test_the_fingerprint_covers_what_was_proven_not_when(self) -> None:
        later = datetime(2026, 9, 1, tzinfo=UTC)
        moved = cc.build_candidate(
            subject=SUBJECT,
            lane="claude-code/github",
            started_at=later,
            finished_at=later,
            pack=_passed_pack(),
            audit={"head": None, "verified": False, "detail": "no ledger was read"},
        )
        assert moved["fingerprint"] == _candidate_document()["fingerprint"]

    def test_a_different_subject_is_different_evidence(self) -> None:
        other = _candidate_document()
        other["subject"] = OTHER_SUBJECT
        assert (
            cc.build_candidate(
                subject=OTHER_SUBJECT,
                lane="claude-code/github",
                started_at=datetime.fromisoformat(NOW),
                finished_at=datetime.fromisoformat(NOW),
                pack=_passed_pack(),
                audit={"head": None, "verified": False, "detail": "no ledger was read"},
            )["fingerprint"]
            != _candidate_document()["fingerprint"]
        )


class TestCandidateMode:
    """The mode actually runs the pack and reports its verdict honestly."""

    def test_it_runs_the_pack_and_records_the_scope_it_ran(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = str(tmp_path / "candidate.json")
        code = cc.main(
            [
                "candidate",
                "--subject",
                SUBJECT,
                "--scope",
                PARTIAL_SCOPE,
                "--now",
                NOW,
                "--out",
                out,
            ]
        )
        capsys.readouterr()
        document = json.loads(Path(out).read_text(encoding="utf-8"))
        assert code == 0
        assert document["pack"]["result"] == "passed"
        assert (
            document["pack"]["scope"] == "tests/conformance/test_two_node_dependency.py"
        )
        assert document["pack"]["counts"]["passed"] > 0
        assert document["subject"] == SUBJECT

    def test_the_pack_transcript_never_enters_the_document(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = str(tmp_path / "candidate.json")
        cc.main(
            [
                "candidate",
                "--subject",
                SUBJECT,
                "--scope",
                PARTIAL_SCOPE,
                "--now",
                NOW,
                "--out",
                out,
            ]
        )
        capsys.readouterr()
        text = Path(out).read_text(encoding="utf-8")
        assert "conftest" not in text
        assert str(tmp_path) not in text

    def test_a_pack_that_did_not_run_is_not_a_pack_that_passed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = str(tmp_path / "candidate.json")
        code = cc.main(
            [
                "candidate",
                "--subject",
                SUBJECT,
                "--skip-pack",
                "--now",
                NOW,
                "--out",
                out,
            ]
        )
        capsys.readouterr()
        document = json.loads(Path(out).read_text(encoding="utf-8"))
        assert code == 1
        assert document["pack"]["result"] == "not_run"
        assert document["findings"] == ["the pack did not pass"]

    def test_the_default_scope_is_the_whole_pack(self) -> None:
        parsed = cc.build_parser().parse_args(["candidate", "--subject", SUBJECT])
        assert parsed.scope == cc.PACK_SCOPE

    def test_the_gate_catalogue_matches_the_pack_it_claims_to_run(self) -> None:
        """The script's gate list is not an independent claim: it must be the
        pack's own catalogue, or the candidate advertises gates nothing runs."""
        sys.path.insert(0, str(SCRIPT.parent.parent / "tests"))
        try:
            from conformance.test_product_coherence import GATES as PACK_GATES
        finally:
            sys.path.pop(0)
        assert set(cc.GATES) == set(PACK_GATES)


class TestAppendApproved:
    """The write boundary. Every refusal below leaves the carrier untouched."""

    def test_it_records_a_ready_envelope_bound_to_the_audit_chain(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        recorded = json.loads(capsys.readouterr().out)
        assert recorded["recorded"] == "certification_ready"
        assert recorded["subject"] == SUBJECT
        assert recorded["ref"] == CERTIFICATE_REF
        assert recorded["predecessor"] is None
        store = SqliteRunStore(store_path, read_only=True)
        try:
            events = store.list_audit_events(limit=None)
        finally:
            store.close()
        appended = [e for e in events if e.event == "certificate_appended"]
        assert len(appended) == 1

    def test_a_recorded_ready_envelope_never_resolves_as_certified(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        """The property the whole two-phase split exists for."""
        assert _append(tmp_path, repo, store_path) == 0
        capsys.readouterr()
        moment = datetime.fromisoformat(NOW)
        reading = resolve_certificate(repo, SUBJECT, fetched_at=moment, now=moment)
        assert reading.state == "uncertified"
        assert reading.availability == "available"

    def test_an_unresolved_operator_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("KEW_CERTIFICATION_ACTOR", raising=False)
        assert _append(tmp_path, repo, store_path, actor=None) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_the_operator_may_come_from_the_environment(
        self, tmp_path: Path, repo: Path, store_path: str, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setenv("KEW_CERTIFICATION_ACTOR", "release-operator")
        assert _append(tmp_path, repo, store_path, actor=None) == 0
        assert json.loads(capsys.readouterr().out)["actor"] == "release-operator"

    def test_an_unreviewed_reason_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        assert _append(tmp_path, repo, store_path, reason="  ok  ") == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_a_pack_that_did_not_pass_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        failed = _candidate_document(pack={**_passed_pack(), "result": "failed"})
        assert _append(tmp_path, repo, store_path, document=failed) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_a_partial_pack_run_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        """A subset that passed is not the pack that the gates claim."""
        partial = _candidate_document(pack=_passed_pack(PARTIAL_SCOPE))
        assert _append(tmp_path, repo, store_path, document=partial) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_a_document_claiming_a_journey_certificate_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        forged = _candidate_document(claim=KIND_CERTIFICATE)
        assert _append(tmp_path, repo, store_path, document=forged) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_a_foreign_document_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        assert _append(tmp_path, repo, store_path, document={"version": 1}) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_an_unreadable_version_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str
    ) -> None:
        future = _candidate_document(version=99)
        assert _append(tmp_path, repo, store_path, document=future) == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_an_append_with_no_audit_binding_is_refused(
        self, tmp_path: Path, repo: Path
    ) -> None:
        code = cc.main(
            [
                "append-approved",
                "--candidate",
                _write(tmp_path, _candidate_document()),
                "--reason",
                "reviewed: the whole pack passed on this exact tree",
                "--actor",
                "release-operator",
                "--repo",
                str(repo),
                "--now",
                NOW,
            ]
        )
        assert code == 1
        assert cc.read_manifest(repo, SUBJECT) is None

    def test_replaying_the_same_candidate_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        head = capsys.readouterr()
        assert _append(tmp_path, repo, store_path, name="again.json") == 1
        capsys.readouterr()
        manifest = cc.read_manifest(repo, SUBJECT)
        assert manifest is not None
        assert len(manifest.entries) == 1
        assert json.loads(head.out)["digest"] == manifest.entries[0].digest

    def test_another_subject_never_touches_this_ones_evidence(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        other = _candidate_document()
        other["subject"] = OTHER_SUBJECT
        assert (
            _append(tmp_path, repo, store_path, document=other, name="other.json") == 0
        )
        capsys.readouterr()
        mine = cc.read_manifest(repo, SUBJECT)
        theirs = cc.read_manifest(repo, OTHER_SUBJECT)
        assert mine is not None and theirs is not None
        assert len(mine.entries) == 1
        assert mine.entries[0].digest != theirs.entries[0].digest


class TestVerify:
    """Read-only, and it fails closed on everything it cannot confirm."""

    def _verify(self, repo: Path, store_path: str, **over: str) -> int:
        argv = ["verify", "--subject", SUBJECT, "--repo", str(repo), "--now", NOW]
        for flag, value in over.items():
            argv += [f"--{flag.replace('_', '-')}", value]
        return int(cc.main(argv))

    def test_it_observes_the_exact_envelope_that_was_appended(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        digest = json.loads(capsys.readouterr().out)["digest"]
        assert self._verify(repo, store_path, digest=digest, store=store_path) == 0
        observed = json.loads(capsys.readouterr().out)
        assert observed["tail"]["digest"] == digest
        assert observed["tail"]["kind"] == "certification_ready"
        assert observed["tail"]["status"] == "build_accepted"
        assert observed["reading"]["state"] == "uncertified"

    def test_a_subject_with_no_evidence_is_refused_not_reported_empty(
        self, repo: Path, store_path: str
    ) -> None:
        assert self._verify(repo, store_path) == 1

    def test_a_digest_that_was_never_recorded_is_refused(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        capsys.readouterr()
        assert self._verify(repo, store_path, digest="0" * 64) == 1

    def test_verification_writes_nothing(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        assert _append(tmp_path, repo, store_path) == 0
        capsys.readouterr()
        before = ref_head(repo, CERTIFICATE_REF)
        assert self._verify(repo, store_path, store=store_path) == 0
        capsys.readouterr()
        assert ref_head(repo, CERTIFICATE_REF) == before


class TestTheRoundTrip:
    """candidate -> append-approved -> verify, on one subject, end to end."""

    def test_all_three_modes_agree_on_one_subject(
        self, tmp_path: Path, repo: Path, store_path: str, capsys: Any
    ) -> None:
        document = _candidate_document()
        path = _write(tmp_path, document)
        assert _append(tmp_path, repo, store_path) == 0
        appended = json.loads(capsys.readouterr().out)
        assert (
            cc.main(
                [
                    "verify",
                    "--subject",
                    SUBJECT,
                    "--digest",
                    appended["digest"],
                    "--repo",
                    str(repo),
                    "--store",
                    store_path,
                    "--now",
                    NOW,
                ]
            )
            == 0
        )
        verified = json.loads(capsys.readouterr().out)
        assert verified["tail"]["fingerprint"] == document["fingerprint"]
        assert verified["tail"]["lane"] == document["lane"]
        assert verified["reading"]["state"] == "uncertified"
        assert Path(path).exists()
