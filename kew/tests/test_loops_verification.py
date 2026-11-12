"""Verification gate: command condition runs in the worktree, exit 0 = met."""

import subprocess
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.loops.ci import CiResult
from kew.loops.verification import (
    DEFAULT_GUARDED_PATHS,
    HOST_OWNED_VERIFICATION_POSTURE,
    UNCONTAINED_VERIFICATION_POSTURE,
    CiGreenCondition,
    CommandCondition,
    ConditionResult,
    Gate,
    GateEvaluator,
    GateVerdict,
    resolve_host_verification_admission,
)


@pytest.fixture(autouse=True)
def _allowlist_sh():
    """The command condition shells out via ``sh`` (and wraps it in the
    allowlisted ``timeout``/``gtimeout`` tool); register only those for the test.

    Registers ``sh`` + ``timeout``/``gtimeout`` directly rather than calling
    ``preflight_tools()`` — which would also run the required-tool check (it
    ``raise SystemExit(1)`` when ``git``/``gh`` are absent) and register
    unrelated tools (curl, ps). Saves/restores TOOL_PATHS so the registration
    doesn't leak into other test modules' allowlist checks.
    """
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("sh")
    timeout_cmd = subprocess_utils.find_timeout_cmd()
    if timeout_cmd:
        subprocess_utils._register_optional_tool(timeout_cmd)
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def test_command_condition_met_on_exit_zero(tmp_path: Path):
    cond = CommandCondition("true")
    result = cond.check(worktree=tmp_path)
    assert isinstance(result, ConditionResult)
    assert result.met is True


def test_command_condition_unmet_on_nonzero_exit(tmp_path: Path):
    cond = CommandCondition("false")
    result = cond.check(worktree=tmp_path)
    assert result.met is False


def test_command_condition_captures_output_as_evidence(tmp_path: Path):
    # stdout+stderr of a failing command becomes the evidence string.
    cond = CommandCondition("echo boom-output >&2; exit 3")
    result = cond.check(worktree=tmp_path)
    assert result.met is False
    assert "boom-output" in result.evidence
    assert "3" in result.evidence  # exit code surfaced


def test_command_condition_runs_in_the_given_worktree(tmp_path: Path):
    # A file present only in the worktree makes the command pass there.
    (tmp_path / "sentinel").write_text("ok")
    cond = CommandCondition("test -f sentinel")
    assert cond.check(worktree=tmp_path).met is True
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert cond.check(worktree=other).met is False


def test_command_condition_refuses_an_agent_written_checkout(tmp_path: Path):
    worktree = tmp_path / "agent-checkout"
    worktree.mkdir()

    result = CommandCondition("touch sentinel").check(
        worktree=worktree,
        agent_written_checkout=True,
    )

    assert result.met is False
    assert "#1444" in result.evidence
    assert "agent-written checkout" in result.evidence
    assert not (worktree / "sentinel").exists()


def test_command_condition_runs_in_an_admitted_agent_checkout(tmp_path: Path):
    worktree = tmp_path / "agent-checkout"
    worktree.mkdir()

    result = CommandCondition("touch sentinel").check(
        worktree=worktree,
        agent_written_checkout=True,
        dangerously_skip_containment=True,
    )

    assert result.met is True
    assert (worktree / "sentinel").is_file()


def test_command_condition_keeps_host_owned_directories_unchanged(tmp_path: Path):
    worktree = tmp_path / "agent" / "issue-1444"
    worktree.mkdir(parents=True)

    result = CommandCondition("touch sentinel").check(worktree=worktree)

    assert result.met is True
    assert (worktree / "sentinel").is_file()


def test_check_directory_redacts_a_proxy_path_from_its_own_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``check`` enters ``proxy_redaction_scope`` before any evidence is
    sanitized, and only inside it is a proxy URL's path segment a credential form
    (a bare segment is not credential-shaped on its own). ``check_directory`` is
    public — the serial merge queue calls it directly and *retains* its evidence
    in a review sidecar — so the scope belongs in the primitive, not in each
    caller (#2173 review round 4)."""
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128/pr0xypathsecret/")
    cond = CommandCondition("printenv https_proxy | cut -d/ -f4; exit 1")

    result = cond.check_directory(tmp_path)

    assert result.met is False
    assert "pr0xypathsecret" not in result.evidence
    assert "<https_proxy redacted>" in result.evidence


def test_command_condition_materializes_the_published_commit_off_checkout(
    tmp_path: Path,
):
    from support.host_git_boundary import record_git_calls

    remote = tmp_path / "remote"
    remote.mkdir()
    _init_repo_with_commit(remote)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (remote / "published.txt").write_text("published\n")
    subprocess.run(["git", "add", "."], cwd=remote, check=True)
    subprocess.run(["git", "commit", "-qm", "published"], cwd=remote, check=True)
    published = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=remote,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    agent_checkout = tmp_path / "agent-checkout"
    agent_checkout.mkdir()
    (agent_checkout / "agent-only.txt").write_text("untrusted\n")
    subprocess_utils._register_optional_tool("git")

    with record_git_calls() as calls:
        result = CommandCondition(
            "test -d .git && test ! -f .git/objects/info/alternates "
            "&& git rev-parse --verify HEAD >/dev/null "
            "&& test -f published.txt && test ! -f agent-only.txt"
        ).check(
            worktree=agent_checkout,
            agent_written_checkout=True,
            dangerously_skip_containment=True,
            published_sha=published,
            publication_base_sha=base,
            publication_remote=str(remote),
        )

    assert result.met is True
    assert not [
        call for call in calls if call.repository().resolves_inside(agent_checkout)
    ]


def test_command_condition_refuses_agent_authored_published_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[Path] = []
    condition = CommandCondition("touch sentinel")
    monkeypatch.setattr(
        condition,
        "_check_published",
        lambda **_kwargs: calls.append(tmp_path) or ConditionResult(True, "unsafe"),
    )

    result = condition.check(
        worktree=tmp_path,
        agent_written_checkout=True,
        published_sha="b" * 40,
        publication_base_sha="a" * 40,
        publication_remote="https://example.invalid/repo.git",
    )

    assert result.met is False
    assert "#1444" in result.evidence
    assert calls == []


@pytest.mark.parametrize(
    ("agent_written_checkout", "dangerous_admission", "posture", "refused"),
    [
        (False, False, HOST_OWNED_VERIFICATION_POSTURE, False),
        (True, True, UNCONTAINED_VERIFICATION_POSTURE, False),
        (True, False, None, True),
    ],
)
def test_host_verification_admission_resolves_posture_and_refusal(
    agent_written_checkout: bool,
    dangerous_admission: bool,
    posture: str | None,
    refused: bool,
):
    admission = resolve_host_verification_admission(
        agent_written_checkout=agent_written_checkout,
        dangerously_skip_containment=dangerous_admission,
    )

    assert admission.posture == posture
    assert (admission.refusal is not None) is refused


def test_command_condition_timeout_is_unmet(tmp_path: Path):
    # Wrapped in the allowlisted `timeout` tool: a `sleep` that overruns is
    # killed (process-group signal) and reported unmet. Falls back to
    # subprocess timeout when no `timeout`/`gtimeout` tool is installed.
    cond = CommandCondition("sleep 5", timeout=0.2)
    result = cond.check(worktree=tmp_path)
    assert result.met is False
    assert "timeout" in result.evidence.lower()


class _FakeCondition:
    """A StoppingCondition stand-in with a fixed result (no subprocess)."""

    def __init__(self, *, met: bool, evidence: str = "") -> None:
        self._result = ConditionResult(met=met, evidence=evidence)

    def check(
        self,
        *,
        worktree: Path,
        agent_written_checkout: bool = False,
        dangerously_skip_containment: bool = False,
        published_sha: str | None = None,
        publication_base_sha: str | None = None,
        publication_remote: str | None = None,
    ) -> ConditionResult:
        return self._result


def test_gate_passes_when_the_single_condition_is_met(tmp_path: Path):
    gate = GateEvaluator([_FakeCondition(met=True)], guarded_paths=[])
    verdict = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[])
    assert isinstance(verdict, GateVerdict)
    assert verdict.passed is True
    assert verdict.sabotage is False
    assert "pass" in verdict.evidence.lower()


