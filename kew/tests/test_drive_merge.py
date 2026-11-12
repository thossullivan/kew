"""`kew drive`'s standing merge rule and merge executor (#2618).

The acceptance, one test each: a READY non-gate PR merges after a passing
verify; a gate-path PR and a UI PR without a browser check are declined (left
for a person); a rule edited by anyone but the signer is refused; two READY PRs
never verify at once; a main that moves mid-verify refuses, and the next round
merges.

Git is real — a bare ``origin`` carrying ``main`` and ``refs/pull/7/head``, and
a clone the drive works in — so the fetch, the fresh worktree, the merge of main
into the head and the net-path read are the code's own. Only the forge (``gh``)
and ``kew verify`` itself are faked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx

from kew import subprocess_utils
from kew.audit import Actor, emit
from kew.config import DispatchConfig, DriveMergeConfig, EpicConfig
from kew.drive_merge import (
    DECLINED_EVENT,
    HOST_UNREACHABLE_EVENT,
    REPORT_DROPPED_EVENT,
    RULE_SIGNED_EVENT,
    DriveMerger,
    RuleRefused,
    SignedRule,
    authorize_rule,
    decline_reason,
    probe_verify_host,
    rule_fingerprint,
)
from kew.graph.driver import merge_queue as _mq
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
    base_integration_exhausted_hold_reason,
    base_integration_hold_reason,
)
from kew.graph.driver.facts import GitHubFactsReader, NodeFacts
from kew.graph.driver.operator_merge import OPERATOR_MERGE_EVENT
from kew.graph.driver.overlay import DriverStatus, overlay_status
from kew.graph.driver.review_stage import drive_review_stage
from kew.hosts import HostError
from kew.hosts.gcp import GcpProvider
from kew.merge_verify import (
    MUTATION_EVENT,
    VERIFY_EVENT,
    MutationReport,
    SurvivingMutant,
    VerifyRefused,
    VerifyResult,
    record_verify_event,
    resolve_candidate,
)
from kew.outcomes import ForgeIdentity

store = _fx.store

EPIC = "epic/1"
ITEM = "42"
PR = 7
REPO = "o/r"
TITLE = "Add the widget"
BODY = "Closes #42\n\nThe widget."


def _git(cwd: Path, *args: str) -> str:
    # A fixed identity: the verify host has no global git config.
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.test"]
    return subprocess.run(
        ["git", "-C", str(cwd), *identity, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", path)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)
    return _git(repo, "rev-parse", "HEAD")


class _Forge:
    """A bare origin with main, a PR head at refs/pull/7/head, and a clone."""

    def __init__(self, root: Path, pr_path: str) -> None:
        self.origin = root / "origin.git"
        self.work = root / "author"
        self.clone = root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)])
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        _commit(self.work, "README.md", "hello\n")
        _git(self.work, "push", "-q", str(self.origin), "main")
        _git(self.work, "checkout", "-qb", "feature")
        self.head = _commit(self.work, pr_path, "change\n")
        _git(self.work, "push", "-q", str(self.origin), f"feature:refs/pull/{PR}/head")
        _git(self.work, "checkout", "-q", "main")
        self.move_main("main moved before the PR merged\n")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)], check=True
        )

    def move_main(self, text: str) -> str:
        sha = _commit(self.work, f"notes/{len(text)}-{text[:4]}.md", text)
        _git(self.work, "push", "-q", str(self.origin), "main")
        return sha


def _facts(head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status="CLEAN",
        head_oid=head,
        pr_pushed_at=None,
        pr_state="OPEN",
    )


class _Reader:
    def __init__(self, head: str) -> None:
        self.head = head

    def issue_facts(self, _key: Any) -> NodeFacts:
        return _facts(self.head)

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        return {
            "number": pr_number,
            "state": "OPEN",
            "headRefOid": self.head,
            "headRefName": "kew/issue-42",
            "mergeStateStatus": "CLEAN",
        }

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return GitHubFactsReader.required_checks_green(self, base, pr)  # type: ignore[arg-type]

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return ("success", "CI green")


class _Run:
    """Real git; a fake ``gh`` that answers PR reads and squash-merges."""

    def __init__(self) -> None:
        self.merges: list[list[str]] = []
        self.comments: list[list[str]] = []
        self.repo_root: Path | None = None
        self.landed: dict[Path, str] = {}

    def _land(self, pr_number: int) -> None:
        """Make origin/main hold the tree a successful squash merge lands."""
        assert self.repo_root is not None
        head_ref = "refs/kew/test-drive-merge/head"
        _git(
            self.repo_root,
            "fetch",
            "-q",
            "origin",
            f"+refs/pull/{pr_number}/head:{head_ref}",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        _git(self.repo_root, "checkout", "-q", "main")
        _git(self.repo_root, "merge", "-q", "--ff-only", "origin/main")
        _git(self.repo_root, "merge", "-q", "--squash", head_ref)
        _git(
            self.repo_root,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            f"squash #{pr_number}",
        )
        self.landed[self.repo_root] = _git(self.repo_root, "rev-parse", "HEAD")

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git":
            if len(cmd) > 2 and cmd[1] == "-C":
                self.repo_root = Path(cmd[2])
            landed = self.landed.get(self.repo_root) if self.repo_root else None
            if landed is not None and "ls-remote" in cmd:
                out = f"{landed}\trefs/heads/main\n"
                return subprocess.CompletedProcess(cmd, 0, out, "")
            if landed is not None and cmd[-1] == landed:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.run(cmd, capture_output=True, text=True, check=False)
        if cmd[:3] == ["gh", "pr", "view"]:
            out = json.dumps({"title": TITLE, "body": BODY})
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:3] == ["gh", "pr", "merge"]:
            self.merges.append(cmd)
            self._land(int(cmd[3]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "comment"]:
            self.comments.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command {cmd}")


class _Verify:
    """``kew verify``: records the ref it verified; ``during`` runs inside."""

    def __init__(self, outcome_code: int | None = 0, during: Any = None) -> None:
        self.refs: list[str] = []
        self.hosts: list[str | None] = []
        self.bodies: list[str | None] = []
        self.bases: list[str | None] = []
        self.prs: list[int | None] = []
        #: Per verify: whether it was told to run the full suite.
        self.full: list[bool] = []
        self.code: int | None = outcome_code
        self.during = during

    def __call__(
        self,
        config: Any,
        *,
        store: Any,
        ref: str,
        repo_root: Path,
        host: str | None = None,
        pr_body: str | None = None,
        base_sha: str | None = None,
        pr: int | None = None,
        force_full: bool = False,
    ) -> Any:
        self.refs.append(ref)
        self.full.append(force_full)
        self.bases.append(base_sha)
        self.prs.append(pr)
        self.hosts.append(host)
        self.bodies.append(pr_body)
        if self.during is not None:
            self.during()
        return None, VerifyResult(
            host=host or "local", exit_code=self.code, seconds=1.0, log=""
        )


class _Report:
    """The post-merge mutation report job: records what it was asked to run.

    *yields* answers None — the host held by another job — that many times
    first; *during* runs inside, as ``_Verify``'s does. A report that ran is
    recorded as :data:`MUTATION_EVENT`, as :func:`run_mutation_report` does.
    """

    def __init__(self, yields: int = 0, during: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.yields = yields
        self.during = during

    def __call__(self, config: Any, **kw: Any) -> MutationReport | None:
        if self.during is not None:
            self.during()
        if self.yields:
            self.yields -= 1
            return None
        self.calls.append(kw)
        emit(
            kw["store"],
            MUTATION_EVENT,
            actor=Actor(type="policy", id="test"),
            payload={"pr": kw["pr_number"], "ref_sha": kw["merged_sha"]},
        )
        return MutationReport(
            outcome="completed",
            survivors=(
                SurvivingMutant(
                    path="src/kew/widget.py",
                    line=3,
                    mutant="kew.widget.x__mutmut_1",
                    mutation="'+' -> '-'",
                ),
            ),
            selected=2,
            killed=1,
        )


def _config(tmp_path: Path, **rule: Any) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.drive.merge = DriveMergeConfig(enabled=True, signer="tom", **rule)
    return config


def _journal(
    store: Any,
    head: str,
    status: str = "in_review",
    tick_ts: str = "2026-10-10T00:00:00+00:00",
) -> None:
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=tick_ts,
        snapshot={
            "epic_ref": EPIC,
            "signal": "progressing",
            "counts": {},
            "nodes": [{"id": ITEM, "status": status}],
        },
        actions={},
        snapshot_hash=f"h-{head}",
    )
    store.record_review_round(
        EPIC, ITEM, head_sha=head, verdict="READY", review_mode="full"
    )


def _tom() -> ForgeIdentity:
    return ForgeIdentity(login="tom")


def _merger(
    store: Any,
    tmp_path: Path,
    forge: _Forge,
    run: _Run,
    verify: _Verify,
    report: _Report | None = None,
    **rule: Any,
) -> DriveMerger:
    return DriveMerger(
        _config(tmp_path, **rule),
        store,
        repo=REPO,
        repo_root=forge.clone,
        reader=lambda: _Reader(forge.head),  # type: ignore[arg-type,return-value]
        provider="github",
        identity=_tom,
        run=run,
        verify=verify,
        report=report or _Report(),
    )


def _events(store: Any, event: str) -> list[Any]:
    return store.list_audit_events(limit=None, event=event)


def test_a_ready_non_gate_pr_merges_after_a_passing_verify(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    # The verified commit is main merged INTO the PR head, in a fresh worktree.
    assert len(verify.refs) == 1
    parents = _git(forge.clone, "rev-list", "--parents", "-n1", verify.refs[0]).split()
    main = _git(forge.origin, "rev-parse", "main")
    assert parents[1:] == [forge.head, main]
    assert run.merges == [
        ["gh", "pr", "merge", str(PR), "--repo", REPO, "--squash"]
        + ["--match-head-commit", forge.head]
        + ["--subject", f"{TITLE} (#{PR})", "--body", BODY]
    ]
    merged = _events(store, OPERATOR_MERGE_EVENT)
    assert [e.payload["trigger"] for e in merged] == ["drive"]
    assert merged[0].actor_id == "drive-merge-rule:tom"
    # The worktree is gone again.
    assert "kew-drive-merge" not in _git(forge.clone, "worktree", "list")


def test_the_drive_names_its_pr_to_the_verify_it_holds_no_claim_for(
    store: Any, tmp_path: Path
) -> None:
    """The drive's verify holds no merge claim, so it is handed the PR as
    data: the verify ledger pairs a drive red with the same PR's next verify
    by it, across heads (#3215)."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.prs == [PR]


@pytest.mark.parametrize(
    "path",
    [
        "scripts/verify.sh",
        ".github/workflows/ci.yml",
        "kew/uv.lock",
        "kew/pyproject.toml",
        "kew/tests/conftest.py",
        "kew.toml",
        "docker/agent.Dockerfile",
        "frontend/scripts/build.mjs",
        "kew/src/kew/hosts/provision/recipe.sh",
        ".pre-commit-config.yaml",
    ],
)
def test_a_gate_or_protected_path_is_declined(path: str) -> None:
    reason = decline_reason([path], ui_paths=["frontend/"], browser_checked=True)
    assert reason is not None, path


@pytest.mark.parametrize(
    "path",
    [
        ".semgrepignore",
        "kew/.semgrep/pinned.yml",
        "kew/.xenon-excludes",
        "kew/pytest.ini",
        "ruff.toml",
        "kew/.coveragerc",
        "kew/.importlinter",
        ".gitleaks.toml",
        "mkdocs.yml",
        "kew/uv.toml",
        "frontend/vite.config.ts",
        "frontend/package.json",
    ],
)
def test_every_default_merge_gate_path_is_declined(path: str) -> None:
    """The drive bypasses the merge queue, so it must decline at least what
    the queue holds as a gate definition — a PR that edits what its own
    ``kew verify`` runs against is never merged without a person."""
    assert _mq.touched_gate_paths([path], EpicConfig().merge_gate_paths)
    reason = decline_reason([path], ui_paths=[], browser_checked=True)
    assert reason is not None, path


def test_configured_gate_paths_only_add_to_what_is_declined() -> None:
    assert decline_reason(["kew/README.md"], ui_paths=[], browser_checked=True) is None
    reason = decline_reason(
        ["kew/README.md"],
        ui_paths=[],
        browser_checked=True,
        gate_paths=["**/README.md"],
    )
    assert reason is not None and "kew/README.md" in reason
    # Emptying the configured list does not lift the built-in classes.
    assert decline_reason([".semgrepignore"], ui_paths=[], browser_checked=True)


def test_a_gate_path_pr_is_declined_and_never_verified(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "scripts/verify.sh")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    merger([EPIC])

    assert verify.refs == [] and run.merges == []
    declined = _events(store, DECLINED_EVENT)
    assert len(declined) == 1, "one decline per head, however many rounds"
    assert "scripts gate class" in declined[0].payload["reason"]
    assert declined[0].payload["sha"] == forge.head


def test_a_ui_pr_merges_only_over_a_recorded_browser_check(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "frontend/src/App.tsx")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    assert run.merges == [] and verify.refs == []
    assert "UI change" in _events(store, DECLINED_EVENT)[0].payload["reason"]

    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "tom"),
        payload={"ref_sha": forge.head, "ui_check": True, "outcome": "passed"},
    )
    merger([EPIC])
    assert len(run.merges) == 1


