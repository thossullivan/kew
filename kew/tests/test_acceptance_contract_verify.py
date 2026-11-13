"""Merge verify runs the node's acceptance contract against the head's src (#2730).

The contract is the read-only acceptance test file an ``acceptance_author``
run wrote before the implementer started, recorded in the ledger by path,
sha256 and commit. The held-out phase (#2485) already runs base's tests against
the head's ``src`` in a tree of its own; a node with a contract adds the
contract file to that tree as the *contract commit* carries it, so a head that
weakened its own copy still answers to the version it was handed.

Each case is a tiny git repository verified end to end on the local host, as in
``test_held_out_base_tests.py``: a base on ``main``, a contract commit that
adds the acceptance file, and a head on top of it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.held_out_lock import pinned_lock

from kew import merge_verify
from kew.config import DispatchConfig, VerifyConfig
from kew.merge_verify import VerifyRefused, VerifyResult, run_verify
from kew.run_store_acceptance import AcceptanceContractRow

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

CONTRACT_PATH = "kew/tests/acceptance/test_issue_7.py"
#: The contract's test as the held-out phase names it.
CONTRACT_TEST = "tests/acceptance/test_issue_7.py::test_ac1"
BASE_TEST = "tests/test_calc.py::test_add"

_CALC = "def add(a, b):\n    return a + b\n"
_BROKEN_CALC = "def add(a, b):\n    return a - b\n"
_BASE_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
#: The base test with a comment added: a head that touches it, nothing more.
_TOUCHED_BASE_TEST = f"# tidied\n{_BASE_TEST}"
_CONTRACT = "from calc import add\n\n\ndef test_ac1():\n    assert add(2, 3) == 5\n"
_WEAKENED_CONTRACT = (
    "from calc import add\n\n\ndef test_ac1():\n    assert add(2, 3) is not None\n"
)


def _run(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    _write(repo, files)
    _run([*_GIT, "add", "--all"], repo)
    _run([*_GIT, "commit", "--quiet", "-m", message], repo)
    return _run(["git", "rev-parse", "HEAD"], repo)


def _repo(tmp_path: Path, head: dict[str, str | None]) -> tuple[Path, str]:
    """A base, a contract commit on the candidate branch, then *head*.

    Returns the repository and the contract commit.
    """
    repo = tmp_path / "project"
    repo.mkdir()
    _run([*_GIT, "init", "--quiet", "-b", "main", "."], repo)
    _commit(
        repo,
        {
            "kew/pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "kew/uv.lock": pinned_lock(),
            "kew/src/calc.py": _CALC,
            "kew/tests/test_calc.py": _BASE_TEST,
            "kew/scripts/verify.sh": "#!/bin/sh\necho GATE RAN\n",
            "scripts/scan-agent-image.sh": "#!/bin/sh\necho PREFLIGHT RAN\n",
        },
        "base",
    )
    _run([*_GIT, "checkout", "--quiet", "-b", "candidate"], repo)
    contract_commit = _commit(repo, {CONTRACT_PATH: _CONTRACT}, "acceptance contract")
    _commit(repo, head, "head")
    return repo, contract_commit


def _contract(
    commit: str, *, text: str = _CONTRACT, path: str = CONTRACT_PATH
) -> AcceptanceContractRow:
    return AcceptanceContractRow(
        id=1,
        epic_ref="epic/1",
        item_id="7",
        version=1,
        path=path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        author_run_id="run-author",
        commit=commit,
        created_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local host's tools, a HOME of its own, and pytest in place of uv."""
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # This interpreter, which runs kew's report plugin as the script.
    monkeypatch.setattr(merge_verify, "HELD_OUT_RUNNER", sys.executable)
    monkeypatch.setattr(merge_verify, "HELD_OUT_PARALLEL", '-m "not serial"')


def _verify(
    repo: Path,
    contract: AcceptanceContractRow | None,
    pr_body: str | None = None,
) -> VerifyResult:
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    _candidate, result = run_verify(
        config, store=None, repo_root=repo, pr_body=pr_body, contract=contract
    )
    return result


def _declaring(*nodes: str) -> str:
    lines = "".join(f"- `{node}`: add now subtracts, on purpose\n" for node in nodes)
    return f"## Behavior changes\n\n{lines}\n## Testing\n\n- ran it\n"


