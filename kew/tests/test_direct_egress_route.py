"""Contained dispatch audit regressions for permanent direct egress acceptance."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kew.config import DispatchConfig


def test_recorded_egress_values_are_bounded_by_route(tmp_path: Path) -> None:
    """The audit writer cannot promote direct egress with a caller's value."""
    from kew import sandbox
    from kew.dispatch_core import _DispatchContext, _emit_sandbox_resolved
    from kew.run_store import SqliteRunStore

    ctx = MagicMock(spec=_DispatchContext)
    ctx.config = DispatchConfig()
    ctx.run_id = "egress-matrix-1945"
    ctx.issue_num = 1945
    ctx.item_id = "1945"
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        ctx.store = store
        # Default direct admission must override a caller's stronger value.
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="docker",
            image="kew-agent",
            egress_verification="sidecar-certified",
        )
        [direct] = store.list_audit_events(event="sandbox_resolved")
        assert direct.payload["egress_verification"] == "operator-managed-unverified"

        frozen = sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
        supplied_values = (
            frozen,
            "unavailable",
            "open",
            "sidecar-certified",
            "operator-verified",
        )
        for route in ("direct", "sidecar"):
            ctx.run_id = f"matrix-{route}"
            for supplied in supplied_values:
                _emit_sandbox_resolved(
                    ctx,
                    "claude-code",
                    posture="sandboxed",
                    runtime="docker",
                    image="kew-agent",
                    egress_route=route,
                    egress_verification=supplied,
                )
        events = store.list_audit_events(event="sandbox_resolved", limit=None)

    produced = {
        route: {
            event.payload["egress_verification"]
            for event in events
            if event.run_id == f"matrix-{route}"
        }
        for route in ("direct", "sidecar")
    }
    assert produced == {
        "direct": {frozen},
        "sidecar": {"unavailable", "sidecar-certified"},
    }
    _assert_recorded_event_basis(events)


def test_the_openshell_route_records_only_its_own_proven_value(tmp_path: Path) -> None:
    """The OpenShell route is bounded exactly as the other two are (#2319).

    A caller cannot supply ``openshell-certified`` on another route, and an
    OpenShell posture with no proof records ``unavailable`` under its own
    ``openshell-uncertified`` basis — there is no pending OpenShell posture
    that could later be mistaken for a proven one, and no borrowing of the
    direct route's permanent ``operator-managed-unverified`` either.
    """
    from kew import sandbox
    from kew.dispatch_core import _DispatchContext, _emit_sandbox_resolved
    from kew.run_store import SqliteRunStore

    ctx = MagicMock(spec=_DispatchContext)
    ctx.config = DispatchConfig()
    ctx.issue_num = 2319
    ctx.item_id = "2319"
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        ctx.store = store
        ctx.run_id = "openshell-proven"
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="openshell",
            image="kew-agent",
            egress_route="openshell",
            egress_verification=sandbox.OPENSHELL_ROUTE_EGRESS_VERIFICATION,
        )
        # The same proven value on the direct route stays frozen.
        ctx.run_id = "openshell-on-direct"
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="docker",
            image="kew-agent",
            egress_verification=sandbox.OPENSHELL_ROUTE_EGRESS_VERIFICATION,
        )
        # An unproven OpenShell posture says so, under its own basis.
        ctx.run_id = "openshell-unproven"
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="openshell",
            image="kew-agent",
            egress_route="openshell",
            egress_verification="unavailable",
        )
        recorded = {
            event.run_id: (
                event.payload["egress_verification"],
                event.payload["network_boundary"],
                event.payload.get("egress_verification_basis"),
            )
            for event in store.list_audit_events(event="sandbox_resolved", limit=None)
        }

    assert recorded == {
        "openshell-proven": (
            "openshell-certified",
            "openshell-certified",
            "openshell-selftest",
        ),
        "openshell-on-direct": (
            sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
            sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
            "operator-asserted",
        ),
        "openshell-unproven": (
            "unavailable",
            "unavailable",
            "openshell-uncertified",
        ),
    }


def _assert_recorded_event_basis(events):
    for event in events:
        assert event.payload["network_boundary"] == event.payload["egress_verification"]
        if event.run_id == "matrix-sidecar":
            assert event.payload.get("egress_verification_basis") == (
                "sidecar-admission"
                if event.payload["egress_verification"] == "sidecar-certified"
                else "pending-sidecar"
            )
        else:
            assert event.payload["egress_verification_basis"] == "operator-asserted"


def _flow_config(tmp_path: Path, sidecar: bool) -> DispatchConfig:
    from kew.config import InferenceTransportConfig

    config = DispatchConfig()
    config.project.repo = "acme/widgets"
    config.logging.dir = str(tmp_path / "logs")
    config.sandbox.enabled = True
    config.sandbox.runtime = "docker"
    config.retry.max_attempts = 1
    if sidecar:
        routes = tmp_path / "routes.toml"
        routes.write_text("[routes]\n")
        config.inference_transport = InferenceTransportConfig.model_validate(
            {
                "mode": "switchyard",
                "switchyard": {
                    "route": "switchyard/passthrough",
                    "config_path": str(routes),
                    "expected_version": "0.1.0",
                    "credential_env": ["ANTHROPIC_API_KEY"],
                    "sidecar": {
                        "enabled": True,
                        "image": "proxy:pinned",
                        "egress_network": "approved-egress",
                    },
                },
            }
        )
    return config