def test_a_failed_verify_never_merges_nor_reruns_until_main_moves(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify(outcome_code=1)
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    merger([EPIC])
    assert len(verify.refs) == 1 and run.merges == []

    forge.move_main("a fix landed on main\n")
    merger.verify = _Verify()
    merger([EPIC])
    assert len(run.merges) == 1


def test_the_verify_reads_the_pr_body_the_squash_carries(
    store: Any, tmp_path: Path
) -> None:
    """The PR's **Behavior changes** section is what excuses a held-out base
    test failure; a drive verify without the body could never pass a PR that
    changes behavior on purpose."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.bodies == [BODY]
    assert run.merges and run.merges[0][-1] == BODY


class _ContractVerify(_Verify):
    """A verify that also records the acceptance contract it was handed."""

    def __init__(self, during: Any = None) -> None:
        super().__init__(during=during)
        self.contracts: list[Any] = []

    def __call__(self, config: Any, *, contract: Any = None, **kw: Any) -> Any:
        self.contracts.append(contract)
        return super().__call__(config, **kw)


def test_the_verify_runs_the_node_s_recorded_acceptance_contract(
    store: Any, tmp_path: Path
) -> None:
    """The contract the ledger holds for the node is the one the verify runs."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    recorded = store.record_acceptance_contract(
        EPIC,
        ITEM,
        path=f"kew/tests/acceptance/test_issue_{ITEM}.py",
        sha256="a" * 64,
        author_run_id="run-author",
        commit="b" * 40,
    )
    run, verify = _Run(), _ContractVerify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.contracts == [recorded]
    assert run.merges