class TestTheContractRunsAsRecorded:
    def test_ac1_a_weakened_contract_copy_still_runs_the_contract_and_fails(
        self, tmp_path: Path
    ) -> None:
        """The head's weakened copy passes; the contract version does not."""
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/tests/test_calc.py": None,
                CONTRACT_PATH: _WEAKENED_CONTRACT,
            },
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert "GATE RAN" in result.log, "the gate itself passed"
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.contract == "tests/acceptance/test_issue_7.py"
        assert result.held_out.undeclared == (CONTRACT_TEST,)
        assert result.held_out.declared == (BASE_TEST,)
        assert result.error is not None
        assert "acceptance contract" in result.error and CONTRACT_TEST in result.error

    def test_ac1_a_contract_failure_is_not_excused_by_the_pr_body(
        self, tmp_path: Path
    ) -> None:
        repo, commit = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, CONTRACT_PATH: _WEAKENED_CONTRACT},
        )
        body = _declaring(BASE_TEST, f"kew/{CONTRACT_TEST}")
        result = _verify(repo, _contract(commit), pr_body=body)
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.undeclared == (CONTRACT_TEST,)

    def test_a_contract_the_head_deleted_still_runs(self, tmp_path: Path) -> None:
        repo, commit = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, CONTRACT_PATH: None},
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.undeclared == (CONTRACT_TEST,)

    def test_the_verify_event_records_the_contract_and_its_failure(
        self, tmp_path: Path
    ) -> None:
        from kew.run_store import SqliteRunStore

        repo, commit = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, CONTRACT_PATH: _WEAKENED_CONTRACT},
        )
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.verify = VerifyConfig(host="local")
        with SqliteRunStore(str(tmp_path / "verify.db")) as store:
            run_verify(
                config,
                store=store,
                repo_root=repo,
                pr_body=_declaring(BASE_TEST),
                contract=_contract(commit),
            )
            events = store.list_audit_events(event=merge_verify.VERIFY_EVENT)
        assert len(events) == 1
        payload = events[0].payload
        held_out = (payload if isinstance(payload, dict) else json.loads(payload))[
            "held_out"
        ]
        assert held_out["outcome"] == "failed"
        assert held_out["contract"] == "tests/acceptance/test_issue_7.py"
        assert held_out["undeclared"] == [CONTRACT_TEST]

    def test_an_honest_head_passes_its_contract(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        result = _verify(repo, _contract(commit))
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.outcome == "passed"
        assert result.held_out.contract == "tests/acceptance/test_issue_7.py"

    def test_head_addopts_that_ignore_the_contract_do_not_skip_it(
        self, tmp_path: Path
    ) -> None:
        """The head's pytest config is the implementer's to edit, so an
        ``--ignore`` in its addopts must not drop the contract from the run."""
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/pyproject.toml": (
                    "[tool.pytest.ini_options]\n"
                    'pythonpath = ["src"]\n'
                    'addopts = "--ignore=tests/acceptance"\n'
                ),
            },
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.undeclared == (CONTRACT_TEST,)

    def test_head_conftest_that_drops_the_contract_fails_the_verification(
        self, tmp_path: Path
    ) -> None:
        """The head's conftest is kept out of the contract's own run, so a
        hook that deselects the contract cannot keep it from running."""
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/conftest.py": (
                    "def pytest_collection_modifyitems(config, items):\n"
                    "    items[:] = [i for i in items if 'acceptance' not in "
                    "i.nodeid]\n"
                ),
            },
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "failed"
        assert result.held_out.undeclared == (CONTRACT_TEST,)

    def test_head_config_that_collects_no_contract_test_fails_the_verification(
        self, tmp_path: Path
    ) -> None:
        """A contract that ran no test is not a contract that passed — and the
        head's config is never read, so the contract runs and its failure is
        named."""
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/pyproject.toml": (
                    "[tool.pytest.ini_options]\n"
                    'pythonpath = ["src"]\n'
                    'python_functions = "check_*"\n'
                ),
            },
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "failed"
        assert result.error is not None
        assert "acceptance contract" in result.error
        assert result.held_out.undeclared == (CONTRACT_TEST,)

    def test_head_conftest_that_rewrites_a_failure_as_a_pass_fails_the_verification(
        self, tmp_path: Path
    ) -> None:
        """The head's conftest hooks are the implementer's to edit, so one that
        reports a failing contract test as passed must not pass the verify."""
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/conftest.py": (
                    "import pytest\n\n\n"
                    "@pytest.hookimpl(wrapper=True)\n"
                    "def pytest_runtest_makereport(item, call):\n"
                    "    rep = yield\n"
                    "    if 'acceptance' in item.nodeid:\n"
                    "        rep.outcome = 'passed'\n"
                    "    return rep\n"
                ),
            },
        )
        result = _verify(repo, _contract(commit), pr_body=_declaring(BASE_TEST))
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.outcome == "failed"
        assert result.held_out.undeclared == (CONTRACT_TEST,)