def _invoke_flow(flow, config, store, checkout, monkeypatch, outcomes=None):
    from kew import ci_fix, resolve, review
    from kew.graph import plan_run
    from kew.review_checkout import ReviewCheckout
    from kew.runners import get_runner
    from kew.runners.registry import ResolvedRoute
    from kew.worktree import ConflictInfo

    route = ResolvedRoute(get_runner("claude-code"), "claude-sonnet-4-6")
    metadata = {
        "number": 99,
        "headRefName": "agent/issue-1945",
        "baseRefName": "main",
        "headRefOid": "a" * 40,
        "title": "Fix egress evidence",
        "body": "Closes #1945",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "labels": [{"name": config.labels.agent_pr}],
    }
    for module in (review, resolve):
        monkeypatch.setattr(module, "_check_github_connectivity", lambda *a, **k: True)
        monkeypatch.setattr(module, "_get_pr_metadata", lambda *a, **k: metadata)
        monkeypatch.setattr(
            module, "publication_producer_for_head", lambda *a, **k: None
        )
    for module in (review, resolve, ci_fix):
        monkeypatch.setattr(module, "resolve_runner", lambda *a, **k: route)
    monkeypatch.setattr("kew.runners.resolve_runner", lambda *a, **k: route)
    conflict = ConflictInfo("merge", ["a.py"], "main")
    monkeypatch.setattr(
        resolve, "checkout_existing_worktree", lambda **k: (checkout, conflict)
    )
    # This scene supplies an admitted synthetic checkout, including its head.
    # Never depend on another module leaving a patched admission guard behind.
    monkeypatch.setattr(resolve, "require_pinned_pr_head", lambda *a: None)
    monkeypatch.setattr(ci_fix, "require_pinned_pr_head", lambda *a: None)
    monkeypatch.setattr(
        ci_fix, "checkout_existing_worktree", lambda **k: (checkout, None)
    )
    monkeypatch.setattr(ci_fix, "get_pr_for_run", lambda **k: metadata)
    monkeypatch.setattr(ci_fix, "extract_ci_log", lambda **k: "FAILED test_example")
    monkeypatch.setattr(ci_fix, "update_pr_labels", lambda *a, **k: True)
    monkeypatch.setattr(ci_fix, "_emit_ci_fix_event", lambda *a, **k: None)
    monkeypatch.setattr(
        review,
        "record_review_outcome",
        lambda outcome, **k: None if outcomes is None else outcomes.append(outcome),
    )
    monkeypatch.setattr(
        review,
        "fetch_review_comments",
        lambda *a, **k: [
            review.ReviewComment(
                1, "reviewer", "Fix this", "a.py", 1, "COMMENTED", None, False
            )
        ],
    )
    monkeypatch.setattr(
        review,
        "materialize_review_checkout",
        lambda *a, **k: ReviewCheckout(
            path=checkout,
            head_sha="a" * 40,
            branch="agent/issue-1945",
            detached=False,
            impl_worktree=None,
            conflict=None,
        ),
    )
    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: checkout)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: Path("plan.md"))
    if flow == "review":
        return review.dispatch_review(pr_number=99, config=config, store=store)
    if flow == "resolve":
        return resolve.dispatch_resolve(pr_number=99, config=config, store=store)
    if flow == "ci-fix":
        return ci_fix.dispatch_ci_fix(run_id="ci-1945", config=config, store=store)
    return plan_run._dispatch_plan_agent(
        name="egress",
        plan_path=checkout / "plan.md",
        config=config,
        store=store,
        item_id="plan:1945",
    )


@pytest.mark.parametrize("flow", ["review", "resolve", "ci-fix", "planning"])
@pytest.mark.parametrize("admitted", [True, False], ids=["admitted", "refused"])
def test_flow_attestation_follows_execution_route(
    tmp_path, monkeypatch, flow, admitted, *, disjoint=False
):
    from contextlib import nullcontext

    from kew import dispatch_core, sandbox
    from kew.inference_transport import resolve_inference_transport
    from kew.orchestrator import ContainedExecution
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner
    from kew.tracking import DispatchResult

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-credential")
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda *a: "/fake/docker")
    monkeypatch.setattr(
        "kew.remediation_containment.detect_runtime_cached", lambda *a: "/fake/docker"
    )
    monkeypatch.setattr(
        dispatch_core,
        "_require_contained_image_id",
        lambda *a, **k: "sha256:" + "f" * 64,
    )
    monkeypatch.setattr(dispatch_core, "_check_existing_pr", lambda *a: (False, None))
    monkeypatch.setattr(
        dispatch_core, "_admit_attempt_route_profile", lambda *a, **k: None
    )
    monkeypatch.setattr(
        dispatch_core, "_record_terminal_sidecar_evidence", lambda *a: None
    )
    supervisor = MagicMock(
        endpoint="http://127.0.0.1:9999",
        config_sha256="a" * 64,
        observed_version="0.1.0",
    )
    monkeypatch.setattr(
        dispatch_core,
        "sidecar_switchyard_context",
        lambda *a, **k: nullcontext(supervisor),
    )
    routes = []
    snapshots = []
    for sidecar in (True,) if disjoint else (False, True):
        root = tmp_path / str(sidecar)
        root.mkdir()
        config = _flow_config(root, sidecar)
        if disjoint:
            monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
            monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
            monkeypatch.setenv("OPENROUTER_API_KEY", "proxy-test-credential")
            config.inference_transport.switchyard.credential_env = (
                "OPENROUTER_API_KEY",
            )
        with SqliteRunStore(str(root / "runs.db")) as store:

            def prepare(*a, **k):
                routes.append("sidecar")
                snapshots.append(
                    store.list_audit_events(event="sandbox_resolved")[0].payload
                )
                if not admitted:
                    raise RuntimeError("synthetic sidecar admission refusal")
                return ContainedExecution.from_argv(
                    ["docker", "run", "--name", "sidecar-worker"]
                )

            def direct(*a, **k):
                routes.append("direct")
                return ["docker", "run", "--name", "direct-worker"]

            monkeypatch.setattr("kew.dispatch_sandbox.prepare_sidecar_worker", prepare)
            monkeypatch.setattr(
                "kew.dispatch_sandbox.build_agent_container_argv", direct
            )
            monkeypatch.setattr(
                dispatch_core,
                "run_agent",
                lambda **k: DispatchResult(
                    exit_code=1,
                    issue_num=k["issue_num"],
                    log_file=k["log_file_path"],
                    error_category="permanent",
                ),
            )
            _invoke_flow(flow, config, store, root, monkeypatch)
            events = store.list_audit_events(event="sandbox_resolved", limit=None)
            _assert_flow_events(events, sidecar, admitted, disjoint)
    assert routes == (["sidecar"] if disjoint else ["direct", "sidecar"])
    assert snapshots[0]["egress_verification"] == "unavailable"
    assert snapshots[0]["egress_verification_basis"] == "pending-sidecar"

    direct_config = _flow_config(tmp_path, False)
    with SqliteRunStore(str(tmp_path / "forged.db")) as store:
        sandbox.attest_flow_posture(
            store,
            flow=flow,
            runner="claude-code",
            config=direct_config.sandbox,
            posture="sandboxed",
            inference_transport=resolve_inference_transport(
                direct_config, get_runner("claude-code"), "claude-sonnet-4-6"
            ),
            certified=True,
        )
        [forged] = store.list_audit_events(event="sandbox_resolved")
    assert forged.payload["egress_verification"] == "operator-managed-unverified"
    assert forged.payload["egress_verification_basis"] == "operator-asserted"


def _expected_flow_egress(sidecar, admitted):
    if not sidecar:
        return "operator-managed-unverified", "operator-asserted"
    if admitted:
        return "sidecar-certified", "sidecar-admission"
    return "unavailable", "pending-sidecar"


def _assert_flow_events(events, sidecar, admitted, disjoint):
    assert events, "flow must attest its containment"
    newest = events[0].payload
    expected = _expected_flow_egress(sidecar, admitted)
    assert (
        newest["egress_verification"],
        newest.get("egress_verification_basis"),
    ) == expected
    assert newest["network_boundary"] == expected[0]
    if disjoint:
        assert newest["credential_names"] == ["ANTHROPIC_API_KEY"]
    if sidecar:
        assert all(
            event.payload["egress_verification"] != "operator-managed-unverified"
            for event in events
        )