def test_a_node_without_a_contract_verifies_without_one(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _ContractVerify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.contracts == [None]
    assert run.merges


def test_a_contract_amended_during_verify_stops_the_standing_rule_merge(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    first = store.record_acceptance_contract(
        EPIC,
        ITEM,
        path=f"kew/tests/acceptance/test_issue_{ITEM}.py",
        sha256="a" * 64,
        author_run_id="run-author-1",
        commit="b" * 40,
    )

    def amend() -> None:
        store.record_acceptance_contract(
            EPIC,
            ITEM,
            path=first.path,
            sha256="c" * 64,
            author_run_id="run-author-2",
            commit="d" * 40,
        )

    run = _Run()
    verify = _ContractVerify(during=amend)
    messages: list[str] = []
    merger = _merger(store, tmp_path, forge, run, verify)
    merger.say = messages.append

    merger([EPIC])

    assert verify.contracts == [first]
    assert run.merges == []
    assert _events(store, OPERATOR_MERGE_EVENT) == []
    assert any(
        "acceptance contract changed" in message and "verify again" in message
        for message in messages
    )


def test_an_unusual_path_name_cannot_slip_past_a_gate(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "scripts/\u00e4 gate.sh")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.refs == [] and run.merges == []
    assert "scripts gate class" in _events(store, DECLINED_EVENT)[0].payload["reason"]


def test_a_rule_edited_by_anyone_but_the_signer_is_refused(store: Any) -> None:
    rule = DriveMergeConfig(enabled=True, signer="tom")
    assert isinstance(authorize_rule(store, rule, _tom), SignedRule)
    # Unchanged: no new signature, whoever runs the drive.
    mallory = lambda: ForgeIdentity(login="mallory")  # noqa: E731
    assert isinstance(authorize_rule(store, rule, mallory), SignedRule)

    edited = DriveMergeConfig(enabled=True, signer="tom", ui_paths=["www/"])
    refused = authorize_rule(store, edited, mallory)
    assert isinstance(refused, RuleRefused) and "only tom" in refused.detail
    # Nor can the editor sign by naming themselves.
    takeover = DriveMergeConfig(enabled=True, signer="mallory")
    assert isinstance(authorize_rule(store, takeover, mallory), RuleRefused)
    # An unreadable login signs nothing.
    unread = lambda: ForgeIdentity(login="", available=False, detail="no gh")  # noqa: E731
    assert isinstance(authorize_rule(store, edited, unread), RuleRefused)
    assert len(_events(store, RULE_SIGNED_EVENT)) == 1

    # The signer can edit, and hand over.
    assert isinstance(authorize_rule(store, takeover, _tom), SignedRule)
    assert isinstance(authorize_rule(store, edited, mallory), SignedRule)
    signed = [e.payload["signed_by"] for e in _events(store, RULE_SIGNED_EVENT)]
    assert sorted(signed) == ["mallory", "tom", "tom"]


def test_a_first_rule_must_be_signed_by_its_named_signer(store: Any) -> None:
    rule = DriveMergeConfig(enabled=True, signer="tom")
    assert isinstance(
        authorize_rule(store, rule, lambda: ForgeIdentity(login="mallory")),
        RuleRefused,
    )
    assert _events(store, RULE_SIGNED_EVENT) == []


def test_a_refused_rule_merges_nothing(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)
    merger.identity = lambda: ForgeIdentity(login="mallory")

    merger([EPIC])

    assert verify.refs == [] and run.merges == []


def test_two_ready_prs_never_verify_at_the_same_time(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run = _Run()
    config = _config(tmp_path)
    lock = _mq.merge_queue_lock_path(config)
    held: list[bool] = []

    def another_verification_tries() -> None:
        with _mq.merge_queue_lock(lock) as lease:
            held.append(lease.acquired)

    verify = _Verify(during=another_verification_tries)
    _merger(store, tmp_path, forge, run, verify)([EPIC])
    assert held == [False], "the queue lock is held for the whole verification"
    assert len(run.merges) == 1

    # And while another verification holds the queue, this one never starts.
    forge2 = _Forge(tmp_path / "second", "kew/src/kew/gadget.py")
    store2_verify = _Verify()
    with _mq.merge_queue_lock(lock) as lease:
        assert lease.acquired
        _journal(store, forge2.head)
        _merger(store, tmp_path, forge2, run, store2_verify)([EPIC])
    assert store2_verify.refs == []


def test_a_main_move_mid_verify_refuses_then_retries(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run = _Run()
    moved = _Verify(during=lambda: forge.move_main("a sibling landed\n"))
    merger = _merger(store, tmp_path, forge, run, moved)

    merger([EPIC])
    assert len(moved.refs) == 1 and run.merges == []

    merger.verify = steady = _Verify()
    merger([EPIC])
    assert len(steady.refs) == 1 and len(run.merges) == 1
    parents = _git(forge.clone, "rev-list", "--parents", "-n1", steady.refs[0]).split()
    assert parents[2] == _git(forge.origin, "rev-parse", "main")


def test_a_verify_another_local_verify_holds_off_waits_without_a_hold_or_failure(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3190: the lease is machine-wide, so a hand ``kew verify``, a nightly or
    another project's drive can hold it. The real ``kew verify`` refuses before
    any job; the pass reads that as contention — no hold row, nothing
    remembered as failed — and the same head merges on the round after the
    lease is released, with main unmoved."""
    from support.held_out_log import opened
    from support.verify_lease_holder import lease_held

    from kew.hosts import Job, LocalProvider
    from kew.merge_verify import run_verify

    for tool in ("git", "sh"):  # the bundle is built with git, the host runs sh
        found = shutil.which(tool)
        assert found is not None, f"{tool} required"
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, found)
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    jobs: list[str] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        jobs.append(job.name)
        return 0, "gate passed\n" + opened("kew-held-out: exit 0 seconds 1\n" * 2)

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    run = _Run()
    merger = _merger(store, tmp_path, forge, run, run_verify)  # type: ignore[arg-type]
    said: list[str] = []
    merger.say = said.append

    with lease_held("refs/heads/nightly") as holder:
        for _round in range(2):
            merger([EPIC])
        assert jobs == [] and run.merges == []
        assert store.list_epic_holds(EPIC) == []
        assert _events(store, VERIFY_EVENT) == []
        refusals = [line for line in said if _mq.MERGE_VERIFY_LEASE_BUSY in line]
        assert refusals, said
        reason = refusals[0][refusals[0].index(_mq.MERGE_VERIFY_LEASE_BUSY) :]
        assert _mq.is_queue_contention(reason)
        assert f"pid {holder.process.pid}" in reason
        holder.release()

    merger([EPIC])

    assert jobs, "the verify ran once the lease was free"
    assert len(run.merges) == 1


def test_the_rule_needs_a_signer_to_be_enabled() -> None:
    with pytest.raises(ValueError, match="signer"):
        DriveMergeConfig(enabled=True)


# ── a verify host that cannot be reached (#2619) ───────────────────────────

FIX = "for gcloud: run `gcloud auth login`"


class _Host:
    """A fake verify host: unreachable for the first *down* probes, then up."""

    def __init__(self, down: int) -> None:
        self.down = down
        self.probes = 0

    def __call__(self, _config: Any) -> str | None:
        self.probes += 1
        if self.probes <= self.down:
            return f"cannot read the status of instance v: login expired; {FIX}"
        return None


def test_an_unreachable_verify_host_pauses_merges_and_resumes_on_its_own(
    store: Any, tmp_path: Path
) -> None:
    """Fails, fails, recovers: no verify and no merge while it is down, ONE
    notification naming the fix, and the merge lands the round the probe
    answers — with review still recording rounds throughout."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)
    merger.probe = host = _Host(down=2)
    notices: list[Any] = []
    merger.notify = notices.append

    for _round in range(2):
        merger([EPIC])
        # The review stage keeps running while merges are paused.
        store.record_review_round(
            EPIC, ITEM, head_sha=forge.head, verdict="READY", review_mode="full"
        )
        assert verify.refs == [] and run.merges == []
        assert merger.host_down is not None

    assert len(notices) == 1
    assert "gcloud auth login" in notices[0].message
    assert "paused" in notices[0].message
    assert len(_events(store, HOST_UNREACHABLE_EVENT)) == 1

    merger([EPIC])
    assert host.probes == 3
    assert merger.host_down is None
    assert len(verify.refs) == 1 and len(run.merges) == 1
    assert len(notices) == 1, "a recovery sends nothing further"


def test_a_second_outage_notifies_again(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    merger = _merger(store, tmp_path, forge, _Run(), _Verify(outcome_code=1))
    answers = iter(["down", None, "down again"])
    merger.probe = lambda _config: next(answers)
    notices: list[Any] = []
    merger.notify = notices.append

    for _round in range(3):
        merger([EPIC])

    assert [n.metadata["detail"] for n in notices] == ["down", "down again"]


def test_an_idle_pass_never_probes_the_host(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    merger = _merger(store, tmp_path, forge, _Run(), _Verify())
    merger.probe = host = _Host(down=5)

    merger([EPIC])  # no READY node: nothing to verify, nothing to probe

    assert host.probes == 0


def test_local_fallback_verifies_locally_only_when_configured(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify, local_fallback=True)
    merger.config.verify.host = "gcp"
    merger.probe = _Host(down=1)
    notices: list[Any] = []
    merger.notify = notices.append

    merger([EPIC])

    assert verify.hosts == ["local"] and len(run.merges) == 1
    assert "operator machine" in notices[0].message


def test_an_excluded_epics_green_non_ui_pr_is_declined_naming_the_exclusion(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")  # non-UI, no gate class
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    probed: list[Any] = []
    merger = _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])
    merger.probe = lambda config: probed.append(config)  # type: ignore[method-assign]

    merger([EPIC])
    merger([EPIC])

    assert verify.refs == [] and run.merges == []
    assert probed == [], "an excluded epic alone never probes the verify host"
    declined = _events(store, DECLINED_EVENT)
    assert len(declined) == 1, "one decline per head, however many rounds"
    assert "excluded from the standing merge rule" in declined[0].payload["reason"]
    assert EPIC in declined[0].payload["reason"]
    assert declined[0].payload["pr"] == PR
    assert declined[0].payload["sha"] == forge.head


@pytest.mark.parametrize("name", [" EPIC/1 ", "epic:epic/1", "Epic/1"])
def test_a_near_miss_spelling_of_an_excluded_epic_still_excludes(
    store: Any, tmp_path: Path, name: str
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify, exclude_epics=[name])([EPIC])

    assert verify.refs == [] and run.merges == []
    assert len(_events(store, DECLINED_EVENT)) == 1


def test_an_earlier_ui_decline_of_the_head_does_not_hide_the_exclusion(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "frontend/src/app.ts")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    _merger(store, tmp_path, forge, run, verify)([EPIC])  # a UI decline first

    _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])([EPIC])

    reasons = [e.payload["reason"] for e in _events(store, DECLINED_EVENT)]
    assert any("is a UI change" in r for r in reasons)
    assert any("excluded from the standing merge rule" in r for r in reasons)


class _FlakyReader(_Reader):
    """Fails the first lookup, then answers with no PR, then with the PR."""

    def __init__(self, head: str, answers: list[Any]) -> None:
        super().__init__(head)
        self.answers = answers

    def issue_facts(self, _key: Any) -> NodeFacts:
        answer = self.answers.pop(0) if self.answers else PR
        if isinstance(answer, Exception):
            raise answer
        facts = _facts(self.head)
        return dataclasses.replace(facts, pr_number=answer)


def test_an_exclusion_unrecorded_by_a_failed_or_empty_lookup_is_retried(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    reader = _FlakyReader(forge.head, [OSError("gh down"), None])
    merger = _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])
    merger.reader = lambda: reader  # type: ignore[assignment,return-value]

    merger([EPIC])  # the lookup raises: nothing recorded
    merger([EPIC])  # no PR linked yet: nothing recorded, never pr 0
    assert _events(store, DECLINED_EVENT) == []
    merger([EPIC])  # the PR answers: recorded once, with its number
    merger([EPIC])

    declined = _events(store, DECLINED_EVENT)
    assert [e.payload["pr"] for e in declined] == [PR]
    assert verify.refs == [] and run.merges == []


def test_an_exclusion_name_that_normalizes_to_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="empty epic name"):
        DriveMergeConfig(enabled=True, signer="tom", exclude_epics=["epic:   "])


def test_a_defect_in_the_exclusion_path_surfaces(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    reader = _FlakyReader(forge.head, [AttributeError("bug")])
    merger = _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])
    merger.reader = lambda: reader  # type: ignore[assignment,return-value]

    with pytest.raises(AttributeError):
        merger([EPIC])


class _CountingReader(_Reader):
    def __init__(self, head: str) -> None:
        super().__init__(head)
        self.lookups = 0

    def issue_facts(self, key: Any) -> NodeFacts:
        self.lookups += 1
        return super().issue_facts(key)


@pytest.mark.parametrize("excluded", [False, True])
def test_a_done_node_with_a_ready_round_is_never_read_again(
    store: Any, tmp_path: Path, excluded: bool
) -> None:
    """#2742: a merged node keeps its last READY round; the merge pass must
    not read its PR, verify it or decline it, round after round."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    # The tick that saw it done ran after its last (READY) review round.
    _journal(store, forge.head, status="done", tick_ts="2999-01-01T00:00:00+00:00")
    run, verify = _Run(), _Verify()
    reader = _CountingReader(forge.head)
    rule = {"exclude_epics": [EPIC]} if excluded else {}
    merger = _merger(store, tmp_path, forge, run, verify, **rule)
    merger.reader = lambda: reader  # type: ignore[assignment,return-value]

    merger([EPIC])
    merger([EPIC])

    assert reader.lookups == 0
    assert verify.refs == [] and run.merges == []
    assert _events(store, DECLINED_EVENT) == []


def test_a_ready_round_newer_than_the_done_tick_is_still_a_candidate(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    # The tick (2026-10-10) predates the READY round recorded now.
    _journal(store, forge.head, status="done")
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert len(verify.refs) == 1 and len(run.merges) == 1


@pytest.mark.parametrize("status", ["blocked", "held", "adversarial_review"])
def test_an_unfinished_status_still_reaches_the_live_gates(
    store: Any, tmp_path: Path, status: str
) -> None:
    """A label-derived or transient status is not a finished node: the merge
    pass still offers it to operator_merge, which checks the real gates."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head, status=status, tick_ts="2999-01-01T00:00:00+00:00")
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert len(verify.refs) == 1 and len(run.merges) == 1


def test_an_epic_not_excluded_still_merges(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify, exclude_epics=["console-qol"])([EPIC])

    assert len(verify.refs) == 1 and len(run.merges) == 1


def test_exclude_epics_is_part_of_the_signed_rule() -> None:
    off = DriveMergeConfig(enabled=True, signer="tom")
    on = DriveMergeConfig(enabled=True, signer="tom", exclude_epics=["console-qol"])
    assert rule_fingerprint(off) != rule_fingerprint(on)
    assert rule_fingerprint(off) == rule_fingerprint(
        DriveMergeConfig(enabled=True, signer="tom", exclude_epics=[])
    ), "a rule signed before the option existed keeps its fingerprint"


def test_local_fallback_is_part_of_the_signed_rule() -> None:
    off = DriveMergeConfig(enabled=True, signer="tom")
    on = DriveMergeConfig(enabled=True, signer="tom", local_fallback=True)
    assert rule_fingerprint(off) != rule_fingerprint(on)


class _RedChecksReader(_Reader):
    """GitHub Actions cannot run: required checks and head checks are red.

    The PR reads as ``gh pr view`` prints it, with its url and base branch,
    which ``gate = "verify"`` binds; *targets* is the base it reads, one per
    fresh read, the last repeated.
    """

    def __init__(self, head: str, targets: tuple[str, ...] = ("main",)) -> None:
        super().__init__(head)
        self.targets = list(targets)

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        target = self.targets.pop(0) if len(self.targets) > 1 else self.targets[0]
        return {
            **super().fresh_pr_facts(pr_number),
            "mergeStateStatus": "BLOCKED",
            "url": f"https://github.com/{REPO}/pull/{pr_number}",
            "baseRefName": target,
        }

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return ("failure", "billing: the job was not started")


def _red_merger(
    store: Any, tmp_path: Path, forge: _Forge, run: _Run, verify: _Verify, **rule: Any
) -> DriveMerger:
    merger = _merger(store, tmp_path, forge, run, verify, **rule)
    merger.reader = lambda: _RedChecksReader(forge.head)  # type: ignore[assignment,return-value]
    return merger


def test_gate_verify_merges_a_ready_pr_with_a_passing_verify_and_red_checks(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _red_merger(store, tmp_path, forge, run, verify, gate="verify")([EPIC])

    assert len(verify.refs) == 1
    assert len(run.merges) == 1 and forge.head in run.merges[0]
    assert [e.payload["trigger"] for e in _events(store, OPERATOR_MERGE_EVENT)] == [
        "drive"
    ]


def test_gate_verify_still_needs_the_verify_to_pass(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify(outcome_code=1)

    _red_merger(store, tmp_path, forge, run, verify, gate="verify")([EPIC])

    assert len(verify.refs) == 1 and run.merges == []


def test_gate_github_refuses_red_checks_before_any_verify(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _red_merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.refs == [] and run.merges == []


class _ResolvingVerify(_Verify):
    """``kew verify``'s own candidate resolution and verdict record around a
    passing run: the base it holds out against is the one the drive passed."""

    def __call__(self, config: Any, *, store: Any, **kw: Any) -> Any:
        _none, result = super().__call__(config, store=store, **kw)
        candidate = resolve_candidate(
            kw["repo_root"], ref=kw["ref"], base_sha=kw.get("base_sha")
        )
        record_verify_event(store, candidate, result, config=config)
        return candidate, result


def test_gate_verify_pins_held_out_base_when_origin_main_is_stale(
    store: Any, tmp_path: Path
) -> None:
    """Remote main B adds test T while the checkout's ``origin/main`` is still
    A; a PR on B that deletes T must be held out against B, where T exists."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    stale = _git(forge.clone, "rev-parse", "origin/main")
    test_t = "kew/tests/test_t.py"
    base_b = _commit(forge.work, test_t, "def test_t():\n    pass\n")
    _git(forge.work, "push", "-q", str(forge.origin), "main")
    _git(forge.work, "checkout", "-qb", "drops-t")
    _git(forge.work, "rm", "-q", test_t)
    forge.head = _commit(forge.work, "kew/src/kew/widget.py", "change\n")
    _git(forge.work, "push", "-qf", str(forge.origin), f"drops-t:refs/pull/{PR}/head")
    _git(forge.work, "checkout", "-q", "main")
    assert _git(forge.clone, "rev-parse", "origin/main") == stale != base_b
    _journal(store, forge.head)
    subprocess_utils._register_optional_tool("git")  # the candidate's git reads
    run, verify = _Run(), _ResolvingVerify()

    _red_merger(store, tmp_path, forge, run, verify, gate="verify")([EPIC])

    assert verify.bases == [base_b]
    [verdict] = _events(store, VERIFY_EVENT)
    assert verdict.payload["base_sha"] == base_b
    assert verdict.payload["held_out_selection"] == {"mode": "selected", "files": 1}
    assert len(run.merges) == 1


def test_the_drives_verify_event_names_the_pr_head_as_first_parent(
    store: Any, tmp_path: Path
) -> None:
    """#3140 — the drive verifies main merged ON the PR head, so the event's
    ``ref_sha`` is no head of the PR; its first recorded parent is."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    subprocess_utils._register_optional_tool("git")  # the candidate's git reads
    run, verify = _Run(), _ResolvingVerify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    [verdict] = _events(store, VERIFY_EVENT)
    assert verdict.payload["ref_sha"] == verify.refs[0] != forge.head
    main = _git(forge.origin, "rev-parse", "main")
    assert verdict.payload["ref_parents"] == [forge.head, main]


def test_gate_verify_refuses_pr_targeting_other_base(
    store: Any, tmp_path: Path
) -> None:
    """A READY PR retargeted to ``release`` is never verified as main plus its
    head and then merged into ``release``."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _red_merger(store, tmp_path, forge, run, verify, gate="verify")
    merger.reader = lambda: _RedChecksReader(forge.head, ("release",))  # type: ignore[assignment,return-value]

    merger([EPIC])

    assert verify.refs == [] and run.merges == []


def test_gate_verify_refuses_a_pr_retargeted_while_it_was_verified(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    merger = _red_merger(store, tmp_path, forge, run, verify, gate="verify")
    reader = _RedChecksReader(forge.head, ("main", "release"))
    merger.reader = lambda: reader  # type: ignore[assignment,return-value]

    merger([EPIC])

    assert len(verify.refs) == 1 and run.merges == []


def test_gate_verify_refuses_a_pr_whose_base_cannot_be_read(
    store: Any, tmp_path: Path
) -> None:
    """The plain reader prints no base: under the verify gate that refuses."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify, gate="verify")([EPIC])

    assert verify.refs == [] and run.merges == []


def test_gate_is_part_of_the_signed_rule() -> None:
    github = DriveMergeConfig(enabled=True, signer="tom")
    verify = DriveMergeConfig(enabled=True, signer="tom", gate="verify")
    assert rule_fingerprint(github) != rule_fingerprint(verify)
    assert rule_fingerprint(github) == rule_fingerprint(
        DriveMergeConfig(enabled=True, signer="tom", gate="github")
    ), "a rule signed before the option existed keeps its fingerprint"


def test_a_verify_the_host_dropped_is_retried_once_the_host_answers(
    store: Any, tmp_path: Path
) -> None:
    """A session the host dropped is no failure of the tree: unlike a red
    verify, it is not held until main moves."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, dropped = _Run(), _Verify(outcome_code=None)
    merger = _merger(store, tmp_path, forge, run, dropped)
    # Up for the pass's probe, gone when re-asked after the verify, then up.
    answers = iter([None, "gone", None])
    merger.probe = lambda _config: next(answers)
    merger.notify = lambda _event: None

    merger([EPIC])
    assert len(dropped.refs) == 1 and run.merges == []

    merger.verify = steady = _Verify()
    merger([EPIC])
    assert len(steady.refs) == 1 and len(run.merges) == 1


def test_no_verdict_from_a_host_that_still_answers_holds_until_main_moves(
    store: Any, tmp_path: Path
) -> None:
    """A suite timeout or a kill on a reachable host would repeat on the same
    tree: re-running it every round would stall the drive, so it is held
    like a red verify — once per main."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, timed_out = _Run(), _Verify(outcome_code=None)
    merger = _merger(store, tmp_path, forge, run, timed_out)

    for _round in range(5):
        merger([EPIC])

    assert len(timed_out.refs) == 1 and run.merges == []


def test_the_gcp_probe_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.verify.host = "gcp"
    config.hosts.gcp.project, config.hosts.gcp.zone = "p", "z"
    config.hosts.gcp.instance = "v"

    def expired(_self: Any) -> str:
        raise HostError("cannot read the status of instance v: reauth required")

    monkeypatch.setattr(GcpProvider, "status", expired)
    detail = probe_verify_host(config)
    assert detail is not None
    assert "reauth required" in detail and "gcloud auth login" in detail

    monkeypatch.setattr(GcpProvider, "status", lambda _self: "TERMINATED")
    assert probe_verify_host(config) is None, "a stopped instance is reachable"


def test_the_local_host_is_always_reachable(tmp_path: Path) -> None:
    assert probe_verify_host(_config(tmp_path)) is None


# ── the post-merge mutation report (#2817) ──────────────────────────────────


def test_a_merge_queues_its_mutation_report_and_posts_the_survivors(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    main = _git(forge.origin, "rev-parse", "main")
    run, verify, report = _Run(), _Verify(), _Report()

    merger = _merger(store, tmp_path, forge, run, verify, report)
    merger([EPIC])

    assert len(run.merges) == 1
    # The report ran against the commit the merge was verified as, scoped to
    # the diff from the main it was verified on.
    assert [(c["merged_sha"], c["base_sha"], c["pr_number"]) for c in report.calls] == [
        (verify.refs[0], main, PR)
    ]
    assert merger.reports == []
    assert len(run.comments) == 1
    comment = run.comments[0]
    assert comment[:5] == ["gh", "pr", "comment", str(PR), "--repo"]
    body = comment[comment.index("--body") + 1]
    assert "mutation=completed survived=1/2" in body
    assert "| `src/kew/widget.py` | 3 | `'+' -> '-'` |" in body


def test_a_gating_verify_waiting_for_the_host_runs_before_a_queued_report(
    store: Any, tmp_path: Path
) -> None:
    timeline: list[str] = []
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run = _Run()
    verify = _Verify(during=lambda: timeline.append("verify"))
    # The first ask finds the host held, so the report stays queued.
    report = _Report(yields=1, during=lambda: timeline.append("report"))
    merger = _merger(store, tmp_path, forge, run, verify, report)
    merger([EPIC])
    assert timeline == ["verify", "report"]
    assert report.calls == [] and len(merger.reports) == 1
    first = verify.refs[0]

    # A second READY PR arrives while the first report is still queued.
    forge2 = _Forge(tmp_path / "second", "kew/src/kew/gadget.py")
    _journal(store, forge2.head)
    merger.repo_root = forge2.clone
    merger.reader = lambda: _Reader(forge2.head)  # type: ignore[assignment,return-value]
    merger([EPIC])

    # Its gating verify ran first; both reports ran only after it, in order.
    assert timeline == ["verify", "report", "verify", "report", "report"]
    assert len(run.merges) == 2
    assert [c["merged_sha"] for c in report.calls] == [first, verify.refs[1]]
    assert merger.reports == []


def test_a_report_yields_while_a_gating_verify_holds_the_merge_queue(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    config = _config(tmp_path)
    lock = _mq.merge_queue_lock_path(config)
    report = _Report(yields=1)
    merger = _merger(store, tmp_path, forge, run, verify, report)
    merger([EPIC])
    assert len(merger.reports) == 1

    with _mq.merge_queue_lock(lock) as lease:
        assert lease.acquired
        merger([])
    assert report.calls == [] and len(merger.reports) == 1
    assert run.comments == []

    merger([])
    assert len(report.calls) == 1 and merger.reports == []
    assert len(run.comments) == 1


def test_a_gating_verify_arriving_while_a_report_runs_finds_the_queue_busy(
    store: Any, tmp_path: Path
) -> None:
    """The report holds the merge-queue lock: a gating verify waits its turn.

    Without it, the verify reached a host the report held, was refused as
    ``incomplete`` and its PR held until main moved.
    """
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    lock = _mq.merge_queue_lock_path(_config(tmp_path))
    leases: list[_mq.QueueLease] = []

    def gating_verify_arrives() -> None:
        with _mq.merge_queue_lock(lock) as lease:
            leases.append(lease)

    report = _Report(during=gating_verify_arrives)
    _merger(store, tmp_path, forge, _Run(), _Verify(), report)([EPIC])

    assert len(report.calls) == 1
    assert [lease.acquired for lease in leases] == [False]
    assert _mq.is_queue_contention(leases[0].reason)


def test_a_report_owed_before_a_restart_runs_in_the_next_drive(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()
    _merger(store, tmp_path, forge, run, verify, _Report(yields=1))([EPIC])
    assert len(run.merges) == 1 and run.comments == []

    # A fresh drive — the first one's process is gone — still owes the report.
    report = _Report()
    restarted = _merger(store, tmp_path, forge, run, verify, report)
    restarted([])

    assert [c["merged_sha"] for c in report.calls] == [verify.refs[0]]
    assert len(run.comments) == 1
    _merger(store, tmp_path, forge, run, verify, report)([])
    assert len(report.calls) == 1, "an answered report is not run again"


def test_a_refused_report_is_dropped_durably(store: Any, tmp_path: Path) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _Run(), _Verify()

    def refuse(_config: Any, **_kw: Any) -> None:
        raise VerifyRefused("no such host")

    merger = _merger(store, tmp_path, forge, run, verify)
    merger.report = refuse
    merger([EPIC])

    assert merger.reports == [] and run.comments == []
    (dropped,) = _events(store, REPORT_DROPPED_EVENT)
    assert dropped.payload["ref_sha"] == verify.refs[0]
    assert "no such host" in dropped.payload["reason"]


class _ConflictRun(_Run):
    """``gh pr view`` answers the mergeability read with *mergeable*."""

    def __init__(self, head: str, mergeable: str) -> None:
        super().__init__()
        self.head = head
        self.mergeable = mergeable

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"] and "mergeable" in cmd[-1]:
            state = "DIRTY" if self.mergeable == "CONFLICTING" else "BLOCKED"
            out = json.dumps(
                {
                    "mergeable": self.mergeable,
                    "mergeStateStatus": state,
                    "headRefOid": self.head,
                }
            )
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return super().__call__(cmd, **kw)


def _hold_reasons(store: Any) -> list[str]:
    return [h.reason for h in store.list_epic_holds(EPIC) if h.item_id == ITEM]


def test_an_excluded_epics_conflicting_pr_is_held_conflicting_and_resolved_never_merged(
    store: Any, tmp_path: Path
) -> None:
    """#2862: the merge pass never reaches an excluded epic, so its READY PR
    kept reading merge-ready after main moved under it. The drive now reads
    the PR's mergeability, holds the node conflicting, and the epic's review
    stage runs the resolve on it — and nothing merges it."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _ConflictRun(forge.head, "CONFLICTING"), _Verify()
    said: list[str] = []
    merger = _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])
    merger.say = said.append

    merger([EPIC])
    merger([EPIC])

    assert _hold_reasons(store) == ["ci_pending:dirty"]
    assert run.merges == [] and verify.refs == []
    assert [s for s in said if "conflicts with main" in s] == [
        f"kew drive: {ITEM}'s PR #{PR} conflicts with main — not merge-ready; "
        "the epic's tick resolves it, and a person still merges it."
    ]
    dirty = dataclasses.replace(_facts(forge.head), merge_state_status="DIRTY")
    status = overlay_status(
        dirty, [], reserved=False, held_reason=_hold_reasons(store)[0]
    )
    assert status is DriverStatus.HELD, "a conflicting PR does not read merge-ready"

    resolved: list[BaseIntegrationContext] = []
    config = _config(tmp_path, exclude_epics=[EPIC])
    config.review.adversarial = True
    outcome = drive_review_stage(
        {ITEM: status},
        {ITEM: dirty},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo=REPO,
        repo_root=forge.clone,
        base="main",
        reviewer_launcher=None,
        base_integrator=lambda ctx: (
            resolved.append(ctx) or BaseIntegrationOutcome("integrated")
        ),
    )

    assert [(c.pr_number, c.head_sha) for c in resolved] == [(PR, forge.head)]
    assert list(outcome.integrated) == [ITEM]
    assert run.merges == []


def test_an_excluded_epics_conflict_hold_clears_once_the_pr_merges_cleanly(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _ConflictRun(forge.head, "CONFLICTING"), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])
    merger([EPIC])
    assert _hold_reasons(store) == ["ci_pending:dirty"]

    run.mergeable = "MERGEABLE"
    merger([EPIC])

    assert _hold_reasons(store) == []
    assert run.merges == []


def test_an_excluded_epics_conflict_never_overwrites_another_hold(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    store.hold_epic_node(EPIC, ITEM, "base_integration_conflict: a.py")
    run, verify = _ConflictRun(forge.head, "CONFLICTING"), _Verify()

    _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])([EPIC])
    run.mergeable = "MERGEABLE"
    _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])([EPIC])

    assert _hold_reasons(store) == ["base_integration_conflict: a.py"]


def test_a_conflict_on_a_head_other_than_the_ready_one_is_not_held(
    store: Any, tmp_path: Path
) -> None:
    """A push past the READY head is the review lifecycle's again."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    run, verify = _ConflictRun("f" * 40, "CONFLICTING"), _Verify()

    _merger(store, tmp_path, forge, run, verify, exclude_epics=[EPIC])([EPIC])

    assert _hold_reasons(store) == []


#: The holds a detached resolver writes, which only ``kew epic unhold`` clears.
_RESOLVER_HOLDS = [
    base_integration_hold_reason(
        BaseIntegrationOutcome("conflicted", conflicted_paths=("a.py",)), base="main"
    ),
    base_integration_exhausted_hold_reason(base="main", attempts=3, head_sha="c" * 40),
]

_HOLD_CALLS = frozenset(
    {
        "list_epic_holds",
        "hold_epic_node",
        "hold_epic_node_item",
        "hold_epic_node_unless",
        "clear_epic_hold",
        "clear_epic_hold_item",
        "remove_epic_hold",
    }
)


class _ResolverRace:
    """The ledger, with a detached resolver that writes its permanent hold
    the moment the drive's first hold read or write after the PR's
    mergeability read returns — the window between a lookup and a write."""

    def __init__(self, store: Any, reason: str) -> None:
        self._store = store
        self._reason = reason
        self.armed = False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._store, name)
        if name not in _HOLD_CALLS:
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if self.armed:
                self.armed = False
                self._store.hold_epic_node(EPIC, ITEM, self._reason)
            return result

        return call


class _RacingConflictRun(_ConflictRun):
    """Arms the resolver race on every mergeability read."""

    def __init__(self, head: str, mergeable: str, race: _ResolverRace) -> None:
        super().__init__(head, mergeable)
        self.race = race

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "view"] and "mergeable" in cmd[-1]:
            self.race.armed = True
        return super().__call__(cmd, **kw)


@pytest.mark.parametrize(
    "resolver_hold", _RESOLVER_HOLDS, ids=["conflict", "exhausted"]
)
def test_a_resolver_hold_written_while_the_conflict_is_held_survives(
    store: Any, tmp_path: Path, resolver_hold: str
) -> None:
    """#2869: the hold transition never replaces the resolver's hold, even
    when the resolver writes it between the drive's read and its write."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    race = _ResolverRace(store, resolver_hold)
    run = _RacingConflictRun(forge.head, "CONFLICTING", race)
    said: list[str] = []
    merger = _merger(race, tmp_path, forge, run, _Verify(), exclude_epics=[EPIC])
    merger.say = said.append

    merger([EPIC])

    assert _hold_reasons(store) == [resolver_hold]
    assert run.merges == []


@pytest.mark.parametrize(
    "resolver_hold", _RESOLVER_HOLDS, ids=["conflict", "exhausted"]
)
def test_a_resolver_hold_written_while_the_conflict_is_released_survives(
    store: Any, tmp_path: Path, resolver_hold: str
) -> None:
    """#2869: the release removes only the conflict hold, so a resolver hold
    written between the drive's read and its delete is never erased."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    _merger(
        store,
        tmp_path,
        forge,
        _ConflictRun(forge.head, "CONFLICTING"),
        _Verify(),
        exclude_epics=[EPIC],
    )([EPIC])
    assert _hold_reasons(store) == ["ci_pending:dirty"]
    race = _ResolverRace(store, resolver_hold)
    run = _RacingConflictRun(forge.head, "MERGEABLE", race)

    _merger(race, tmp_path, forge, run, _Verify(), exclude_epics=[EPIC])([EPIC])

    assert _hold_reasons(store) == [resolver_hold]
    assert run.merges == []


def _ready_naming(store: Any, tmp_path: Path, head: str, kinds: list[str]) -> None:
    """A later READY on *head* whose sidecar names *kinds* in ``not_checked``."""
    not_checked = [{"check": f"{k} check", "kind": k, "reason": "no"} for k in kinds]
    sidecar = tmp_path / "review-sidecar.json"
    sidecar.write_text(json.dumps({"verdict": "READY", "not_checked": not_checked}))
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="READY",
        review_mode="full",
        sidecar_path=str(sidecar),
        sidecar_digest=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("kind", ["docker", "network", "host", "other"])
def test_a_check_only_a_person_can_run_is_declined_to_a_person(kind: str) -> None:
    """A console change, so the recorded ui-check answers the browser entry."""
    reason = decline_reason(
        ["frontend/src/lib/chat.ts"],
        ui_paths=[],
        browser_checked=True,
        unchecked=[kind, "browser", "full_suite"],
    )

    assert reason == (
        f"its READY review could not run the {kind} checks; a person merges"
    )


def test_the_drive_s_own_verify_answers_the_full_suite() -> None:
    assert (
        decline_reason(
            ["kew/src/kew/widget.py"],
            ui_paths=[],
            browser_checked=False,
            unchecked=["full_suite"],
        )
        is None
    )


def test_a_browser_check_is_declined_in_the_ui_wording_until_checked() -> None:
    """A console change: a ``kew verify`` of it runs the browser ui-check."""
    paths = ["frontend/src/lib/chat.ts"]
    declined = decline_reason(
        paths, ui_paths=[], browser_checked=False, unchecked=["browser"]
    )

    assert declined is not None and declined.startswith("is a UI change")
    assert "could not run the browser check" in declined
    assert (
        decline_reason(paths, ui_paths=[], browser_checked=True, unchecked=["browser"])
        is None
    )


def test_a_ready_naming_docker_is_declined_and_never_verified(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    _ready_naming(store, tmp_path, forge.head, ["docker"])
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    # A browser check of the head lifts a UI decline, never this one.
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "tom"),
        payload={"ref_sha": forge.head, "ui_check": True, "outcome": "passed"},
    )
    merger([EPIC])

    assert verify.refs == [] and run.merges == []
    [declined] = _events(store, DECLINED_EVENT)
    assert declined.payload["reason"] == (
        "its READY review could not run the docker checks; a person merges"
    )


def test_a_ready_naming_a_browser_check_merges_after_one_is_recorded(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "frontend/src/lib/chat.ts")
    _journal(store, forge.head)
    _ready_naming(store, tmp_path, forge.head, ["browser", "full_suite"])
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    assert run.merges == [] and verify.refs == []
    [declined] = _events(store, DECLINED_EVENT)
    assert declined.payload["reason"].startswith("is a UI change")

    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "tom"),
        payload={"ref_sha": forge.head, "ui_check": True, "outcome": "passed"},
    )
    merger([EPIC])
    assert len(run.merges) == 1


@pytest.mark.parametrize(
    ("unchecked", "named"),
    [(["browser"], "browser"), (["docker", "browser"], "browser, docker")],
)
def test_a_browser_check_no_verify_of_the_diff_runs_is_declined_to_a_person(
    unchecked: list[str], named: str
) -> None:
    """Nothing under ``frontend/`` changed, so no ``kew verify`` of the head
    ever runs ui-check: the browser check is a person's, like Docker."""
    reason = decline_reason(
        ["kew/src/kew/widget.py"],
        ui_paths=[],
        browser_checked=False,
        unchecked=unchecked,
    )

    assert (
        reason == f"its READY review could not run the {named} checks; a person merges"
    )


def test_a_ready_naming_a_browser_check_off_the_console_is_declined_to_a_person(
    store: Any, tmp_path: Path
) -> None:
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    _ready_naming(store, tmp_path, forge.head, ["browser"])
    run, verify = _Run(), _Verify()

    _merger(store, tmp_path, forge, run, verify)([EPIC])

    assert verify.refs == [] and run.merges == []
    [declined] = _events(store, DECLINED_EVENT)
    assert declined.payload["reason"] == (
        "its READY review could not run the browser checks; a person merges"
    )


def test_a_browser_checked_verify_of_a_merge_of_the_head_lifts_the_ui_decline(
    store: Any, tmp_path: Path
) -> None:
    """The drive reads the one predicate the queues read: a passing ui-check of
    main merged into the head (``ref_parents``) checked the head too."""
    forge = _Forge(tmp_path, "frontend/src/lib/chat.ts")
    _journal(store, forge.head)
    _ready_naming(store, tmp_path, forge.head, ["browser"])
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)

    merger([EPIC])
    assert run.merges == []

    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "tom"),
        payload={
            "ref_sha": "d" * 40,
            "ref_parents": ["e" * 40, forge.head],
            "ui_check": True,
            "ui_check_modules": ["chat"],
            "outcome": "passed",
        },
    )
    merger([EPIC])
    assert len(run.merges) == 1