def test_gate_fails_when_a_condition_is_unmet(tmp_path: Path):
    gate = GateEvaluator(
        [_FakeCondition(met=False, evidence="2 tests red")], guarded_paths=[]
    )
    verdict = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[])
    assert verdict.passed is False
    assert verdict.sabotage is False
    assert "gate_failed" in verdict.evidence
    assert "2 tests red" in verdict.evidence


def test_gate_and_semantics_all_must_be_met(tmp_path: Path):
    # AND: one unmet condition fails the whole gate even if others pass.
    gate = GateEvaluator(
        [_FakeCondition(met=True), _FakeCondition(met=False, evidence="lint dirty")],
        guarded_paths=[],
    )
    verdict = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[])
    assert verdict.passed is False
    assert "lint dirty" in verdict.evidence


def test_gate_with_no_conditions_does_not_pass(tmp_path: Path):
    # An empty condition list must NOT vacuously pass — that would let the
    # anti-proxy invariant be bypassed by configuring zero conditions.
    gate = GateEvaluator([], guarded_paths=[])
    verdict = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[])
    assert verdict.passed is False
    assert "no conditions" in verdict.evidence.lower()


def test_gate_runs_real_command_condition_end_to_end(tmp_path: Path):
    gate = GateEvaluator([CommandCondition("true")], guarded_paths=[])
    assert (
        gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[]).passed
        is True
    )
    gate_red = GateEvaluator([CommandCondition("false")], guarded_paths=[])
    assert (
        gate_red.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[]).passed
        is False
    )


def test_gate_forwards_agent_checkout_posture_to_command_condition(tmp_path: Path):
    gate = GateEvaluator([CommandCondition("touch sentinel")], guarded_paths=[])

    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=[],
        agent_written_checkout=True,
    )

    assert verdict.passed is False
    assert "#1444" in verdict.evidence
    assert not (tmp_path / "sentinel").exists()


def test_gate_evaluator_satisfies_the_gate_protocol(tmp_path: Path):
    # Plan 2 types its driver param as `Gate`; GateEvaluator must satisfy it
    # structurally. Binding to a `Gate`-annotated name is checked by mypy; here
    # we assert the call shape at runtime.
    gate: Gate = GateEvaluator([_FakeCondition(met=True)], guarded_paths=[])
    verdict = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=[])
    assert isinstance(verdict, GateVerdict)
    assert verdict.passed is True


def test_default_guarded_paths_value():
    # The locked default set: any test file, any conftest.py, any CI workflow.
    assert DEFAULT_GUARDED_PATHS == [
        "**/tests/**",
        "**/test_*.py",
        "**/*_test.py",
        "**/*.test.*",
        "**/*.spec.*",
        "**/AGENTS.md",
        "**/CLAUDE.md",
        "**/conftest.py",
        "**/ratchets/**",
        ".github/workflows/**",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "kew/tests/test_a.py",
        "kew/tests/fixtures/data.json",
        "pkg/src/foo_test.py",
        "pkg/src/test_foo.py",
        "web/src/x.test.ts",
        "web/src/x.spec.ts",
        "pkg/AGENTS.md",
        "AGENTS.md",
        "pkg/CLAUDE.md",
        "CLAUDE.md",
        "kew/ratchets/test_quality.json",
        "kew/ratchets/silence_on_failure_baseline.txt",
        "ratchets/test_quality.json",
    ],
)
def test_default_guarded_paths_flag_tests_at_any_depth(path: str):
    from kew.loops.verification import _matches_guarded

    assert _matches_guarded([path], DEFAULT_GUARDED_PATHS) == [path]


@pytest.mark.parametrize(
    "path",
    [
        "src/kew/testing_utils.py",
        "docs/tests.md",
        "src/kew/contest.py",
        "docs/AGENTS.md.bak",
    ],
)
def test_default_guarded_paths_leave_lookalikes_unflagged(path: str):
    from kew.loops.verification import _matches_guarded

    assert _matches_guarded([path], DEFAULT_GUARDED_PATHS) == []


@pytest.mark.parametrize(
    "path", ["kew/tests/test_a.py", "pkg/src/foo_test.py", "kew/AGENTS.md"]
)
def test_empty_guarded_paths_still_get_any_depth_defaults(tmp_path: Path, path: str):
    # evaluate unions DEFAULT_GUARDED_PATHS, so [] still guards nested tests.
    gate = GateEvaluator([_FakeCondition(met=True)], guarded_paths=[])
    verdict = gate.evaluate(
        worktree=tmp_path, base_ref="main", changed_paths=["src/kew/foo.py", path]
    )
    assert verdict.sabotage is True
    assert verdict.passed is False
    assert path in verdict.evidence