@pytest.mark.parametrize("flow", ["review", "resolve", "ci-fix", "planning"])
def test_round8_flows_admit_disjoint_sidecar_credentials(tmp_path, monkeypatch, flow):
    test_flow_attestation_follows_execution_route(
        tmp_path, monkeypatch, flow, admitted=True, disjoint=True
    )


@pytest.mark.parametrize("flow", ["review", "resolve"])
@pytest.mark.usefixtures("real_runner_preflight")
def test_round9_sidecar_launches_without_host_runner(tmp_path, monkeypatch, flow):
    from kew import subprocess_utils
    from kew.runner_preflight import runner_preflight_failure

    config, store, stages = _tracked_sidecar(tmp_path, monkeypatch)
    config.retry.max_attempts = 1
    monkeypatch.setattr(subprocess_utils.shutil, "which", lambda *a, **k: None)
    assert "was not found" in runner_preflight_failure(
        config, runner_name="claude-code"
    )
    with store:
        _invoke_flow(flow, config, store, tmp_path, monkeypatch)
        assert stages == ["setup", "launch"]
        latest, pending, *_ = store.list_audit_events(event="sandbox_resolved")
        assert latest.payload["egress_verification_basis"] == "sidecar-admission"
        assert pending.payload["egress_verification_basis"] == "pending-sidecar"


@pytest.mark.parametrize("flow", ["review", "resolve"])
@pytest.mark.usefixtures("real_runner_preflight")
def test_round9_uncontained_refuses_without_host_runner(
    tmp_path, monkeypatch, capsys, flow
):
    from kew import resolve, subprocess_utils
    from kew.cli import _command_exit_code

    config, store, stages = _tracked_sidecar(tmp_path, monkeypatch)
    config.inference_transport = DispatchConfig().inference_transport
    config.sandbox.enabled = False
    monkeypatch.setattr(subprocess_utils.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(resolve, "cleanup_worktree", lambda *a: None)
    with store:
        outcome = _invoke_flow(flow, config, store, tmp_path, monkeypatch)
        assert outcome.kind == "not_run"
        assert outcome.exit_code == 1
        assert _command_exit_code(outcome) == 2
        assert stages == []
        assert "needs the 'claude' CLI, which was not found" in capsys.readouterr().err


@pytest.mark.parametrize("flow", ["review", "resolve", "ci-fix", "planning"])
def test_round8_direct_flows_refuse_missing_worker_credential(
    tmp_path, monkeypatch, flow
):
    from kew import dispatch_core
    from kew.ci_fix import CI_FIX_CANNOT_RUN
    from kew.cli import _command_exit_code
    from kew.review_outcome import ReviewOutcome
    from kew.run_store import SqliteRunStore
    from kew.sandbox import SandboxRuntimeUnavailable

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "proxy-test-credential")
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda *a: "/fake/docker")
    monkeypatch.setattr(
        "kew.remediation_containment.detect_runtime_cached", lambda *a: "/fake/docker"
    )

    def forbidden_launch(**kwargs):
        pytest.fail("missing direct credentials must refuse before agent launch")

    monkeypatch.setattr(dispatch_core, "run_agent", forbidden_launch)
    config = _flow_config(tmp_path, False)
    outcomes: list[ReviewOutcome] = []
    with SqliteRunStore(str(tmp_path / "direct.db")) as store:
        try:
            result = _invoke_flow(flow, config, store, tmp_path, monkeypatch, outcomes)
        except SandboxRuntimeUnavailable as exc:
            assert exc.reason == "credential_missing"
        else:
            if flow == "planning":
                assert result.could_not_run is True
                assert result.launched is False
                assert result.manifest_path is None
            elif flow == "ci-fix":
                assert result == CI_FIX_CANNOT_RUN
            else:
                # Review and resolve refuse containment before launch: raw and
                # public exit 2 (could not run).
                assert result.kind == "not_run"
                assert result.exit_code == 2
                assert _command_exit_code(result) == 2
            if flow == "review":
                # The returned outcome, the refused run row and the review
                # outcome carry the same raw exit code and reason.
                row = store.get(result.run_id or "")
                [outcome] = outcomes
                assert row is not None
                assert row.status == "refused"
                assert outcome.classification == "containment_refused"
                assert result.exit_code == row.exit_code == outcome.exit_code == 2
                assert result.reason == outcome.detail