class TestAShardedContractRun:
    def test_the_contract_runs_once_in_the_gate_job_not_per_shard(
        self,
    ) -> None:
        """The held-out phase judges one contract run, whatever the shards, in
        the gate job that runs before any of them (#3212)."""
        candidate = merge_verify.VerifyCandidate(
            ref="HEAD",
            ref_sha="a" * 40,
            base_ref="origin/main",
            base_sha="b" * 40,
            frontend_changed=False,
            contract=merge_verify.TrustedContract(path=CONTRACT_PATH, text=_CONTRACT),
        )
        shards = [merge_verify.shard_job_script(i, 3, candidate) for i in (1, 2, 3)]
        single = merge_verify.single_job_script(candidate, images=[])
        gate = merge_verify.job_script(candidate, images=[])
        assert [script.count("contract-exit") for script in shards] == [0, 0, 0]
        assert single.count("contract-exit") == 0
        assert gate.count("contract-exit") == 1


class TestWithoutAContract:
    def test_ac2_a_node_without_a_contract_verifies_as_the_held_out_phase_does(
        self, tmp_path: Path
    ) -> None:
        """No contract: the head-only acceptance file is not held out, and a
        declared base failure is excused, exactly as #2485 judges it."""
        repo, _commit_sha = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                CONTRACT_PATH: _WEAKENED_CONTRACT,
                # The head touches the base test, so the held-out phase runs it.
                "kew/tests/test_calc.py": _TOUCHED_BASE_TEST,
            },
        )
        result = _verify(repo, None, pr_body=_declaring(BASE_TEST))
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.contract is None
        assert result.held_out.failed == (BASE_TEST,)
        assert result.held_out.declared == (BASE_TEST,)
        assert "contract" not in result.log

    def test_ac2_an_undeclared_base_failure_still_fails(self, tmp_path: Path) -> None:
        repo, _commit_sha = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/tests/test_calc.py": _TOUCHED_BASE_TEST,
            },
        )
        result = _verify(repo, None)
        assert result.outcome == "failed"
        assert result.held_out is not None
        assert result.held_out.undeclared == (BASE_TEST,)