def test_diff_guard_flags_sabotage_when_test_file_edited(tmp_path: Path):
    # The command PASSES (exit 0) but the diff edits a gated test file — the
    # classic "make the gate green by editing the test" attack. Must be caught.
    gate = GateEvaluator(
        [CommandCondition("true")],
        guarded_paths=["tests/test_foo.py"],
    )
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=["src/kew/foo.py", "tests/test_foo.py"],
    )
    assert verdict.sabotage is True
    assert verdict.passed is False  # NOT passed despite exit 0
    assert "sabotage" in verdict.evidence
    assert "tests/test_foo.py" in verdict.evidence


def test_diff_guard_flags_sabotage_for_a_brand_new_test_file(tmp_path: Path):
    # Saboteur evasion: add a NEW always-passing test file not named in the
    # caller's guarded_paths. The DEFAULT `**/tests/**` glob (always unioned) must
    # still catch it. Caller passes NO test path of its own.
    gate = GateEvaluator([CommandCondition("true")], guarded_paths=["verify.sh"])
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=["tests/test_evil.py"],
    )
    assert verdict.sabotage is True
    assert verdict.passed is False
    assert "tests/test_evil.py" in verdict.evidence


def test_diff_guard_flags_sabotage_for_a_conftest_edit(tmp_path: Path):
    # Saboteur evasion: edit a conftest.py (fixtures/collection) to neuter the
    # suite. `**/conftest.py` (always unioned) must catch it at any depth.
    gate = GateEvaluator([CommandCondition("true")], guarded_paths=["verify.sh"])
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=["src/kew/sub/conftest.py"],
    )
    assert verdict.sabotage is True
    assert verdict.passed is False


def test_diff_guard_flags_sabotage_when_verify_script_edited(tmp_path: Path):
    gate = GateEvaluator([CommandCondition("true")], guarded_paths=["verify.sh"])
    verdict = gate.evaluate(
        worktree=tmp_path, base_ref="main", changed_paths=["verify.sh"]
    )
    assert verdict.sabotage is True
    assert verdict.passed is False


def test_diff_guard_flags_sabotage_when_ci_config_edited(tmp_path: Path):
    # DEFAULT_GUARDED_PATHS covers .github/workflows/** via glob.
    gate = GateEvaluator(
        [CommandCondition("true")], guarded_paths=DEFAULT_GUARDED_PATHS
    )
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=[".github/workflows/ci.yml"],
    )
    assert verdict.sabotage is True
    assert verdict.passed is False


def test_diff_guard_empty_guarded_paths_still_protects_ci(tmp_path: Path):
    # A caller passing [] must NOT be able to disable sabotage protection:
    # evaluate ALWAYS unions DEFAULT_GUARDED_PATHS. A CI-config edit still trips.
    gate = GateEvaluator([CommandCondition("true")], guarded_paths=[])
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=[".github/workflows/ci.yml"],
    )
    assert verdict.sabotage is True
    assert verdict.passed is False


def test_diff_guard_allows_clean_diff(tmp_path: Path):
    # Touching only source files is fine: gate passes normally (even though the
    # always-on DEFAULT_GUARDED_PATHS are in effect).
    gate = GateEvaluator(
        [CommandCondition("true")],
        guarded_paths=["tests/test_foo.py"],
    )
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=["src/kew/foo.py", "README.md"],
    )
    assert verdict.sabotage is False
    assert verdict.passed is True


def test_glob_star_does_not_cross_slash():
    # GLOB SEMANTICS (path-aware glob->regex): a single `*` must NOT cross `/`,
    # but `**` must. Tested at the matcher level because evaluate() always
    # unions DEFAULT_GUARDED_PATHS (which includes `tests/**`), so a guard of
    # `tests/*` could not be observed in isolation through evaluate().
    # (Guards against fnmatch's over-broad `*`, which silently crosses `/`, AND
    # against PurePosixPath.match, whose `**` would not recurse on 3.12.)
    from kew.loops.verification import _matches_guarded

    # `*` does NOT cross `/`: a direct child matches, a nested file does not.
    assert _matches_guarded(["tests/x.py"], ["tests/*"]) == ["tests/x.py"]
    assert _matches_guarded(["tests/sub/x.py"], ["tests/*"]) == []
    # `**` DOES cross `/`.
    assert _matches_guarded(["tests/sub/x.py"], ["tests/**"]) == ["tests/sub/x.py"]
    # `**/conftest.py` matches a top-level conftest too (zero leading segments).
    assert _matches_guarded(["conftest.py"], ["**/conftest.py"]) == ["conftest.py"]
    # a non-test path with a similar suffix is NOT a false positive.
    assert _matches_guarded(["src/kew/myconftest.py"], ["**/conftest.py"]) == []


def test_sabotage_short_circuits_before_running_the_command(tmp_path: Path):
    # Even if the command would FAIL, a sabotaging diff is reported as sabotage
    # (not a plain gate failure) — and the verdict is authoritative either way:
    # never passed. We assert the sabotage label wins.
    gate = GateEvaluator([CommandCondition("false")], guarded_paths=["verify.sh"])
    verdict = gate.evaluate(
        worktree=tmp_path, base_ref="main", changed_paths=["verify.sh"]
    )
    assert verdict.sabotage is True
    assert verdict.passed is False


def test_anti_proxy_no_path_passes_without_a_met_condition(tmp_path: Path):
    # The anti-proxy invariant, asserted as a property: across an unmet
    # condition, an empty condition list, and a sabotaging diff, NONE yield
    # passed=True. Only a genuinely met condition + clean diff does.
    unmet = GateEvaluator([_FakeCondition(met=False)], guarded_paths=[])
    empty = GateEvaluator([], guarded_paths=[])
    saboteur = GateEvaluator([_FakeCondition(met=True)], guarded_paths=["verify.sh"])
    for gate, changed in [
        (unmet, []),
        (empty, []),
        (saboteur, ["verify.sh"]),
    ]:
        verdict = gate.evaluate(
            worktree=tmp_path, base_ref="main", changed_paths=changed
        )
        assert verdict.passed is False

    honest = GateEvaluator([_FakeCondition(met=True)], guarded_paths=["verify.sh"])
    assert (
        honest.evaluate(
            worktree=tmp_path, base_ref="main", changed_paths=["src/kew/foo.py"]
        ).passed
        is True
    )


def test_diff_guard_matches_nested_ci_glob(tmp_path: Path):
    # `.github/workflows/**` must match nested workflow files, not just the top
    # level — assert the DEFAULT catches a deeply nested CI file.
    gate = GateEvaluator([_FakeCondition(met=True)], guarded_paths=[])
    verdict = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=[".github/workflows/nested/deploy.yml"],
    )
    assert verdict.sabotage is True