def _regular_dispatch(tmp_path, monkeypatch, observe, *, sidecar=True):
    """Exercise regular dispatch with real admission and mocked runtime boundaries."""
    from contextlib import contextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    from kew import dispatch_core, dispatch_sandbox, sandbox
    from kew.inference_transport import resolve_inference_transport
    from kew.orchestrator import ContainedExecution
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner

    tmp_path.mkdir(exist_ok=True)
    config = _flow_config(tmp_path, sidecar)
    Path(config.logging.dir).mkdir()
    store = SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db"))
    transport = resolve_inference_transport(
        config, get_runner("claude-code"), "claude-sonnet-4-6"
    )
    ctx = SimpleNamespace(
        config=config,
        store=store,
        run_id="dispatch-1945",
        issue_num=1945,
        item_id="1945",
        provider="github",
        issue_title="Egress lifecycle",
        issue_body="Test regular dispatch",
        complexity="medium",
        merge_context=None,
        branch="agent/issue-1945",
        publication_base_sha="a" * 40,
        epic_ref=None,
        allow_unrecorded=True,
        expected_reservation_run_id=None,
        expected_reservation_generation=None,
        on_admitted=None,
    )
    decision = sandbox.SandboxDecision(
        sandboxed=True,
        posture="sandboxed",
        runtime="docker",
        image="worker:pinned",
        notice=None,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-credential")
    monkeypatch.setattr(dispatch_core, "_build_repo_map", lambda *a: "")
    monkeypatch.setattr(dispatch_core, "build_prompt", lambda **k: "test prompt")
    monkeypatch.setattr(
        dispatch_core, "_detect_sandbox_runtime", lambda *a: "/fake/docker"
    )
    monkeypatch.setattr(
        dispatch_sandbox,
        "_resolve_launch_image_id",
        lambda *a, **k: "sha256:" + "f" * 64,
    )
    for name in (
        "_capture_launch_evidence_or_finalize",
        "_require_initial_sandbox_publication_credentials",
        "append_sidecar_evidence",
        "finalize_container_hard_stop",
        "write_container_evidence",
    ):
        monkeypatch.setattr(dispatch_sandbox, name, lambda *a, **k: None)

    @contextmanager
    def setup(*a, **k):
        observe("setup", ctx)
        yield SimpleNamespace(
            endpoint="http://127.0.0.1:9999",
            config_sha256="a" * 64,
            topology_evidence=lambda **k: None,
        )

    def worker(*a, **k):
        observe("worker", ctx)
        # A prepared worker carries the full id its create recorded; cleanup
        # targets that id, never the name (#1963).
        return replace(
            ContainedExecution.from_argv(["docker", "run", "--name", "sidecar-worker"]),
            container_id="b" * 64,
            prepared=True,
        )

    def profile(*a, **k):
        # The real profile writer must complete before the ready callback.
        assert store.require_route_profile(ctx.run_id, 1) is not None
        observe("profile", ctx)

    def launch(*a, **k):
        observe("launch", ctx)
        return SimpleNamespace()

    def runtime_control(argv, *a, **k):
        # Round 28: a refusal after the worker was created kills and waits it;
        # the fake runtime confirms both so the refusal can finalize the run.
        import subprocess

        assert argv[1] in {"kill", "wait"}, argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(dispatch_sandbox, "_run", runtime_control)
    monkeypatch.setattr(dispatch_sandbox, "sidecar_switchyard_context", setup)
    monkeypatch.setattr(dispatch_sandbox, "prepare_sidecar_worker", worker)
    monkeypatch.setattr(
        dispatch_sandbox, "require_publication_launch_credentials", profile
    )
    monkeypatch.setattr(
        dispatch_sandbox,
        "build_agent_container_argv",
        lambda *a, **k: ["docker", "run", "--name", "direct-worker"],
    )
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", lambda *a, **k: 1)

    def invoke():
        dispatch_core._attest_direct_sandbox_resolution(
            ctx, "claude-code", decision, transport, tmp_path
        )
        return dispatch_core._launch_sandboxed(
            ctx,
            tmp_path,
            dispatch_core._EarlyRunInfo(
                runner="claude-code", model="claude-sonnet-4-6"
            ),
            None,
            image=decision.image,
            sandbox_decision=decision,
            inference_transport=transport,
        )

    return ctx, invoke


def _assert_dispatch_posture(ctx, value, basis):
    """Read SQLite through an independent connection, DTOs, and both exports."""
    import json
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from kew.report_export import (
        build_evidence_pack,
        render_evidence_pack_html,
        render_evidence_pack_json,
    )
    from kew.run_store import SqliteRunStore
    from kew.service.runs import get_run, get_run_posture

    with SqliteRunStore(str(Path(ctx.config.logging.dir) / "dispatch.db")) as reader:
        events = reader.list_audit_events(event="sandbox_resolved", run_id=ctx.run_id)
        assert events, "pending posture must be durable before sidecar setup"
        latest = events[0].payload
        assert (
            latest["egress_verification"],
            latest.get("egress_verification_basis"),
        ) == (
            value,
            basis,
        )
        assert latest["network_boundary"] == value
        for dto in (get_run(reader, ctx.run_id), get_run_posture(reader, ctx.run_id)):
            assert dto is not None
            assert (dto.network_boundary, dto.egress_verification_basis) == (
                value,
                basis,
            )
    now = datetime.now(UTC)
    with (
        patch("kew.report_export.sync_outcomes", return_value=0),
        patch(
            "kew.report_export._open_report_store",
            side_effect=lambda path: SqliteRunStore(str(path)),
        ),
    ):
        pack = build_evidence_pack(
            config=ctx.config,
            since=now - timedelta(days=1),
            now=now,
            repo_dir=Path(ctx.config.logging.dir).parent,
        )
    data = json.loads(render_evidence_pack_json(pack))
    posture = data["epics"][0]["items"][0]["postures"][0]
    assert (posture["network_boundary"], posture.get("egress_verification_basis")) == (
        value,
        basis,
    )
    html = render_evidence_pack_html(pack)
    assert f"network_boundary={value}" in html
    assert f"egress_verification_basis={basis}" in html


def test_regular_dispatch_attests_setup_and_admission(tmp_path, monkeypatch):
    """Direct stays frozen; sidecar promotion follows worker and profile admission."""
    for sidecar in (False, True):
        stages = []

        def observe(stage, ctx, *, sidecar=sidecar, stages=stages):
            stages.append(stage)
            value, basis = (
                (
                    ("sidecar-certified", "sidecar-admission")
                    if stage == "launch"
                    else ("unavailable", "pending-sidecar")
                )
                if sidecar
                else ("operator-managed-unverified", "operator-asserted")
            )
            _assert_dispatch_posture(ctx, value, basis)

        ctx, invoke = _regular_dispatch(
            tmp_path / str(sidecar), monkeypatch, observe, sidecar=sidecar
        )
        with ctx.store:
            result = invoke()
            assert result.exit_code == 1
            assert stages == (
                ["setup", "worker", "profile", "launch"] if sidecar else ["launch"]
            )
            events = ctx.store.list_audit_events(event="sandbox_resolved", limit=None)
            assert len(events) == (2 if sidecar else 1)
            _assert_dispatch_posture(
                ctx,
                "sidecar-certified" if sidecar else "operator-managed-unverified",
                "sidecar-admission" if sidecar else "operator-asserted",
            )


@pytest.mark.parametrize("refusal_stage", ["setup", "worker", "profile"])
def test_regular_dispatch_refusal_keeps_pending_basis(
    tmp_path, monkeypatch, refusal_stage
):
    def refuse(stage, ctx):
        if stage == refusal_stage:
            raise RuntimeError(f"synthetic {stage} refusal")
        assert stage != "launch", "refused dispatch must not launch"

    ctx, invoke = _regular_dispatch(tmp_path, monkeypatch, refuse)
    with ctx.store:
        with pytest.raises(RuntimeError, match=f"synthetic {refusal_stage} refusal"):
            invoke()
        _assert_dispatch_posture(ctx, "unavailable", "pending-sidecar")
        assert len(ctx.store.list_audit_events(event="sandbox_resolved")) == 1


def test_regular_dispatch_termination_during_setup_keeps_pending(tmp_path):
    """Abrupt process termination bypasses every Python finalizer."""
    import subprocess
    import sys
    from types import SimpleNamespace

    script = """
import os
import runpy
import sys
from pathlib import Path
import pytest

helpers = runpy.run_path(sys.argv[1])
def terminate(stage, ctx):
    if stage == "setup":
        os._exit(73)
with pytest.MonkeyPatch.context() as monkeypatch:
    ctx, invoke = helpers["_regular_dispatch"](
        Path(sys.argv[2]), monkeypatch, terminate
    )
    invoke()
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 73, child.stderr
    ctx = SimpleNamespace(config=_flow_config(tmp_path, True), run_id="dispatch-1945")
    _assert_dispatch_posture(ctx, "unavailable", "pending-sidecar")


@pytest.mark.parametrize("stage", ["pending-sidecar", "sidecar-admission"])
def test_regular_dispatch_requires_posture_acknowledgment(tmp_path, monkeypatch, stage):
    from kew.sandbox import SandboxRuntimeUnavailable

    stages = []
    ctx, invoke = _regular_dispatch(
        tmp_path, monkeypatch, lambda stage, ctx: stages.append(stage)
    )
    append = ctx.store.append_audit_event

    def drop(**kwargs):
        if (
            kwargs["event"] == "sandbox_resolved"
            and kwargs["payload"].get("egress_verification_basis") == stage
        ):
            raise OSError("synthetic posture append failure")
        return append(**kwargs)

    monkeypatch.setattr(ctx.store, "append_audit_event", drop)
    with ctx.store:
        with pytest.raises(SandboxRuntimeUnavailable, match="was not durably recorded"):
            invoke()
        assert "launch" not in stages
        assert ("setup" in stages) == (stage == "sidecar-admission")
        [refusal] = ctx.store.list_audit_events(event="sandbox_posture_refused")
        assert refusal.payload["attempt_number"] == 1
        assert "was not durably recorded" in refusal.payload["reason"]
        if stage == "sidecar-admission":
            assert ctx.store.get(ctx.run_id).status == "failure"
            _assert_dispatch_posture(ctx, "unavailable", "pending-sidecar")


def test_round12_resolve_sidecar_in_fresh_interpreter(tmp_path):
    """The synthetic checkout admits a sidecar without earlier resolve tests."""
    import subprocess
    import sys

    script = """
import runpy
import sys
from pathlib import Path
import pytest

assert "kew.resolve" not in sys.modules
helpers = runpy.run_path(sys.argv[1])
assert "kew.resolve" not in sys.modules
with pytest.MonkeyPatch.context() as monkeypatch:
    config, store, stages = helpers["_tracked_sidecar"](
        Path(sys.argv[2]), monkeypatch
    )
    config.retry.max_attempts = 1
    with store:
        helpers["_invoke_flow"](
            "resolve", config, store, Path(sys.argv[2]), monkeypatch
        )
        assert stages == ["setup", "launch"], stages
        latest, pending, *_ = store.list_audit_events(event="sandbox_resolved")
        assert pending.payload["egress_verification"] == "unavailable"
        assert latest.payload["egress_verification"] == "sidecar-certified"
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stdout + child.stderr


def _tracked_sidecar(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from kew import dispatch_core
    from kew.orchestrator import ContainedExecution
    from kew.run_store import SqliteRunStore
    from kew.tracking import DispatchResult

    config = _flow_config(tmp_path, True)
    config.retry.max_attempts = 2
    config.retry.backoff_base_seconds = 0
    Path(config.logging.dir).mkdir()
    store = SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db"))
    stages = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-credential")
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda *a: "/fake/docker")
    monkeypatch.setattr(
        "kew.remediation_containment.detect_runtime_cached", lambda *a: "/fake/docker"
    )
    monkeypatch.setattr(
        dispatch_core,
        "_require_contained_image_id",
        lambda *a, **k: "sha256:" + "f" * 64,
    )
    monkeypatch.setattr(dispatch_core, "_check_existing_pr", lambda *a: (False, None))
    monkeypatch.setattr(
        dispatch_core, "_admit_attempt_route_profile", lambda *a, **k: None
    )
    monkeypatch.setattr(
        dispatch_core, "_record_terminal_sidecar_evidence", lambda *a: None
    )

    def setup(*args, **kwargs):
        stages.append("setup")
        return nullcontext(
            SimpleNamespace(
                endpoint="http://127.0.0.1:9999",
                config_sha256="a" * 64,
                observed_version="0.1.0",
            )
        )

    def launch(**kwargs):
        stages.append("launch")
        return DispatchResult(
            exit_code=1,
            issue_num=kwargs["issue_num"],
            log_file=kwargs["log_file_path"],
            error_category="transient",
        )

    monkeypatch.setattr(dispatch_core, "sidecar_switchyard_context", setup)
    monkeypatch.setattr(dispatch_core, "run_agent", launch)
    monkeypatch.setattr(
        "kew.dispatch_sandbox.prepare_sidecar_worker",
        lambda *a, **k: ContainedExecution.from_argv(
            ["docker", "run", "--name", "sidecar-worker"]
        ),
    )
    return config, store, stages


@pytest.mark.parametrize("flow", ["review", "resolve", "ci-fix", "planning"])
@pytest.mark.parametrize("stage", ["pending-sidecar", "sidecar-admission"])
def test_flow_refuses_unacknowledged_posture(tmp_path, monkeypatch, flow, stage):
    from kew.sandbox import SandboxRuntimeUnavailable

    config, store, stages = _tracked_sidecar(tmp_path, monkeypatch)
    append = store.append_audit_event

    def drop(**kwargs):
        if (
            kwargs["event"] == "sandbox_resolved"
            and kwargs["payload"].get("egress_verification_basis") == stage
        ):
            raise OSError("synthetic posture append failure")
        return append(**kwargs)

    monkeypatch.setattr(store, "append_audit_event", drop)
    with store:
        try:
            _invoke_flow(flow, config, store, tmp_path, monkeypatch)
        except SandboxRuntimeUnavailable as exc:
            assert "was not durably recorded" in str(exc)
        assert "launch" not in stages
        assert ("setup" in stages) == (stage == "sidecar-admission")
        refusals = store.list_audit_events(event="sandbox_posture_refused")
        assert refusals and refusals[0].payload["attempt_number"] == 1
        assert all(
            e.payload["egress_verification"] != "sidecar-certified"
            for e in store.list_audit_events(event="sandbox_resolved")
        )


@pytest.mark.parametrize(
    "drop_pending", [False, True], ids=["admitted", "pending-write-failed"]
)
def test_retry_posture_is_bound_to_current_attempt(tmp_path, monkeypatch, drop_pending):
    from types import SimpleNamespace

    from kew import dispatch_core
    from kew.runners import get_runner

    config, store, stages = _tracked_sidecar(tmp_path, monkeypatch)
    append = store.append_audit_event
    pending_writes = 0

    def drop(**kwargs):
        nonlocal pending_writes
        if (
            kwargs["event"] == "sandbox_resolved"
            and kwargs["payload"].get("egress_verification_basis") == "pending-sidecar"
        ):
            pending_writes += 1
            if drop_pending and pending_writes == 2:
                # Assert the retry identity precedes its pending write.
                assert store.get("retry-1945").attempt_number == 2
                raise OSError("synthetic attempt two pending failure")
        return append(**kwargs)

    monkeypatch.setattr(store, "append_audit_event", drop)
    with store:
        result = dispatch_core.run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1945,
            item_id="1945",
            store=store,
            runner=get_runner("claude-code"),
            model="claude-sonnet-4-6",
            run_id="retry-1945",
            validate_continuation=False,
            contained_image="worker:pinned",
            sandbox_posture="sandboxed",
        )
        events = store.list_audit_events(event="sandbox_resolved", run_id=result.run_id)
        assert store.get(result.run_id).attempt_number == 2
        assert result.attempt_number == 2
        _assert_retry_stages(store, stages, events, drop_pending)
        _assert_reader_posture(
            SimpleNamespace(config=config, run_id=result.run_id),
            "unavailable" if drop_pending else "sidecar-certified",
            "pending-sidecar" if drop_pending else "sidecar-admission",
        )