def test_the_console_s_ui_check_does_not_answer_a_www_browser_check() -> None:
    """A browser-checked verify of the head is on record, but it ran the
    console's ui-check: no ``kew verify`` runs www's ``npm run browser-check``,
    so the browser check its reviewer could not run is a person's."""
    reason = decline_reason(
        ["frontend/src/lib/chat.ts", "www/index.html"],
        ui_paths=[],
        browser_checked=True,
        unchecked=["browser"],
    )

    assert reason == (
        "its READY review could not run the browser checks; a person merges"
    )


@pytest.mark.parametrize("suite", ["full", "tiered"])
@pytest.mark.parametrize(("kinds", "full"), [(["full_suite"], True), ([], False)])
def test_a_ready_naming_the_full_suite_is_verified_on_the_full_suite(
    store: Any, tmp_path: Path, suite: str, kinds: list[str], full: bool
) -> None:
    """The rule waives a ``full_suite`` entry only because the drive's verify
    runs the suite, so it tells the verify to, however ``[verify] suite`` is
    set: a tiered run of selected tests answers nothing about the rest."""
    forge = _Forge(tmp_path, "kew/src/kew/widget.py")
    _journal(store, forge.head)
    _ready_naming(store, tmp_path, forge.head, kinds)
    run, verify = _Run(), _Verify()
    merger = _merger(store, tmp_path, forge, run, verify)
    merger.config.verify.suite = suite

    merger([EPIC])

    assert verify.full == [full]
    assert len(run.merges) == 1