def _init_repo_with_commit(path):
    """Create a real git repo with one commit so rev-parse HEAD resolves."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


class TestCiGreenCondition:
    @pytest.fixture(autouse=True)
    def _git_allowed(self):
        # CiGreenCondition.check shells out to ``git`` via subprocess_utils, but
        # this module's autouse ``_allowlist_sh`` fixture only registers
        # sh/timeout. Register git too; save/restore TOOL_PATHS so it doesn't
        # leak into other test modules. WITHOUT this, resolve_cmd raises
        # ValueError("Unknown executable 'git'") and every test below fails or
        # passes for the wrong reason.
        saved = subprocess_utils.TOOL_PATHS.copy()
        subprocess_utils._register_optional_tool("git")
        yield
        subprocess_utils.TOOL_PATHS.clear()
        subprocess_utils.TOOL_PATHS.update(saved)

    def _cond(self, result: CiResult) -> CiGreenCondition:
        return CiGreenCondition(
            repo="o/r",
            timeout_secs=60,
            gated_checks=["test", "agent-image-scan"],
            _wait=lambda *a, **k: result,
        )

    def test_awaiting_approval_is_not_met_and_not_failed(self, tmp_path):
        evidence = "awaiting ci-approved: tests did not run (test)"
        res = self._cond(CiResult("awaiting_approval", evidence)).check(
            worktree=tmp_path, published_sha="a" * 40
        )
        assert res.met is False
        assert res.waiting is True
        assert res.evidence == evidence
        verdict = GateEvaluator(
            [self._cond(CiResult("awaiting_approval", evidence))], guarded_paths=[]
        ).evaluate(
            worktree=tmp_path,
            base_ref="HEAD",
            changed_paths=[],
            published_sha="a" * 40,
        )
        assert verdict.passed is False
        assert verdict.evidence == f"gate_waiting: {evidence}"
        assert "gate_failed" not in verdict.evidence

    def test_a_failure_is_not_waiting(self, tmp_path):
        res = self._cond(CiResult("failure", "CI failed: test")).check(
            worktree=tmp_path, published_sha="a" * 40
        )
        assert res.met is False and res.waiting is False

    def test_refuses_an_agent_written_checkout_before_git(self, tmp_path, monkeypatch):
        worktree = tmp_path / "agent-checkout"
        worktree.mkdir()
        calls = []
        monkeypatch.setattr(
            subprocess_utils,
            "run",
            lambda *args, **kwargs: (
                calls.append((args, kwargs))
                or subprocess.CompletedProcess(args[0], 0, "a" * 40, "")
            ),
        )

        res = self._cond(CiResult("success", "should not be reached")).check(
            worktree=worktree,
            agent_written_checkout=True,
        )

        assert res.met is False
        assert "#1444" in res.evidence
        assert "agent-written checkout" in res.evidence
        assert calls == []

    def test_runs_git_in_an_admitted_agent_checkout(self, tmp_path):
        worktree = tmp_path / "agent-checkout"
        worktree.mkdir()
        _init_repo_with_commit(worktree)

        res = self._cond(CiResult("success", "CI green")).check(
            worktree=worktree,
            agent_written_checkout=True,
            dangerously_skip_containment=True,
        )

        assert res.met is True

    def test_polls_a_canonical_sha_without_checkout_admission(self, tmp_path):
        res = self._cond(CiResult("success", "CI green")).check(
            worktree=tmp_path,
            agent_written_checkout=True,
            published_sha="a" * 40,
        )

        assert res.met is True

    def test_success_is_met(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        res = self._cond(CiResult("success", "CI green (3 checks)")).check(
            worktree=tmp_path
        )
        assert res.met is True
        assert "green" in res.evidence

    def test_failure_is_unmet(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        res = self._cond(CiResult("failure", "CI failed: lint")).check(
            worktree=tmp_path
        )
        assert res.met is False
        assert "lint" in res.evidence

    def test_no_checks_is_unmet(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        res = self._cond(CiResult("no_checks", "no CI checks")).check(worktree=tmp_path)
        assert res.met is False

    def test_timeout_is_unmet(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        res = self._cond(CiResult("timeout", "still pending")).check(worktree=tmp_path)
        assert res.met is False

    def test_unresolvable_head_is_unmet(self, tmp_path, monkeypatch):
        # No git repo at tmp_path -> git rev-parse HEAD fails -> fail closed.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        res = self._cond(CiResult("success", "should not be reached")).check(
            worktree=tmp_path
        )
        assert res.met is False
        assert "HEAD" in res.evidence

    def test_passes_resolved_sha_to_wait(self, tmp_path):
        _init_repo_with_commit(tmp_path)
        seen = {}

        def _wait(repo, sha, *, timeout_secs, gated_checks):
            seen["repo"], seen["sha"], seen["t"] = repo, sha, timeout_secs
            seen["gated"] = gated_checks
            return CiResult("success", "ok")

        CiGreenCondition(
            repo="o/r",
            timeout_secs=42,
            gated_checks=["test", "agent-image-scan"],
            _wait=_wait,
        ).check(worktree=tmp_path)
        assert seen["repo"] == "o/r"
        assert seen["t"] == 42
        assert len(seen["sha"]) == 40  # full git sha
        # The gate decides with the configured approval-gated checks.
        assert seen["gated"] == ("test", "agent-image-scan")

    def test_uses_the_recorded_published_sha_without_reading_the_checkout(
        self, tmp_path, monkeypatch
    ):
        published = "d" * 40
        seen = {}

        def _wait(repo, sha, *, timeout_secs, gated_checks):
            seen["repo"], seen["sha"], seen["t"] = repo, sha, timeout_secs
            seen["gated"] = gated_checks
            return CiResult("success", "ok")

        monkeypatch.setattr(
            subprocess_utils,
            "run",
            lambda *_args, **_kwargs: pytest.fail("checkout git must not run"),
        )

        result = CiGreenCondition(
            repo="o/r",
            timeout_secs=42,
            gated_checks=["test", "agent-image-scan"],
            _wait=_wait,
        ).check(
            worktree=tmp_path / "agent-checkout",
            published_sha=published,
        )

        assert result.met is True
        assert seen == {
            "repo": "o/r",
            "sha": published,
            "t": 42,
            "gated": ("test", "agent-image-scan"),
        }


@pytest.fixture
def contained_command(monkeypatch, tmp_path):
    """Mock every subprocess; reject any host command execution."""
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from kew import sandbox
    from kew.config import SandboxConfig
    from kew.loops import verification

    assert callable(getattr(GateEvaluator, "prepare_verification", None))
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: "/bin/docker")
    inspected = Mock(return_value="sha256:" + "d" * 64)
    monkeypatch.setattr(verification, "require_image_id", inspected)
    monkeypatch.setattr(
        verification,
        "provision",
        lambda *a, **k: SimpleNamespace(path=tmp_path / "repo"),
    )
    monkeypatch.setattr(verification, "fetch_commit", lambda *a, **k: None)
    calls = []
    completed = SimpleNamespace(code=0, stdout="out-tail", stderr="err-tail")
    launch_name = None

    def run(argv, **kwargs):
        nonlocal launch_name
        calls.append((argv, kwargs))
        if argv[0] == "git":
            argv = [argv[0], *argv[7:]]  # Host preparation adds three -c controls.
            if argv[1] == "clone":
                assert argv[2:7] == [
                    "--no-hardlinks",
                    "--no-checkout",
                    "--branch",
                    "verification",
                    "--",
                ]
                checkout = Path(argv[-1])
                checkout.mkdir()
                (checkout / ".git").mkdir()
                (checkout / "verify.sh").write_text("#!/bin/sh\ntouch HOST_PWNED\n")
            elif argv[3] == "update-ref":
                assert argv[4:] == ["refs/heads/verification", "b" * 40]
            else:
                assert argv[3:6] == ["checkout", "--detach", "--force"]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["docker", "inspect"]:
            assert argv[-1] == "a" * 64
            return subprocess.CompletedProcess(
                argv,
                getattr(completed, "inspect_code", 0),
                json.dumps(
                    {
                        "Name": "/" + launch_name,
                        "State": getattr(
                            completed,
                            "state",
                            {"Status": "exited", "ExitCode": completed.code},
                        ),
                    }
                ),
                "",
            )
        if argv[:2] == ["docker", "rm"]:
            assert argv[2:-1] in ([], ["-f"])
            assert argv[-1] == "a" * 64
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[:2] == ["docker", "run"], "agent code reached host subprocess"
        launch_name = argv[argv.index("--name") + 1]
        Path(argv[argv.index("--cidfile") + 1]).write_text("a" * 64)
        return subprocess.CompletedProcess(
            argv, completed.code, completed.stdout, completed.stderr
        )

    monkeypatch.setattr(subprocess_utils, "run", run)
    monkeypatch.setattr(
        subprocess_utils,
        "run_bounded",
        lambda argv, *, deadline, output_limit, **kwargs: run(
            argv, timeout=deadline - __import__("time").monotonic()
        ),
    )
    condition = CommandCondition("./verify.sh")
    host = Mock(side_effect=AssertionError("agent code reached host command path"))
    monkeypatch.setattr(condition, "check_directory", host)
    gate = GateEvaluator([condition], guarded_paths=[])
    gate.prepare_verification(
        SandboxConfig(enabled=True, images={"opencode": "approved:tag"}),
        runner="opencode",
    )
    inspected.assert_called_once_with(
        "/bin/docker",
        "approved:tag",
        runner="opencode",
        refusal="command verification refused",
    )
    return condition, gate, calls, completed, host


def _check_contained_published(condition, tmp_path):
    return condition.check(
        worktree=tmp_path / "agent",
        agent_written_checkout=True,
        published_sha="b" * 40,
        publication_base_sha="a" * 40,
        publication_remote="/trusted/remote.git",
    )


def test_contained_verification_argv_and_agent_script_never_run_on_host(
    contained_command, tmp_path
):
    import os

    condition, _, calls, _, host = contained_command
    result = _check_contained_published(condition, tmp_path)
    assert result.met
    host.assert_not_called()
    argv, kwargs = next((argv, kwargs) for argv, kwargs in calls if argv[1] == "run")
    assert argv[-3:] == ["sha256:" + "d" * 64, "-c", "./verify.sh"]
    assert argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "-v"]
    assert len(mounts) == 1 and mounts[0].endswith("/checkout:/work:ro")
    env = [argv[i + 1] for i, value in enumerate(argv) if value == "-e"]
    assert set(env) == {"HOME=/tmp"} | {
        f"{name}="
        for base in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")
        for name in (base, base.lower())
    }
    assert "--read-only" in argv and "--pull=never" in argv
    assert "--cpus" in argv
    assert 0 < kwargs["timeout"] <= 600
    assert not (tmp_path / "HOST_PWNED").exists()


@pytest.mark.parametrize("code", [0, 7, 124, 125])
def test_contained_verification_exit_status_and_redacted_tails(
    contained_command, tmp_path, monkeypatch, code
):
    condition, _, _, completed, host = contained_command
    monkeypatch.setenv("VERIFY_API_KEY", "sentinel-credential-value")
    completed.code = code
    completed.stdout = "x" * 600 + "out-tail sentinel-credential-value"
    completed.stderr = "y" * 600 + "err-tail sentinel-credential-value"
    result = _check_contained_published(condition, tmp_path)
    assert result.met is (code == 0)
    assert f"command exited {code}" in result.evidence
    assert "out-tail" in result.evidence and "err-tail" in result.evidence
    assert "sentinel-credential-value" not in result.evidence
    assert "<VERIFY_API_KEY redacted>" in result.evidence
    assert len(result.evidence) < 600
    host.assert_not_called()


@pytest.mark.parametrize("removed", [True, False])
def test_contained_verification_signalled_client_fails_safely(
    tmp_path, monkeypatch, capsys, removed
):
    import sys

    from kew import sandbox
    from kew.loops import verification

    runtime = tmp_path / "fake-runtime"
    removal_log = tmp_path / "removed"
    cidfile = tmp_path / "client.cid"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys\n"
        "if sys.argv[1] == 'rm':\n"
        "    assert sys.argv[-1] == 'a' * 64\n"
        f"    assert os.path.exists({str(cidfile)!r})\n"
        f"    open({str(removal_log)!r}, 'w').write(sys.argv[-1])\n"
        f"    sys.exit({0 if removed else 1})\n"
        "if sys.argv[1] == 'ps':\n"
        "    print('abc123')\n"
        "    sys.exit(0)\n"
        "assert sys.argv[1] == 'run'\n"
        f"open({str(cidfile)!r}, 'w').write('a' * 64)\n"
        "print('sentinel-secret-value /Users/private-owner/repo', flush=True)\n"
        "print('sentinel-secret-value /home/private-owner/repo', "
        "file=sys.stderr, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(runtime))
    monkeypatch.setattr(sandbox, "_daemon_cpu_count", lambda *a: 4)
    monkeypatch.setattr(sandbox, "_new_container_cidfile", lambda *a: cidfile)
    monkeypatch.setenv("VERIFY_API_KEY", "sentinel-secret-value")
    condition = CommandCondition("./verify.sh", timeout=5)
    admission = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )
    result = condition._check_contained(
        tmp_path, admission, timeout=condition._requested_timeout()
    )

    assert not result.met
    assert removal_log.exists(), "the signalled client must trigger container removal"
    assert removal_log.read_text() == "a" * 64
    assert cidfile.exists() is not removed
    assert "signal 9" in result.evidence
    assert "sentinel-secret-value" not in result.evidence
    assert "private-owner" not in result.evidence
    assert "<VERIFY_API_KEY redacted>" in result.evidence
    warning = capsys.readouterr().err
    if not removed:
        assert "kew-verify-" in warning
        assert "cidfile is kept" in warning


@pytest.mark.parametrize("code", [0, 3])
@pytest.mark.parametrize(
    "stderr",
    [
        "Cannot connect to the Docker daemon",
        "unable to connect to Podman socket: EOF",
        "error during connect",
        "error waiting for container: unexpected EOF",
        "error hijack",
        "daemon is unavailable",
        "daemon disconnected",
        *[
            f"daemon connection {failure}"
            for failure in ("lost", "closed", "reset", "refused", "broken", "eof")
        ],
    ],
)
def test_contained_verification_project_stderr_does_not_set_runtime_health(
    tmp_path, monkeypatch, code, stderr
):
    import json
    import shlex
    import sys

    from kew import sandbox
    from kew.loops import verification

    runtime = tmp_path / "fake-runtime"
    statefile = tmp_path / "state.json"
    control_log = tmp_path / "controls.jsonl"
    cidfile = tmp_path / "client.cid"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        f"state = Path({str(statefile)!r})\n"
        f"cidfile = Path({str(cidfile)!r})\n"
        "if sys.argv[1] == 'run':\n"
        "    cidfile.write_text('a' * 64)\n"
        "    result = subprocess.run(['/bin/sh', '-c', sys.argv[-1]])\n"
        "    state.write_text(json.dumps({'Name': '/' + "
        "sys.argv[sys.argv.index('--name') + 1], "
        "'State': {'Status': 'exited', 'ExitCode': result.returncode}}))\n"
        "    if '--rm' in sys.argv:\n"
        "        state.unlink()\n"
        "    sys.exit(result.returncode)\n"
        "assert cidfile.exists()\n"
        "assert sys.argv[-1] == 'a' * 64\n"
        f"with open({str(control_log)!r}, 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'inspect':\n"
        "    print(state.read_text())\n"
        "elif sys.argv[1] == 'rm':\n"
        "    state.unlink(missing_ok=True)\n"
        "else:\n"
        "    raise AssertionError(sys.argv)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(runtime))
    monkeypatch.setattr(sandbox, "_daemon_cpu_count", lambda *a: 4)
    monkeypatch.setattr(sandbox, "_new_container_cidfile", lambda *a: cidfile)
    condition = CommandCondition(
        f"printf '%s\\n' {shlex.quote(stderr)} >&2; exit {code}", timeout=5
    )
    admission = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )

    result = condition._check_contained(
        tmp_path, admission, timeout=condition._requested_timeout()
    )

    assert result.met is (code == 0)
    assert "runtime client" not in result.evidence
    assert f"command exited {code}" in result.evidence
    assert stderr in result.evidence
    controls = [json.loads(line) for line in control_log.read_text().splitlines()]
    assert [cmd[0] for cmd in controls] == ["inspect", "rm"]
    assert controls[0][1:] == [
        "--type",
        "container",
        "--format",
        '{"Name":{{json .Name}},"State":{{json .State}}}',
        "a" * 64,
    ]
    assert controls[1] == ["rm", controls[0][-1]]  # No abnormal forced removal.
    assert not cidfile.exists()
    assert not statefile.exists()


@pytest.mark.parametrize("inspection", ["running", "failed", "mismatched"])
@pytest.mark.parametrize("removed", [True, False])
def test_contained_verification_unconfirmed_completion_fails(
    contained_command, tmp_path, monkeypatch, inspection, removed
):
    from kew import sandbox

    condition, _, calls, completed, host = contained_command
    completed.state = {"Status": "running", "ExitCode": 0}
    if inspection == "mismatched":
        completed.state = {"Status": "exited", "ExitCode": 3}
    completed.inspect_code = int(inspection == "failed")
    cidfile = tmp_path / "client.cid"
    cidfile.write_text("a" * 64)
    monkeypatch.setattr(sandbox, "_new_container_cidfile", lambda *a: cidfile)
    removals = []

    def remove(argv):
        assert cidfile.exists()
        removals.append(argv)
        return removed

    monkeypatch.setattr(sandbox, "_remove_launched_container", remove)
    result = _check_contained_published(condition, tmp_path)

    assert not result.met
    assert "container completion could not be confirmed" in result.evidence
    assert len(removals) == 1
    assert cidfile.exists() is not removed
    inspections = [kwargs for argv, kwargs in calls if argv[1] == "inspect"]
    assert inspections == [
        {"check": False, "timeout": sandbox.RUNTIME_CONTROL_TIMEOUT_SECONDS}
    ]
    host.assert_not_called()


def test_contained_verification_daemon_disconnect_cannot_report_success(
    contained_command, tmp_path, monkeypatch
):
    from kew import sandbox

    condition, _, _, completed, host = contained_command
    completed.code = 0
    completed.stderr = "error waiting for container: unexpected EOF"
    completed.state = {"Status": "running", "ExitCode": 0}
    removals = []
    monkeypatch.setattr(
        sandbox,
        "_remove_launched_container",
        lambda argv: removals.append(argv) or True,
    )
    result = _check_contained_published(condition, tmp_path)
    assert not result.met
    assert "container completion could not be confirmed" in result.evidence
    assert len(removals) == 1
    host.assert_not_called()


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("docker", 600), OSError("launch failed")]
)
def test_contained_launch_failure_never_falls_back(
    contained_command, tmp_path, monkeypatch, failure
):
    condition, _, _, _, host = contained_command
    from unittest.mock import Mock

    monkeypatch.setattr(
        "kew.loops.verification.run_container_with_cpu_cap_retry",
        Mock(side_effect=failure),
    )
    result = _check_contained_published(condition, tmp_path)
    assert not result.met
    host.assert_not_called()


def test_contained_command_explicit_write_declaration(contained_command, tmp_path):
    condition, gate, calls, _, _ = contained_command
    assert (
        "needs_writes" in __import__("inspect").signature(CommandCondition).parameters
    )
    writable = CommandCondition("./verify.sh", needs_writes=True, timeout=12)
    writable.admission = gate.verification_admission
    assert _check_contained_published(writable, tmp_path).met
    argv, kwargs = next((argv, kwargs) for argv, kwargs in calls if argv[1] == "run")
    assert any(value.endswith("/checkout:/work:rw") for value in argv)
    assert "--read-only" in argv
    assert 0 < kwargs["timeout"] <= 12


@pytest.mark.parametrize(
    "enabled,dangerous",
    [(True, False), ("auto", False), ("auto", True), (False, False), (False, True)],
)
@pytest.mark.parametrize("missing", [None, "runtime", "mapping", "image"])
def test_command_verification_admission_matrix(
    monkeypatch, enabled, dangerous, missing
):
    from kew import sandbox
    from kew.config import SandboxConfig
    from kew.loops import verification

    resolver = getattr(verification, "resolve_command_verification_admission", None)
    assert callable(resolver), "command verification needs containment-first admission"
    monkeypatch.setattr(
        sandbox,
        "detect_runtime_cached",
        lambda _config: None if missing == "runtime" else "/bin/docker",
    )

    def inspect(runtime, image, **kwargs):
        if runtime is None or missing == "image":
            raise sandbox.ContainedImageUnresolvedError("local image unavailable")
        return "sha256:" + "d" * 64

    monkeypatch.setattr(verification, "require_image_id", inspect)
    config = SandboxConfig(
        enabled=enabled,
        dangerously_skip_containment=dangerous,
        images={} if missing == "mapping" else {"opencode": "approved:tag"},
    )
    admission = resolver(config, runner="opencode")
    if missing is None:
        assert admission.posture == "contained_verification"
        assert admission.image == "sha256:" + "d" * 64
    elif dangerous:
        assert admission.posture == "host_verification"
        assert admission.image is None
    else:
        assert admission.posture is None
        assert "#1946 refusal:" in admission.refusal
        assert "dangerously_skip_containment" in admission.refusal


def test_published_command_cannot_claim_trusted_files_to_bypass_admission(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    condition = CommandCondition("./verify.sh")
    published = Mock(return_value=ConditionResult(True, "unsafe host execution"))
    monkeypatch.setattr(condition, "_check_published", published)
    result = condition.check(worktree=tmp_path, published_sha="b" * 40)
    assert result.met is False
    published.assert_not_called()


@pytest.mark.parametrize("dangerous", [False, True])
def test_published_host_fallback_requires_missing_prerequisites_and_flag(
    contained_command, tmp_path, monkeypatch, dangerous
):
    from unittest.mock import Mock

    from kew import sandbox
    from kew.config import SandboxConfig
    from kew.loops import verification

    condition, gate, calls, _, host = contained_command
    monkeypatch.setattr(sandbox, "detect_runtime_cached", lambda _config: None)
    monkeypatch.setattr(verification, "require_image_id", sandbox.require_image_id)
    gate.prepare_verification(
        SandboxConfig(dangerously_skip_containment=dangerous), runner="claude-code"
    )
    container = Mock(side_effect=AssertionError("missing runtime cannot execute"))
    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", container)
    host.side_effect = None
    host.return_value = ConditionResult(True, "admitted host result")
    result = _check_contained_published(condition, tmp_path)
    assert result.met is dangerous
    assert host.call_count == int(dangerous)
    assert len(calls) == 3 * int(dangerous)  # Only admitted execution materializes.
    container.assert_not_called()


@pytest.mark.parametrize("attack", ["hook", "smudge", "template"])
def test_verification_preparation_disables_ambient_git(tmp_path, monkeypatch, attack):
    from support.host_git_boundary import record_git_calls

    from kew.loops import verification

    remote = tmp_path / "remote"
    remote.mkdir()
    _init_repo_with_commit(remote)
    subprocess_utils._register_optional_tool("git")

    def git(*args):
        return subprocess_utils.run(["git", "-C", str(remote), *args]).stdout.strip()

    base = git("rev-parse", "HEAD")
    marker = tmp_path / "HOST_PWNED"
    script = f"#!/bin/sh\necho pwned > '{marker}'\n"
    hooks = remote / ".githooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text(script)
    (hooks / "post-checkout").chmod(0o755)
    (remote / ".gitattributes").write_text("payload filter=attack\n")
    (remote / "payload").write_text("original\n")
    git("add", ".")
    published = git("commit-tree", git("write-tree"), "-p", base, "-m", "payload")
    home = tmp_path / "operator-home"
    home.mkdir()
    config = {
        "hook": "[core]\n hooksPath = .githooks\n",
        "smudge": f'[filter "attack"]\n smudge = "echo pwned > {marker}; cat"\n',
        "template": f"[init]\n templateDir = {tmp_path / 'template'}\n",
    }[attack]
    template_hooks = tmp_path / "template" / "hooks"
    template_hooks.mkdir(parents=True)
    (template_hooks / "post-checkout").write_text(script)
    (template_hooks / "post-checkout").chmod(0o755)
    (home / ".gitconfig").write_text(config)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    condition = CommandCondition("./verify.sh")
    condition.admission = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )
    checked = []

    def contained(checkout, admission, *, timeout=None):
        checked.append((checkout / "payload").read_text())
        return ConditionResult(True, "contained check")

    monkeypatch.setattr(condition, "_check_contained", contained)
    with record_git_calls() as calls:
        paths = verification.published_changed_paths(
            remote=str(remote), base_sha=base, published_sha=published
        )
        result = condition.check(
            worktree=tmp_path / "agent",
            published_sha=published,
            publication_base_sha=base,
            publication_remote=str(remote),
        )
    assert not marker.exists(), "ambient Git executed agent code on the host"
    assert result.met, result.evidence
    assert checked == ["original\n"]
    assert ".githooks/post-checkout" in paths
    guarded = GateEvaluator([], guarded_paths=[".githooks/**"]).evaluate(
        worktree=remote,
        base_ref=base,
        changed_paths=paths,
    )
    assert guarded.sabotage
    assert {call.subcommand for call in calls} == {
        "init",
        "fetch",
        "rev-parse",
        "diff",
        "update-ref",
        "clone",
        "checkout",
    }
    for call in calls:
        env = call.effective_env
        assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TEMPLATE_DIR"] == ""
        assert Path(env["HOME"]).name == "git-home"
        assert env["XDG_CONFIG_HOME"] == env["HOME"]
        assert call.argv[1] == "-c"
        assert call.argv[2].endswith("/git-hooks")
        assert "core.fsmonitor=false" in call.argv


@pytest.mark.serial
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_contained_verification_stops_output_flood(tmp_path, monkeypatch, stream):
    import sys
    import time

    from kew.loops import verification

    # A throttled endless producer keeps the regression safe on the old code.
    # The fake runtime logs removal without starting Docker.
    runtime = tmp_path / "fake-runtime"
    removed = tmp_path / "removed"
    runtime.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if sys.argv[1] == 'rm':\n"
        "    assert sys.argv[-1] == 'a' * 64\n"
        f"    open({str(removed)!r}, 'w').write(sys.argv[-1])\n"
        "    sys.exit(0)\n"
        "open(sys.argv[sys.argv.index('--cidfile') + 1], 'w').write('a' * 64)\n"
        f"fd = {1 if stream == 'stdout' else 2}\n"
        "while True:\n"
        "    os.write(fd, (b'X sentinel-secret-value\\n') * 160)\n"
        "    time.sleep(0.001)\n"
    )
    runtime.chmod(0o755)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(runtime))
    monkeypatch.setenv("VERIFY_API_KEY", "sentinel-secret-value")
    peaks = []
    buffer_type = getattr(subprocess_utils, "BoundedOutput", None)
    if buffer_type is not None:
        original_append = buffer_type.append

        def observe(buffer, chunk):
            original_append(buffer, chunk)
            peaks.append((len(buffer.head), len(buffer.tail), buffer.total))

        monkeypatch.setattr(buffer_type, "append", observe)
    condition = CommandCondition("yes X", timeout=3)
    admission = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )
    start = time.monotonic()
    result = condition._check_contained(
        tmp_path, admission, timeout=condition._requested_timeout()
    )
    assert "output-budget refusal" in result.evidence
    assert not result.met
    assert time.monotonic() - start < 2.5
    assert removed.read_text() == "a" * 64
    assert "sentinel-secret-value" not in result.evidence
    assert "sentinel-" not in result.evidence
    assert "redacted>" in result.evidence
    assert len(result.evidence) < 650
    assert peaks
    assert max(head + tail for head, tail, _ in peaks) <= 262144
    assert max(total for _, _, total in peaks) == 262145


def test_check_admitted_directory_contains_or_refuses_but_never_leaks_to_host(
    monkeypatch, tmp_path
):
    """#2184: the merge queue's seam onto this primitive. A contained admission
    executes in the image; a refused one is returned as an unrunnable result
    and reaches no host child at all — a caller holding an admission must not
    be able to run agent code on the host by calling the runner directly."""
    from unittest.mock import Mock

    from kew.loops import verification

    host = Mock(side_effect=AssertionError("must not run on the host"))
    monkeypatch.setattr(CommandCondition, "_run_in_directory", host)
    launched = []

    def launch(argv, **kwargs):
        launched.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", launch)
    contained = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )
    condition = CommandCondition("./verify.sh", timeout=900)

    result = condition.check_admitted_directory(tmp_path, contained)

    assert result.met
    assert launched[0][0][:2] == ["docker", "run"]
    # The caller's budget is the caller's: the loop gate caps an operator's
    # timeout, the merge queue's 1800s constant is not operator input.
    assert launched[0][1]["timeout"] == 900

    refused = verification.HostVerificationAdmission(
        posture=None, refusal="#1946 refusal: no image"
    )
    denied = condition.check_admitted_directory(tmp_path, refused)

    assert (denied.met, denied.unrunnable) == (False, True)
    assert "#1946 refusal" in denied.evidence
    host.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "flag"),
    [
        (subprocess.TimeoutExpired("docker", 1), "timed_out"),
        (OSError("no runtime"), "unrunnable"),
    ],
)
def test_a_contained_launch_that_never_ran_says_so(
    monkeypatch, tmp_path, failure, flag
):
    """A container that timed out or could not start proves nothing about the
    project under test. The merge queue reads these flags to withhold a merge
    as an operator problem instead of recording a defect in someone's PR."""
    from kew.loops import verification

    def launch(argv, **kwargs):
        raise failure

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", launch)
    admission = verification.HostVerificationAdmission(
        posture=verification.CONTAINED_VERIFICATION_POSTURE,
        refusal=None,
        runtime="docker",
        image="sha256:" + "d" * 64,
    )

    result = CommandCondition("./verify.sh", timeout=30).check_admitted_directory(
        tmp_path, admission
    )

    assert not result.met
    assert getattr(result, flag) is True