def _assert_retry_stages(store, stages, events, drop_pending):
    if drop_pending:
        assert stages == ["setup", "launch"], (
            "attempt two must refuse before sidecar setup"
        )
        [refusal] = store.list_audit_events(event="sandbox_posture_refused")
        assert refusal.payload["attempt_number"] == 2
    else:
        assert stages == ["setup", "launch", "setup", "launch"]
        assert events[0].payload.get("attempt_number") == 2
        assert [event.payload["attempt_number"] for event in events] == [2, 2, 1, 1]


@pytest.mark.parametrize("route", ["direct", "native", "host", "host-default"])
@pytest.mark.parametrize("initial_recorded", [False, True])
def test_non_sidecar_retry_records_each_launch_posture(
    tmp_path, monkeypatch, route, initial_recorded
):
    from kew import dispatch_core, sandbox
    from kew.orchestrator import ContainedExecution
    from kew.runners import get_runner
    from kew.service.runs import get_run_posture
    from kew.tracking import DispatchResult

    config, store, _ = _tracked_sidecar(tmp_path, monkeypatch)
    config.inference_transport = DispatchConfig().inference_transport
    runner = get_runner("codex" if route == "native" else "claude-code")
    # Pin the attachment-network evidence so this retry assertion compares the
    # recorded posture, not whatever network the host running the suite has.
    monkeypatch.setattr(
        sandbox, "inspect_attachment_network", lambda *a, **k: ("bridge", False)
    )
    posture = {
        "direct": "sandboxed",
        "native": "native",
        "host": "uncontained",
        "host-default": "host",
    }[route]
    image = "worker:pinned" if route == "direct" else None
    launches = []

    def launch(**kwargs):
        events = store.list_audit_events(event="sandbox_resolved", run_id="retry-1945")
        launches.append(
            (
                [event.payload for event in events],
                store.get("retry-1945").attempt_number,
            )
        )
        return DispatchResult(
            exit_code=1,
            issue_num=kwargs["issue_num"],
            log_file=kwargs["log_file_path"],
            error_category="transient",
        )

    monkeypatch.setattr(dispatch_core, "run_agent", launch)
    monkeypatch.setattr(
        dispatch_core,
        "_contained_execution_for",
        lambda *a, **k: (
            ContainedExecution.from_argv(["docker", "run", "--name", "worker"])
            if image is not None
            else None
        ),
    )
    with store:
        if initial_recorded:
            sandbox.attest_flow_posture(
                store,
                flow="dispatch",
                runner=runner.name,
                config=config.sandbox,
                posture=posture,
                image=image,
                run_id="retry-1945",
                checkout_dir=tmp_path,
            )
        result = dispatch_core.run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1945,
            item_id="1945",
            store=store,
            runner=runner,
            model="gpt-5.4" if route == "native" else "claude-sonnet-4-6",
            run_id="retry-1945",
            validate_continuation=False,
            contained_image=image,
            sandbox_posture=None if route == "host-default" else posture,
        )
        events = store.list_audit_events(event="sandbox_resolved", run_id=result.run_id)
        _assert_retry_launches(events, launches)
        expected = sandbox.posture_record_fields(
            runner=runner.name,
            posture=posture,
            egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
            checkout_dir=tmp_path,
        )
        _assert_posture_event_fields(events, posture, expected)
        dto = get_run_posture(store, result.run_id)
        _assert_non_sidecar_posture(dto, route, posture, expected)