class TestARefusedContract:
    def test_a_contract_whose_bytes_do_not_match_the_ledger_is_refused(
        self, tmp_path: Path
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        with pytest.raises(VerifyRefused, match="does not match the sha256"):
            _verify(repo, _contract(commit, text=_WEAKENED_CONTRACT))

    def test_a_contract_outside_the_test_tree_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        with pytest.raises(VerifyRefused, match="never run it"):
            _verify(repo, _contract(commit, path="kew/src/calc.py", text=_CALC))

    def test_a_contract_pytest_would_not_collect_is_refused(
        self, tmp_path: Path
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        with pytest.raises(VerifyRefused, match="never run it"):
            _verify(repo, _contract(commit, path="kew/tests/test_calc.py.bak"))

    def test_a_contract_commit_that_does_not_carry_the_file_is_refused(
        self, tmp_path: Path
    ) -> None:
        repo, _commit_sha = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        base = _run(["git", "rev-parse", "main"], repo)
        with pytest.raises(VerifyRefused, match="could not be read"):
            _verify(repo, _contract(base))


SECOND_CONTRACT_PATH = "kew/tests/acceptance/test_issue_8.py"
SECOND_CONTRACT_TEST = "tests/acceptance/test_issue_8.py::test_ac1"
_SECOND_CONTRACT = (
    "from calc import add\n\n\ndef test_ac1():\n    assert add(10, 1) == 11\n"
)
#: Right for the first contract's sums, wrong for the second's.
_CALC_WRONG_ABOVE_TEN = "def add(a, b):\n    return a + b if a < 10 else 0\n"


def _second_contract(repo: Path) -> AcceptanceContractRow:
    """A second node's contract, committed on top of the candidate."""
    commit = _commit(repo, {SECOND_CONTRACT_PATH: _SECOND_CONTRACT}, "contract 8")
    row = _contract(commit, text=_SECOND_CONTRACT, path=SECOND_CONTRACT_PATH)
    return AcceptanceContractRow(**{**row.__dict__, "id": 2, "item_id": "8"})


def _verify_contracts(
    repo: Path, contracts: list[AcceptanceContractRow]
) -> VerifyResult:
    config = DispatchConfig()
    config.logging.dir = str(repo.parent / "logs")
    config.verify = VerifyConfig(host="local")
    _candidate, result = run_verify(
        config, store=None, repo_root=repo, contracts=contracts
    )
    return result


class TestEveryContractOfATrainRuns:
    """A merge-queue train verifies one tree for several nodes (#2814), so
    every node's contract runs in it and each one can fail it."""

    def test_two_contracts_that_both_pass_pass_and_are_both_recorded(
        self, tmp_path: Path
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        second = _second_contract(repo)
        result = _verify_contracts(repo, [_contract(commit), second])
        assert result.outcome == "passed", result.error
        assert result.held_out is not None
        assert result.held_out.contracts == (
            "tests/acceptance/test_issue_7.py",
            "tests/acceptance/test_issue_8.py",
        )

    def test_the_second_contract_failing_fails_the_train(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _CALC_WRONG_ABOVE_TEN})
        second = _second_contract(repo)
        result = _verify_contracts(repo, [_contract(commit), second])
        assert result.outcome == "failed", result.log
        assert result.held_out is not None
        assert result.held_out.undeclared == (SECOND_CONTRACT_TEST,)
        assert result.error is not None
        assert SECOND_CONTRACT_TEST in result.error

    def test_two_different_contracts_at_one_path_are_refused(
        self, tmp_path: Path
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        rewritten = _commit(repo, {CONTRACT_PATH: _WEAKENED_CONTRACT}, "rewrite")
        with pytest.raises(VerifyRefused, match="two different acceptance"):
            _verify_contracts(
                repo,
                [_contract(commit), _contract(rewritten, text=_WEAKENED_CONTRACT)],
            )


class TestTheVerifyCommandRunsTheBoundContract:
    """``kew verify`` binds the candidate to its node through the ledger (#2880)."""

    @staticmethod
    def _invoke(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo: Path,
        args: list[str],
        *,
        contract: AcceptanceContractRow | None = None,
        branch_item: int | None = None,
        epics: tuple[str, ...] = ("epic/1",),
        members: tuple[str, ...] = (),
    ) -> tuple[int, str, list[dict[str, object]]]:
        from typer.testing import CliRunner

        from kew.cli import app
        from kew.run_store import RunRecord, SqliteRunStore

        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        home = tmp_path / "config"
        home.mkdir(exist_ok=True)
        (home / "kew.toml").write_text(
            f'[logging]\ndir = "{logs}"\n\n[verify]\nhost = "local"\n',
            encoding="utf-8",
        )
        with SqliteRunStore(str(logs / "dispatch.db")) as store:
            if contract is not None:
                for epic in epics:
                    store.record_acceptance_contract(
                        epic,
                        contract.item_id,
                        path=contract.path,
                        sha256=contract.sha256,
                        author_run_id=contract.author_run_id,
                        commit=contract.commit,
                    )
            for epic in members:
                # The epic's newest tick lists item 7 as a node of its own.
                store.append_epic_tick(
                    epic_ref=epic,
                    tick_ts=datetime.now(UTC).isoformat(),
                    snapshot={"nodes": [{"id": "7", "status": "in_progress"}]},
                    actions={},
                    snapshot_hash=f"hash-{epic}",
                )
            if branch_item is not None:
                store.save(
                    RunRecord(
                        run_id="run-impl",
                        issue_num=branch_item,
                        branch="candidate",
                        status="success",
                        exit_code=0,
                        started_at=datetime.now(UTC),
                    )
                )
        monkeypatch.setattr("kew.cli.project_root", lambda: repo)
        monkeypatch.setenv("KEW_PROJECT", str(home))
        monkeypatch.chdir(home)
        result = CliRunner().invoke(app, ["verify", *args])
        with SqliteRunStore(str(logs / "dispatch.db")) as store:
            events = store.list_audit_events(event=merge_verify.VERIFY_EVENT)
        payloads = [
            event.payload
            if isinstance(event.payload, dict)
            else json.loads(event.payload)
            for event in events
        ]
        return result.exit_code, result.output, payloads

    def test_the_branchs_contract_fails_the_verify_even_when_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(
            tmp_path,
            {"kew/src/calc.py": _BROKEN_CALC, CONTRACT_PATH: _WEAKENED_CONTRACT},
        )
        body = tmp_path / "body.md"
        body.write_text(
            _declaring(BASE_TEST, f"kew/{CONTRACT_TEST}", CONTRACT_TEST),
            encoding="utf-8",
        )
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate", "--pr-body", str(body)],
            contract=_contract(commit),
            branch_item=7,
            members=("epic/1",),
        )
        assert code == 1, output
        assert f"acceptance contract: {CONTRACT_PATH} v1" in output
        assert f" contract={CONTRACT_PATH}" in output
        (payload,) = payloads
        assert payload["outcome"] == "failed"
        assert payload["contract_selected"] == CONTRACT_PATH
        assert payload["contract_executed"] is True
        assert payload["held_out"]["undeclared"] == [CONTRACT_TEST]  # type: ignore[index]

    def test_an_explicit_item_binds_a_detached_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        head = _run(["git", "rev-parse", "HEAD"], repo)
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            [head, "--item", "7"],
            contract=_contract(commit),
            members=("epic/1",),
        )
        assert code == 1, output
        (payload,) = payloads
        assert payload["contract_selected"] == CONTRACT_PATH
        assert payload["contract_executed"] is True
        assert payload["held_out"]["undeclared"] == [CONTRACT_TEST]  # type: ignore[index]

    def test_a_candidate_with_no_bound_node_runs_no_contract_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        code, output, payloads = self._invoke(
            tmp_path, monkeypatch, repo, ["candidate"], contract=_contract(commit)
        )
        assert code == 0, output
        assert "acceptance contract: none (no node is bound" in output
        assert "contract=none" in output
        (payload,) = payloads
        assert payload["contract_selected"] is None
        assert payload["contract_executed"] is False
        assert payload["held_out"].get("contract") is None  # type: ignore[attr-defined]

    def test_an_item_with_contracts_in_two_epics_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate", "--item", "7"],
            contract=_contract(commit),
            epics=("epic/1", "epic/2"),
        )
        assert code == 2, output
        assert "epic cannot be established" in output
        assert payloads == []

    def test_the_epic_option_settles_an_ambiguous_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/README.md": "docs only\n"})
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate", "--item", "7", "--epic", "epic/2"],
            contract=_contract(commit),
            epics=("epic/1", "epic/2"),
        )
        assert code == 0, output
        assert "(epic epic/2, item 7)" in output
        (payload,) = payloads
        assert payload["contract_selected"] == CONTRACT_PATH
        assert payload["contract_executed"] is True

    def test_an_epic_that_names_no_node_or_contract_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo'd epic never binds the candidate to no contract at all."""
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate", "--item", "7", "--epic", "epic/typo"],
            contract=_contract(commit),
            epics=("epic/1", "epic/2"),
            members=("epic/1", "epic/2"),
        )
        assert code == 2, output
        assert "epic epic/typo has no node and no acceptance contract" in output
        assert payloads == []

    def test_another_epics_contract_is_not_run_for_this_epics_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate"],
            contract=_contract(commit),
            branch_item=7,
            epics=("epic/1",),
            members=("epic/2",),
        )
        assert code == 0, output
        assert "acceptance contract: none (epic epic/2, item 7 has none)" in output
        (payload,) = payloads
        assert payload["contract_selected"] is None

    def test_a_contracted_item_no_epic_lists_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate"],
            contract=_contract(commit),
            branch_item=7,
        )
        assert code == 2, output
        assert "no epic lists it" in output
        assert payloads == []

    def test_a_shared_item_needs_the_epic_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(tmp_path, {"kew/src/calc.py": _BROKEN_CALC})
        shared = {
            "contract": _contract(commit),
            "branch_item": 7,
            "members": ("epic/1", "epic/2"),
        }
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate"],
            **shared,  # type: ignore[arg-type]
        )
        assert code == 2, output
        assert "epic cannot be established (epic/1, epic/2)" in output
        assert payloads == []
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate", "--epic", "epic/1"],
        )
        assert code == 1, output
        (payload,) = payloads
        assert payload["contract_selected"] == CONTRACT_PATH
        assert payload["held_out"]["undeclared"] == [CONTRACT_TEST]  # type: ignore[index]

    def test_a_gate_that_fails_first_selects_the_contract_and_runs_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, commit = _repo(
            tmp_path,
            {
                "kew/src/calc.py": _BROKEN_CALC,
                "kew/scripts/verify.sh": "#!/bin/sh\necho GATE FAILED\nexit 3\n",
            },
        )
        code, output, payloads = self._invoke(
            tmp_path,
            monkeypatch,
            repo,
            ["candidate"],
            contract=_contract(commit),
            branch_item=7,
            members=("epic/1",),
        )
        assert code == 3, output
        assert f"contract_not_run={CONTRACT_PATH}" in output
        (payload,) = payloads
        assert payload["contract_selected"] == CONTRACT_PATH
        assert payload["contract_executed"] is False