def test_a_runs_extra_guarded_path_is_sabotage_when_edited(tmp_path: Path):
    """#2729: the node's acceptance file joins the guard for that run, even
    where no default glob would reach it."""
    gate = GateEvaluator([_FakeCondition(met=True)], guarded_paths=[])
    changed = ["acceptance/contract_7.py", "src/foo.py"]

    unguarded = gate.evaluate(worktree=tmp_path, base_ref="main", changed_paths=changed)
    guarded = gate.evaluate(
        worktree=tmp_path,
        base_ref="main",
        changed_paths=changed,
        extra_guarded_paths=("acceptance/contract_7.py",),
    )

    assert unguarded.passed is True
    assert guarded.passed is False
    assert guarded.sabotage is True
    assert guarded.evidence == "sabotage: diff edits acceptance/contract_7.py"


def test_a_red_contained_command_keeps_its_whole_output_redacted(
    contained_command, tmp_path
):
    """The evidence keeps each stream's tail; ``output`` keeps every line, so
    an error printed before a screen of warnings is still there (#3495)."""
    condition, _, _, completed, _ = contained_command
    completed.code = 1
    completed.stdout = (
        "src/lib/fixtures.test.ts:120:7\n"
        "Error: 'needsYou' does not exist token=ghp_"
        + "A" * 36
        + "\n"
        + "Warn: state_referenced_locally\n" * 40
    )
    result = _check_contained_published(condition, tmp_path)
    assert not result.met
    assert "needsYou" not in result.evidence
    assert result.output.splitlines()[:2] == [
        "src/lib/fixtures.test.ts:120:7",
        "Error: 'needsYou' does not exist token=<redacted>",
    ]
    assert result.output.count("Warn: state_referenced_locally") == 40