def _assert_posture_event_fields(events, posture, expected):
    for event in events:
        assert event.payload["posture"] == posture
        for key, value in expected.items():
            assert event.payload[key] == value


def _assert_retry_launches(events, launches):
    assert len(launches) == 2
    assert [event.payload["attempt_number"] for event in events] == [2, 1]
    assert len({event.payload["posture_id"] for event in events}) == 2
    assert [attempt for _, attempt in launches] == [1, 2]
    assert [
        [payload["attempt_number"] for payload in payloads] for payloads, _ in launches
    ] == [[1], [2, 1]]


def _assert_non_sidecar_posture(dto, route, posture, expected):
    assert dto.posture == posture
    assert dto.network_boundary == (
        "operator-managed-unverified" if route == "direct" else "not-applicable"
    )
    assert dto.egress_verification_basis == (
        "operator-asserted" if route == "direct" else None
    )
    assert (
        dto.filesystem_boundary
        == {
            "direct": "container-mount",
            "native": "runner-native-workspace-write",
            "host": "host",
            "host-default": "host",
        }[route]
    )
    assert dto.credential_names == expected["credential_names"]
    assert "ANTHROPIC_API_KEY" in dto.credential_names


@pytest.mark.parametrize("sidecar", [True, False], ids=["sidecar", "direct"])
def test_existing_pr_skip_preserves_last_attempt_posture(
    tmp_path, monkeypatch, sidecar
):
    from types import SimpleNamespace

    from kew import dispatch_core
    from kew.orchestrator import ContainedExecution
    from kew.runners import get_runner
    from kew.tracking import DispatchResult

    check_existing_pr = dispatch_core._check_existing_pr
    config, store, _ = _tracked_sidecar(tmp_path, monkeypatch)
    if not sidecar:
        config.inference_transport = DispatchConfig().inference_transport
    launches = []
    pr_lookups = []

    def launch(**kwargs):
        launches.append(store.get("retry-1945").attempt_number)
        return DispatchResult(
            exit_code=1,
            issue_num=kwargs["issue_num"],
            log_file=kwargs["log_file_path"],
            error_category="transient",
        )

    def existing_pr(branch, config):
        assert launches == [1], "the first launch created the PR before failing"
        pr_lookups.append(branch)
        return 1955

    monkeypatch.setattr(dispatch_core, "_check_existing_pr", check_existing_pr)
    monkeypatch.setattr(dispatch_core, "check_pr_exists", existing_pr)
    monkeypatch.setattr(dispatch_core, "update_pr_labels", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_core, "run_agent", launch)
    monkeypatch.setattr(
        dispatch_core,
        "_contained_execution_for",
        lambda *a, **k: ContainedExecution.from_argv(
            ["docker", "run", "--name", "worker"]
        ),
    )
    with store:
        result = dispatch_core.run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1945,
            item_id="1945",
            branch="agent/issue-1945",
            store=store,
            runner=get_runner("claude-code"),
            model="claude-sonnet-4-6",
            run_id="retry-1945",
            validate_continuation=False,
            contained_image="worker:pinned",
            sandbox_posture="sandboxed",
        )
        record = store.get(result.run_id)
        assert result.exit_code == 0
        assert record.status == "success"
        assert launches == [1]
        assert pr_lookups == ["agent/issue-1945"]
        _assert_single_attempt_completion(store, result, sidecar)
        _assert_reader_posture(
            SimpleNamespace(config=config, run_id=result.run_id),
            "sidecar-certified" if sidecar else "operator-managed-unverified",
            "sidecar-admission" if sidecar else "operator-asserted",
        )


def _assert_single_attempt_completion(store, result, sidecar):
    record = store.get(result.run_id)
    assert record.attempt_number == 1
    assert result.attempt_number == 1
    assert record.pr_url.endswith("/pull/1955")
    events = store.list_audit_events(event="sandbox_resolved", run_id=result.run_id)
    assert [event.payload["attempt_number"] for event in events] == (
        [1, 1] if sidecar else [1]
    )
    [completion] = store.list_audit_events(
        event="dispatch_completed", run_id=result.run_id
    )
    assert completion.payload["attempt_number"] == 1


def _assert_reader_posture(ctx, value, basis):
    import json
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from kew.report_export import (
        build_evidence_pack,
        render_evidence_pack_html,
        render_evidence_pack_json,
    )
    from kew.run_store import SqliteRunStore
    from kew.service.runs import get_run, get_run_posture, list_run_postures, list_runs

    with SqliteRunStore(str(Path(ctx.config.logging.dir) / "dispatch.db")) as reader:
        for dto in (
            get_run(reader, ctx.run_id),
            get_run_posture(reader, ctx.run_id),
            list_run_postures(reader, [ctx.run_id])[ctx.run_id],
            list_runs(reader)[0],
        ):
            assert dto is not None
            assert (dto.network_boundary, dto.egress_verification_basis) == (
                value,
                basis,
            )
    now = datetime.now(UTC)
    with (
        patch("kew.report_export.sync_outcomes", return_value=0),
        patch(
            "kew.report_export._open_report_store",
            side_effect=lambda path: SqliteRunStore(str(path)),
        ),
    ):
        pack = build_evidence_pack(
            config=ctx.config,
            since=now - timedelta(days=1),
            now=now,
            repo_dir=Path(ctx.config.logging.dir).parent,
        )
    posture = json.loads(render_evidence_pack_json(pack))["epics"][0]["items"][0][
        "postures"
    ][0]
    assert (posture["network_boundary"], posture["egress_verification_basis"]) == (
        value,
        basis,
    )
    html = render_evidence_pack_html(pack)
    assert f"network_boundary={value}" in html
    assert f"egress_verification_basis={basis}" in html


@pytest.mark.parametrize("current", [None, "pending-sidecar", "sidecar-admission"])
def test_readers_reject_newer_event_from_old_attempt(tmp_path, monkeypatch, current):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from kew.run_store import RunRecord

    config, store, _ = _tracked_sidecar(tmp_path, monkeypatch)
    with store:
        store.save(
            RunRecord(
                run_id="stale-1945",
                issue_num=1945,
                item_id="1945",
                status="pending",
                branch="agent/issue-1945",
                exit_code=-1,
                attempt_number=2,
                started_at=datetime.now(UTC),
            )
        )
        for attempt, basis in [(2, current), (1, "sidecar-admission")]:
            if basis is not None:
                value = (
                    "sidecar-certified"
                    if basis == "sidecar-admission"
                    else "unavailable"
                )
                store.append_audit_event(
                    timestamp=datetime.now(UTC).isoformat(),
                    event="sandbox_resolved",
                    run_id="stale-1945",
                    payload={
                        "attempt_number": attempt,
                        "posture": "sandboxed",
                        "network_boundary": value,
                        "egress_verification": value,
                        "egress_verification_basis": basis,
                        "reason": "old attempt" if attempt == 1 else "current attempt",
                    },
                )
        from kew.service.runs import get_run_posture

        posture = get_run_posture(store, "stale-1945")
        assert posture.reason != "old attempt"
        _assert_reader_posture(
            SimpleNamespace(config=config, run_id="stale-1945"),
            "sidecar-certified" if current == "sidecar-admission" else "unavailable",
            current or "pending-sidecar",
        )


@pytest.mark.parametrize("admitted", [False, True], ids=["pending", "promotion"])
def test_previous_transition_cannot_acknowledge_a_dropped_write(
    tmp_path, monkeypatch, admitted
):
    from kew import sandbox
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:

        def emit():
            sandbox.emit_resolved_posture(
                store,
                runner="claude-code",
                config=DispatchConfig().sandbox,
                posture="sandboxed",
                runtime="docker",
                image="worker:pinned",
                reason=None,
                run_id="repeat-1945",
                checkout_dir=tmp_path,
                repo=None,
                issue_num=1945,
                item_id="1945",
                egress_route="sidecar",
                certified=admitted,
            )

        emit()
        append = store.append_audit_event

        def drop(**kwargs):
            if kwargs["event"] == "sandbox_resolved":
                raise OSError("synthetic repeated transition failure")
            return append(**kwargs)

        monkeypatch.setattr(store, "append_audit_event", drop)
        with pytest.raises(
            sandbox.SandboxRuntimeUnavailable, match="was not durably recorded"
        ):
            emit()
        assert len(store.list_audit_events(event="sandbox_resolved")) == 1
        [refusal] = store.list_audit_events(event="sandbox_posture_refused")
        assert refusal.payload["attempt_number"] == 1


def _inspect_stub(outcome):
    """Stand in for the bounded `network inspect` on the admission path."""
    import subprocess as sp

    def run(cmd, **kwargs):
        if cmd[1:3] != ["network", "inspect"]:
            return sp.CompletedProcess(cmd, 0, "", "")
        if isinstance(outcome, Exception):
            raise outcome
        returncode, stdout = outcome
        return sp.CompletedProcess(cmd, returncode, stdout, "")

    return run


@pytest.mark.parametrize(
    ("outcome", "driver", "internal"),
    [
        ((0, '[{"Driver": "bridge", "Internal": false}]'), "bridge", False),
        ((0, '[{"Driver": "bridge", "Internal": true}]'), "bridge", True),
        ((0, '[{"Driver": "macvlan", "Internal": false}]'), "macvlan", False),
        ((1, ""), "unknown", "unknown"),
        ((0, "not json"), "unknown", "unknown"),
        ((0, "[]"), "unknown", "unknown"),
        ((0, '[{"Driver": "bridge"}]'), "unknown", "unknown"),
        (OSError("daemon unreachable"), "unknown", "unknown"),
        # Podman's native endpoint answers in the libpod schema, whose keys
        # are lowercase. Podman is a supported runtime, so its own spelling
        # must read as evidence and not as an unreadable daemon (#2313).
        ((0, '[{"driver": "bridge", "internal": false}]'), "bridge", False),
        ((0, '[{"driver": "bridge", "internal": true}]'), "bridge", True),
        ((0, '[{"driver": "macvlan", "internal": false}]'), "macvlan", False),
        ((0, '[{"driver": "bridge"}]'), "unknown", "unknown"),
        ((0, '[{"internal": false}]'), "unknown", "unknown"),
    ],
)
def test_posture_records_the_allowlist_network_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome, driver, internal
) -> None:
    """The direct route records the network shape kew can see, or 'unknown'."""
    from kew import sandbox, subprocess_utils

    monkeypatch.setattr(subprocess_utils, "run", _inspect_stub(outcome))
    fields = sandbox.posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
        checkout_dir=tmp_path,
        runtime="docker",
    )
    assert fields["network_driver"] == driver
    assert fields["network_internal"] == internal
    assert isinstance(fields["network_internal"], type(internal))
    # The label is unchanged by what the inspect found: an Internal=true
    # network has no egress at all, so refusing here would strand the agent.
    assert fields["network_boundary"] == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    assert fields["egress_verification_basis"] == "operator-asserted"


def test_posture_inspects_the_configured_runtime_once_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence collection uses the dispatch runtime under a hard timeout."""
    from kew import sandbox, subprocess_utils

    calls: list[tuple[list[str], object]] = []
    stub = _inspect_stub((0, '[{"Driver": "bridge", "Internal": false}]'))

    def record(cmd, **kwargs):
        calls.append((cmd, kwargs.get("timeout")))
        return stub(cmd, **kwargs)

    monkeypatch.setattr(subprocess_utils, "run", record)
    sandbox.posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
        checkout_dir=tmp_path,
        runtime="podman",
    )
    assert calls == [(["podman", "network", "inspect", "kew-allowlist"], 5)]


#: One whole `podman network inspect` answer: the native libpod endpoint
#: names every attribute in lowercase, so a parser written against Docker's
#: capitalized keys reads a readable network as an unreadable one (#2313).
_PODMAN_INSPECT_DOCUMENT = json.dumps(
    [
        {
            "name": "kew-allowlist",
            "id": "c1a2b3d4e5f60718293a4b5c6d7e8f90",
            "driver": "bridge",
            "network_interface": "podman1",
            "created": "2026-10-05T00:00:00.000000000Z",
            "ipv6_enabled": False,
            "internal": False,
            "dns_enabled": True,
            "ipam_options": {"driver": "host-local"},
        }
    ]
)


def test_posture_reads_a_whole_podman_inspect_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Podman's own output is evidence, not an unreadable-daemon 'unknown'."""
    from kew import sandbox, subprocess_utils

    monkeypatch.setattr(
        subprocess_utils, "run", _inspect_stub((0, _PODMAN_INSPECT_DOCUMENT))
    )
    fields = sandbox.posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
        checkout_dir=tmp_path,
        runtime="podman",
    )
    assert fields["network_driver"] == "bridge"
    assert fields["network_internal"] is False


@pytest.mark.parametrize(
    ("posture", "egress_verification"),
    [
        ("sandboxed", "sidecar-certified"),
        ("host", "unavailable"),
        ("refused", "unavailable"),
    ],
)
def test_other_routes_record_no_allowlist_network_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: str,
    egress_verification: str,
) -> None:
    """Only the direct route attaches kew-allowlist, so only it inspects it."""
    from kew import sandbox, subprocess_utils

    def refuse(cmd, **_kwargs):
        raise AssertionError(f"no inspect expected on this route: {cmd}")

    monkeypatch.setattr(subprocess_utils, "run", refuse)
    fields = sandbox.posture_record_fields(
        runner="claude-code",
        posture=posture,
        egress_verification=egress_verification,
        checkout_dir=tmp_path,
        runtime="docker",
    )
    assert "network_driver" not in fields
    assert "network_internal" not in fields


def test_admission_survives_an_unreadable_allowlist_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon that cannot answer leaves the record honest, not the run dead."""
    from kew import sandbox, subprocess_utils
    from kew.run_store import SqliteRunStore

    monkeypatch.setattr(
        subprocess_utils, "run", _inspect_stub(OSError("daemon unreachable"))
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        sandbox.emit_resolved_posture(
            store,
            runner="claude-code",
            config=DispatchConfig().sandbox,
            posture="sandboxed",
            runtime="docker",
            image="worker:pinned",
            reason=None,
            run_id="unreadable-2313",
            checkout_dir=tmp_path,
            repo=None,
            issue_num=2313,
            item_id="2313",
        )
        [resolved] = store.list_audit_events(event="sandbox_resolved")
    assert resolved.payload["network_driver"] == "unknown"
    assert resolved.payload["network_internal"] == "unknown"
    assert (
        resolved.payload["egress_verification"]
        == sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    )


def test_admission_records_the_bridge_it_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted posture carries the observed driver and Internal flag."""
    from kew import sandbox, subprocess_utils
    from kew.run_store import SqliteRunStore

    monkeypatch.setattr(
        subprocess_utils,
        "run",
        _inspect_stub((0, '[{"Driver": "bridge", "Internal": false}]')),
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        sandbox.emit_resolved_posture(
            store,
            runner="claude-code",
            config=DispatchConfig().sandbox,
            posture="sandboxed",
            runtime="docker",
            image="worker:pinned",
            reason=None,
            run_id="bridge-2313",
            checkout_dir=tmp_path,
            repo=None,
            issue_num=2313,
            item_id="2313",
        )
        [resolved] = store.list_audit_events(event="sandbox_resolved")
    assert resolved.payload["network_driver"] == "bridge"
    assert resolved.payload["network_internal"] is False


@pytest.mark.parametrize(
    ("network", "inspected"),
    [("allowlist", "kew-allowlist"), ("approved-egress", "approved-egress")],
)
def test_posture_inspects_the_network_the_dispatch_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: str, inspected: str
) -> None:
    """A custom attachment network is the one whose shape kew reports."""
    import subprocess as sp

    from kew import sandbox, subprocess_utils

    names: list[str] = []

    def record(cmd, **_kwargs):
        names.append(cmd[3])
        return sp.CompletedProcess(
            cmd, 0, '[{"Driver": "bridge", "Internal": true}]', ""
        )

    monkeypatch.setattr(subprocess_utils, "run", record)
    fields = sandbox.posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
        checkout_dir=tmp_path,
        runtime="docker",
        network=network,
    )
    assert names == [inspected]
    assert fields["network_internal"] is True


def test_open_network_posture_claims_no_attachment_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under network = open no kew-named network attaches, so kew reads none."""
    from kew import sandbox, subprocess_utils

    def refuse(cmd, **_kwargs):
        raise AssertionError(f"no network attaches under 'open': {cmd}")

    monkeypatch.setattr(subprocess_utils, "run", refuse)
    fields = sandbox.posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification=sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION,
        checkout_dir=tmp_path,
        runtime="docker",
        network="open",
    )
    assert fields["network_driver"] == "unknown"
    assert fields["network_internal"] == "unknown"
