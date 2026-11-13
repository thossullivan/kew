"""Placement wired into the contained dispatch launch (#2526).

Epic #2338 node 8. Nodes 5 and 6 shipped remote dispatch and placement with no
caller between them, so every agent ran on the operator machine whatever
``[sandbox] placement`` said. These tests drive the launch site itself:
``placement = local`` must construct no host provider at all, and
``placement = gcp`` must send the dispatch through
``place_dispatch_on_host`` — with the runner's declared credential NAME and
nothing else — record the host and instance on the run row before anything is
launched, and bring the work back onto this machine so the existing
publication path has a tree to publish.

Driven entirely through the fake ``gcloud`` on PATH; no test here touches a
real cloud.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from support.fake_gcloud import fake_gcloud_on_path, launchable
from support.fake_gcloud import recorded_calls as _calls
from support.fake_gcloud import write_state as _write_state
from support.image_resolution import instance_image_probe

from kew import subprocess_utils
from kew.config import (
    DispatchConfig,
    GcpHostConfig,
    HostsConfig,
    LoggingConfig,
    SandboxConfig,
)
from kew.dispatch_placement import (
    DispatchPlacementRefused,
    PlacedRunKindRefused,
    RemoteInputs,
    RemoteWorkRefused,
    hold_contained_placement,
    host_model_credentials,
    place_contained_dispatch,
    run_placed_dispatch,
)
from kew.fleet_placement import LOCAL_SITE, Placement, RunSite
from kew.hosts.remote_dispatch import DISPATCH_RUN_KIND
from kew.inference_transport import ResolvedInferenceTransport
from kew.run_store import (
    CI_FIX_RUN_KIND,
    RESOLVE_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.sandbox import (
    REFUSAL_PLACEMENT_UNAVAILABLE,
    _expected_packaged_image_pin,
    packaged_agent_image_context,
)
from kew.tracking import DispatchResult

#: The tag kew's packaged inputs pin: a host runs no other image (#2569).
IMAGE = _expected_packaged_image_pin(packaged_agent_image_context(), "claude-code")
SLUG = "issue-2526"
#: The immutable ID the INSTANCE resolves :data:`IMAGE` to. Deliberately not
#: this machine's ID for the same tag: a placed container is launched from the
#: tag against the instance's own image store, so an attestation that named
#: the operator's ID would be describing different bytes (#2526 review).
INSTANCE_IMAGE_ID = "sha256:" + "b" * 64
RUNNER = "claude-code"
CREDENTIAL = "CLAUDE_CODE_OAUTH_TOKEN"

#: Deliberately not shaped like a real model credential: a secret scanner
#: flagging a fixture is a gate that fails for a reason the test is not about.
TOKEN = "kew-fixture-model-credential-value"

REMOTE_CHECKOUT = "/home/kewrunner/kew-host/checkout"
SITE_ANSWER = f"1000 1000 {REMOTE_CHECKOUT}"

#: Two JSON event lines, exactly as a claude-code container writes them.
EVENT_LINES = (
    '{"type":"system","subtype":"init","session_id":"s-1"}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
)

#: What the agent on the instance wrote. Asserted BY CONTENT in the operator's
#: own checkout, because "the work came back" is a file on this machine.
REMOTE_WORK = b"the agent's work\n"


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with fake_gcloud_on_path(tmp_path, monkeypatch) as state:
        yield state


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Any]:
    ledger = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield ledger
    ledger.close()


def _config(tmp_path: Path, *, placement: str) -> DispatchConfig:
    return DispatchConfig(
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        hosts=HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="agent-1")
        ),
        sandbox=SandboxConfig(image=IMAGE, placement=placement),
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real git checkout with one commit, which is what a run ships."""
    from kew.sandbox import write_prompt_handoff

    root = tmp_path / "checkout"
    root.mkdir()
    subprocess_utils.ensure_tool("git")
    for args in (
        ["init", "--quiet", "--initial-branch", "work"],
        ["config", "user.email", "kew@example.invalid"],
        ["config", "user.name", "kew"],
    ):
        subprocess_utils.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess_utils.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess_utils.run(
        ["git", "commit", "--quiet", "-m", "base"], cwd=root, check=True
    )
    write_prompt_handoff(root, "Fix issue #2526.\n")
    return root


def _head(checkout: Path) -> str:
    return subprocess_utils.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, check=True
    ).stdout.strip()


def _changed_paths(checkout: Path) -> set[str]:
    """Every path git reports as changed in *checkout* — the publication's view."""
    porcelain = subprocess_utils.run(
        ["git", "status", "--porcelain"], cwd=checkout, check=True
    ).stdout
    return {line[3:].strip() for line in porcelain.splitlines() if line.strip()}


def _ssh_commands(state: Path) -> list[str]:
    """Every command the shim was asked to run over ssh, in order."""
    return [
        call[call.index("--command") + 1]
        for call in _calls(state)
        if "--command" in call
    ]


def _artifact_payload(base_sha: str, work: bytes = REMOTE_WORK) -> str:
    """A valid artifact directory, tarred and base64'd as the VM returns it."""
    digest = sha256(work).hexdigest()
    manifest = json.dumps(
        {
            "base": base_sha,
            "entries": [{"path": "notes.md", "mode": "100644", "sha256": digest}],
            "deletions": [],
            "scope": "workspace",
        }
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in (
            ("manifest.json", manifest),
            (f"blobs/{digest}", work),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(body))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _artifact_dir(
    root: Path,
    base_sha: str,
    files: dict[str, bytes],
    deletions: tuple[str, ...] = (),
) -> Path:
    """An artifact directory on disk naming *files*, as a fetch leaves it."""
    entries = []
    (root / "blobs").mkdir(parents=True)
    for path, body in files.items():
        digest = sha256(body).hexdigest()
        (root / "blobs" / digest).write_bytes(body)
        entries.append({"path": path, "mode": "100644", "sha256": digest})
    manifest = {
        "base": base_sha,
        "entries": entries,
        "deletions": list(deletions),
        "scope": "workspace",
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _responses(base_sha: str, **agent: Any) -> list[dict[str, Any]]:
    """How the instance answers each remote command, most specific first."""
    return [
        {"match": "kew-extract-artifact", "exit": 0},
        {"match": "tar -C", "output": _artifact_payload(base_sha), "exit": 0},
        instance_image_probe(IMAGE, INSTANCE_IMAGE_ID),
        {"match": "id -u", "output": SITE_ANSWER, "exit": 0},
        {"match": f"--name kew-{SLUG}", "output": EVENT_LINES, "exit": 0, **agent},
    ]


def _pending(run_id: str, issue_num: int) -> RunRecord:
    """One in-flight row, placed nowhere yet — as admission writes it."""
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        item_id=str(issue_num),
        run_kind=DISPATCH_RUN_KIND,
    )


def _inputs(checkout: Path, config: DispatchConfig, tmp_path: Path) -> RemoteInputs:
    return RemoteInputs(
        run_id="r-1",
        runner=RUNNER,
        slug=SLUG,
        model="claude-sonnet-4-5",
        image_tag=IMAGE,
        checkout_dir=checkout,
        base_sha=_head(checkout),
        log_file=tmp_path / "logs" / f"sandbox-{SLUG}.log",
        scratch=tmp_path / "logs" / "placed",
        run_kind=DISPATCH_RUN_KIND,
        transport=None,
    )


def _result() -> DispatchResult:
    return DispatchResult(exit_code=-1, issue_num=2526, log_file=Path("unused.log"))


class _OrderWatchingStore:
    """The ledger, plus WHEN the placement write landed relative to the wire.

    ``record_placement`` has to happen before the instance is touched: the
    guard a release asks reads the ledger, so a row written after the agent
    is running leaves a window where another process reads that instance as
    free. Counting the recorded ``gcloud`` calls at the moment of the write is
    how that ordering is asserted as an effect rather than as a code shape.
    """

    def __init__(self, inner: Any, state: Path) -> None:
        self._inner = inner
        self._state = state
        self.calls_when_placed: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def set_run_site(self, run_id: str, host: str, instance: str | None) -> bool:
        self.calls_when_placed.append(len(_calls(self._state)))
        return bool(self._inner.set_run_site(run_id, host, instance))


class TestTheDecision:
    """Where a dispatch goes, decided before anything is admitted."""

    def test_local_placement_asks_the_ledger_nothing(self, tmp_path: Path) -> None:
        """``local`` is the default and must acquire no new dependency.

        The store is one whose every method raises, so a single read on the
        local path fails this rather than passing quietly.
        """

        class _Unreadable:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"the local path read the ledger: {name}")

        placement = place_contained_dispatch(
            _config(tmp_path, placement="local"),
            _Unreadable(),
            runner=RUNNER,
            run_kind=DISPATCH_RUN_KIND,
        )

        assert placement.site == LOCAL_SITE

    def test_the_refusal_reaches_the_dispatchs_prerequisite_arm(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A busy cloud must not spend the node's retry budget.

        ``dispatch_core._refuse_or_fail`` routes an unlaunched unwind by TYPE:
        a ``SandboxRuntimeUnavailable`` is a prerequisite refusal (reservation
        released, refusal reported, no attempt consumed) and anything else is a
        run failure. The isinstance relationship IS that routing, so asserting
        it is asserting where this lands — and the reason token keeps the cause
        queryable without reading a message.
        """
        from kew.dispatch_core import SandboxRuntimeUnavailable

        monkeypatch.delenv(CREDENTIAL, raising=False)

        with pytest.raises(SandboxRuntimeUnavailable) as refusal:
            place_contained_dispatch(
                _config(tmp_path, placement="gcp"),
                store,
                runner=RUNNER,
                run_kind=DISPATCH_RUN_KIND,
            )

        assert isinstance(refusal.value, DispatchPlacementRefused)
        assert refusal.value.reason == REFUSAL_PLACEMENT_UNAVAILABLE

    def test_a_gcp_pin_with_no_room_refuses_before_admission(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        # The one instance already has an in-flight run on it.
        placed = _pending("r-0", 2525)
        store.save(placed)
        store.set_run_site("r-0", "gcp", "agent-1")

        with pytest.raises(DispatchPlacementRefused) as refusal:
            place_contained_dispatch(
                _config(tmp_path, placement="gcp"),
                store,
                runner=RUNNER,
                run_kind=DISPATCH_RUN_KIND,
            )

        assert "max_runs" in str(refusal.value)
        # Nothing was admitted for it: the refusal costs a queue wait.
        assert [run.run_id for run in store.list_pending()] == ["r-0"]

    def test_a_gcp_pin_sweeps_a_dead_row_before_refusing(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2627 — one crashed placed run must not wedge strict placement.

        Dead rows are otherwise swept only at admission, which a strict
        refusal never reaches. The death proof is stood in for at its own
        seam; what is asserted is that placement asks it, and that the row it
        proves dead is finalized and no longer fills the instance. The row
        the proof cannot settle keeps refusing, above.
        """
        import kew.run_store_liveness as liveness

        monkeypatch.setenv(CREDENTIAL, TOKEN)
        store.save(_pending("r-0", 2525))
        store.set_run_site("r-0", "gcp", "agent-1")
        monkeypatch.setattr(
            liveness,
            "_orphan_ready",
            lambda _store, record, _snap: record.run_id == "r-0",
        )

        placement = place_contained_dispatch(
            _config(tmp_path, placement="gcp"),
            store,
            runner=RUNNER,
            run_kind=DISPATCH_RUN_KIND,
        )

        assert placement.site == RunSite("gcp", "agent-1")
        assert store.list_pending() == []
        assert store.get("r-0").error_category == "orphaned_pending_reaped"

    def test_a_gcp_pin_refuses_when_this_machine_holds_no_credential(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CREDENTIAL, raising=False)

        with pytest.raises(DispatchPlacementRefused) as refusal:
            place_contained_dispatch(
                _config(tmp_path, placement="gcp"),
                store,
                runner=RUNNER,
                run_kind=DISPATCH_RUN_KIND,
            )

        assert "model credential" in str(refusal.value)

    def test_an_unobservable_base_refuses_the_placement(self, tmp_path: Path) -> None:
        """An instance extracts against a commit, so an unknown one is refused.

        The marker ``_trusted_base_observation`` returns for a checkout it
        cannot read is not a sha. Carried to an instance it would produce an
        artifact nothing could bind to a commit, and the refusal would arrive
        after the agent had run and been paid for — so it is taken here, where
        no row exists yet.
        """
        from kew.dispatch_sandbox import _placed_base_observation

        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        placed = Placement(RunSite("gcp", "agent-1"), "gcp", "agent-1 has room")

        with pytest.raises(DispatchPlacementRefused) as refusal:
            _placed_base_observation(not_a_repo, placed)

        assert "trusted base commit" in str(refusal.value)
        # A local placement never reads it at all, so it cannot be refused.
        assert (
            _placed_base_observation(
                not_a_repo, Placement(LOCAL_SITE, "local", "placement is local")
            )
            == ""
        )

    def test_prefer_gcp_falls_back_here_when_a_run_cannot_leave(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preference degrades; a pin refuses. Same cause, two answers."""
        monkeypatch.delenv(CREDENTIAL, raising=False)

        placement = place_contained_dispatch(
            _config(tmp_path, placement="prefer-gcp"),
            store,
            runner=RUNNER,
            run_kind=DISPATCH_RUN_KIND,
        )

        assert placement.site == LOCAL_SITE
        assert "model credential" in placement.reason

    def test_a_sidecar_transport_cannot_be_placed_on_an_instance(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        sidecar = ResolvedInferenceTransport(
            mode="switchyard",
            requested_model="claude-sonnet-4-5",
            model="claude-sonnet-4-5",
            config_sha256="0" * 64,
            sidecar=True,
        )

        with pytest.raises(DispatchPlacementRefused) as refusal:
            place_contained_dispatch(
                _config(tmp_path, placement="gcp"),
                store,
                runner=RUNNER,
                run_kind=DISPATCH_RUN_KIND,
                transport=sidecar,
            )

        assert "sidecar" in str(refusal.value)

    def test_only_the_runners_own_declared_credential_is_offered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        monkeypatch.setenv("GITHUB_TOKEN", "gh-fixture")
        monkeypatch.setenv("OPENAI_API_KEY", "other-runner-fixture")

        assert host_model_credentials(RUNNER) == {CREDENTIAL: TOKEN}


class TestALocalPlacementIsTodaysPath:
    def test_it_constructs_no_host_provider(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative claim, enumerated: no provider is built, at all.

        Both factories the module could reach are replaced with a raise, so a
        local launch that touched either fails here instead of discovering a
        cloud dependency on an operator's machine.
        """
        import kew.hosts as hosts

        for name in ("provider_on_instance", "provider_named", "resolve_provider"):
            monkeypatch.setattr(
                hosts,
                name,
                lambda *a, **k: pytest.fail("the local path built a host provider"),
            )
        launched: list[str] = []

        exit_code = run_placed_dispatch(
            _config(tmp_path, placement="local"),
            store,
            placement=Placement(LOCAL_SITE, "local", "placement is local"),
            inputs=RemoteInputs(
                run_id="r-1",
                runner=RUNNER,
                slug=SLUG,
                model="m",
                image_tag=IMAGE,
                checkout_dir=tmp_path / "unused",
                base_sha="",
                log_file=tmp_path / "unused.log",
                scratch=tmp_path / "unused-scratch",
                run_kind=DISPATCH_RUN_KIND,
            ),
            result=_result(),
            local_launch=lambda: (launched.append("here"), 0)[1],
        )

        assert (exit_code, launched) == (0, ["here"])
        # Nothing was written for a host that was never asked for.
        assert not (tmp_path / "unused-scratch").exists()


class TestARemotePlacementRunsOnItsInstance:
    """The whole wire, through the fake ``gcloud``: decision to publishable tree."""

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        # A tracker credential in the SAME environment, so "only the runner's
        # own declared model credential reaches a VM" is asserted against a
        # process that actually holds one to leak.
        monkeypatch.setenv("GITHUB_TOKEN", "kew-fixture-tracker-credential")

    def _place(
        self,
        config: DispatchConfig,
        store: Any,
        checkout: Path,
        tmp_path: Path,
        result: DispatchResult,
    ) -> int:
        placement = place_contained_dispatch(
            config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
        )
        assert placement.site == RunSite("gcp", "agent-1")
        return run_placed_dispatch(
            config,
            store,
            placement=placement,
            inputs=_inputs(checkout, config, tmp_path),
            result=result,
            local_launch=lambda: pytest.fail("a placed run launched on this machine"),
        )

    def test_the_agent_runs_on_the_instance_and_its_work_comes_back(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        result = _result()

        exit_code = self._place(config, store, checkout, tmp_path, result)

        assert exit_code == 0
        # The agent container launched on the instance, over ssh.
        commands = [
            call[call.index("--command") + 1]
            for call in _calls(fake_gcloud)
            if "--command" in call
        ]
        agent = [text for text in commands if f"--name kew-{SLUG}" in text]
        assert len(agent) == 1, commands
        # The credential travels by NAME on the command line and by value only
        # on the session's stdin, so no recorded argv may hold the value.
        assert f"-e {CREDENTIAL}" not in agent[0]
        assert f"export {CREDENTIAL}" in agent[0]
        assert not [call for call in _calls(fake_gcloud) if TOKEN in " ".join(call)]
        # And ONLY that name: the operator's GitHub token is in this process's
        # environment too, and no VM may ever be offered one.
        assert "GITHUB_TOKEN" not in agent[0]
        assert "GH_TOKEN" not in agent[0]
        # And the work is now on THIS machine, in the run's own checkout —
        # which is what leaves publication exactly where it already was.
        assert (checkout / "notes.md").read_bytes() == REMOTE_WORK

    def test_the_operator_side_still_has_a_tree_to_publish(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """The operator-side publication sees the remote agent's work.

        The change set git reports for the run's checkout is what every
        publication path on this machine works from — the legacy preflight
        reads it to classify a zero-change run and the canonical extractor
        walks the same tree. A placement whose work never came back leaves
        that set holding only this machine's own untracked prompt handoff,
        which publishes nothing while reporting the run a success, so this
        asserts the set rather than one file's existence.
        """
        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        assert "notes.md" not in _changed_paths(checkout)

        self._place(config, store, checkout, tmp_path, _result())

        assert "notes.md" in _changed_paths(checkout)

    def test_the_host_and_instance_are_recorded_before_anything_is_launched(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        watched = _OrderWatchingStore(store, fake_gcloud)

        self._place(config, watched, checkout, tmp_path, _result())

        record = store.get("r-1")
        assert (record.run_host, record.run_host_instance) == ("gcp", "agent-1")
        # Written before the first word reached the cloud, so no window exists
        # in which another process reads that instance as free.
        assert watched.calls_when_placed == [0]

    def test_the_relayed_stream_is_folded_by_the_local_consumer(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """One stream fold, so one set of evidence rules (#2526).

        Asserted as EFFECTS the fold alone produces — the sanitized log and the
        derived-evidence sidecar — not as a captured stream: the relay writes
        nothing to this process's stdout, so a stdout assertion would pass
        against a build that folded nothing.
        """
        from kew.transcript_metrics import read_derived_evidence

        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        inputs = _inputs(checkout, config, tmp_path)
        inputs.log_file.parent.mkdir(parents=True, exist_ok=True)
        result = _result()

        self._place(config, store, checkout, tmp_path, result)

        evidence = read_derived_evidence(inputs.log_file)
        assert evidence is not None
        assert evidence.lines_observed == 2
        # The daemon has no signal to give across a wire; a line the agent
        # wrote is the proof its container ran.
        assert result.launched is True

    def test_an_idle_stop_that_could_not_be_armed_is_in_the_runs_log(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2627 — the verdict stands, and the VM that may still be billing is
        written where the operator reads this run, not only to a logger."""
        config = _config(tmp_path, placement="gcp")
        _write_state(
            fake_gcloud, job_responses=_responses(_head(checkout)), release_exit=1
        )
        store.save(_pending("r-1", 2526))
        inputs = _inputs(checkout, config, tmp_path)
        inputs.log_file.parent.mkdir(parents=True, exist_ok=True)

        exit_code = self._place(config, store, checkout, tmp_path, _result())

        assert exit_code == 0
        log = inputs.log_file.read_text(encoding="utf-8")
        assert "could not arm the idle stop" in log
        assert "agent-1" in log

    def test_a_lost_session_is_a_named_failure_not_a_silent_success(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """A pre-emption mid-run: the lines arrive, the marker never does."""
        config = _config(tmp_path, placement="gcp")
        _write_state(
            fake_gcloud,
            job_responses=_responses(_head(checkout), swallow=True),
        )
        store.save(_pending("r-1", 2526))
        result = _result()

        exit_code = self._place(config, store, checkout, tmp_path, result)

        assert exit_code == 1
        assert result.error_category == "remote_host_interrupted"
        assert "gcp/agent-1" in result.stop_reason
        # Nothing was written into the checkout from a run with no verdict.
        assert not (checkout / "notes.md").exists()

    def test_work_carrying_a_held_credential_is_refused_before_the_checkout(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2569 item C: the placed artifact meets the publication scan first.

        The agent echoed the model credential it was handed into a file. The
        artifact is well-formed and names the right base, so only the scan
        stands between it and the operator's working tree.
        """
        config = _config(tmp_path, placement="gcp")
        leaked = _artifact_payload(_head(checkout), f"key = {TOKEN}\n".encode())
        answers = _responses(_head(checkout))
        answers[1] = {"match": "tar -C", "output": leaked, "exit": 0}
        _write_state(fake_gcloud, job_responses=answers)
        store.save(_pending("r-1", 2526))
        result = _result()

        exit_code = self._place(config, store, checkout, tmp_path, result)

        assert exit_code == 1
        assert result.error_category == RemoteWorkRefused.error_category
        assert "secret scan" in result.stop_reason
        assert TOKEN not in result.stop_reason
        assert not (checkout / "notes.md").exists()

    def test_placed_work_naming_an_lfs_filter_is_refused_with_no_fetch(
        self, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2636: placed work meets the publication filter before the checkout.

        The artifact names ``filter=lfs`` for its file; the operator's global
        git config has an LFS smudge driver, which is what would fetch bytes
        no scan read. A ``.gitattributes`` that introduces a driver is a
        refusal, so nothing is written and the driver never runs.
        """
        from kew.dispatch_placement import _materialize_remote_work

        fetched = tmp_path / "smudge-ran"
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            '[filter "lfs"]\n'
            f"\tsmudge = touch {fetched}; echo fetched-bytes\n"
            "\trequired = true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(home))
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:" + b"0" * 64 + b"\nsize 12\n"
        )
        artifact = _artifact_dir(
            tmp_path / "artifact",
            _head(checkout),
            {".gitattributes": b"*.bin filter=lfs\n", "data.bin": pointer},
        )

        with pytest.raises(RemoteWorkRefused, match="gitattributes_driver"):
            _materialize_remote_work(
                checkout,
                base_sha=_head(checkout),
                artifact_dir=artifact,
                index_file=tmp_path / "index",
            )

        assert not fetched.exists()
        assert not (checkout / ".gitattributes").exists()
        assert not (checkout / "data.bin").exists()

    def test_the_checkouts_own_filter_never_runs_on_placed_work(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        """#2636: a filter the checkout's ``.git/config`` defines, selected by
        the artifact's ``.gitattributes`` and marked required, is refused
        before anything is written — so it has nothing to run on."""
        from kew.dispatch_placement import _materialize_remote_work

        ran = tmp_path / "ran"

        def git(*args: str) -> None:
            subprocess_utils.run(["git", *args], cwd=checkout, check=True)

        git("config", "filter.evil.smudge", f"touch {ran}; echo unscanned")
        git("config", "filter.evil.clean", f"touch {ran}; cat")
        git("config", "filter.evil.required", "true")
        artifact = _artifact_dir(
            tmp_path / "artifact",
            _head(checkout),
            {".gitattributes": b"*.bin filter=evil\n", "data.bin": b"scanned\n"},
        )

        with pytest.raises(RemoteWorkRefused, match="gitattributes_driver"):
            _materialize_remote_work(
                checkout,
                base_sha=_head(checkout),
                artifact_dir=artifact,
                index_file=tmp_path / "index",
            )

        assert not ran.exists()
        assert not (checkout / ".gitattributes").exists()
        assert not (checkout / "data.bin").exists()

    def test_the_checkouts_own_fsmonitor_runs_nothing_while_writing_the_work(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        """#2569 review round 2: repository-local config is code too.

        Global and system config were already off; the operator checkout's
        own ``.git/config`` was not, so an fsmonitor defined there ran after
        the scan. It may not execute, and the file on disk is the blob the
        scan read. It runs through the shell, so an inline command (never a
        script file in a possibly noexec temp directory) proves whether git
        ran it.
        """
        from kew.dispatch_placement import _materialize_remote_work

        ran = tmp_path / "ran"
        subprocess_utils.run(
            ["git", "config", "core.fsmonitor", f"touch {ran}; true"],
            cwd=checkout,
            check=True,
        )
        artifact = _artifact_dir(
            tmp_path / "artifact", _head(checkout), {"data.bin": b"scanned\n"}
        )

        _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

        assert not ran.exists()
        assert (checkout / "data.bin").read_bytes() == b"scanned\n"
        # The publication's view of the checkout carries the written work.
        assert "data.bin" in _changed_paths(checkout)

    def test_work_against_another_base_is_refused_rather_than_applied(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """An artifact naming a base this run was not cut from is not a tree.

        Applying it would layer the agent's changes onto a tree they were never
        diffed against, publishing whatever moved in between as the agent's.
        """
        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses("b" * 40))
        store.save(_pending("r-1", 2526))
        result = _result()

        exit_code = self._place(config, store, checkout, tmp_path, result)

        assert exit_code == 1
        assert result.error_category == RemoteWorkRefused.error_category
        assert not (checkout / "notes.md").exists()


def test_placed_work_the_filter_refuses_is_never_written(
    tmp_path: Path, checkout: Path
) -> None:
    """#2636: the publication filter runs on placed work before the checkout.

    A ``.gitattributes`` that introduces a driver is refused for a local run's
    publication; work an instance returns meets the same refusal before a byte
    of it reaches the operator's working tree, and the configured filter has
    nothing to run on.
    """
    from kew.dispatch_placement import _materialize_remote_work

    ran = tmp_path / "filter-ran"
    for key, value in (
        ("filter.evil.smudge", f"touch {ran}; cat"),
        ("filter.evil.clean", f"touch {ran}; cat"),
        ("filter.evil.required", "true"),
    ):
        subprocess_utils.run(["git", "config", key, value], cwd=checkout, check=True)
    artifact = _artifact_dir(
        tmp_path / "artifact",
        _head(checkout),
        {".gitattributes": b"*.bin filter=evil\n", "data.bin": b"payload\n"},
    )

    with pytest.raises(RemoteWorkRefused, match="gitattributes_driver") as refused:
        _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

    assert ".gitattributes" in str(refused.value)
    assert not (checkout / ".gitattributes").exists()
    assert not (checkout / "data.bin").exists()
    assert not ran.exists()


def test_placed_work_a_person_must_review_is_written_for_the_publication_hold(
    tmp_path: Path, checkout: Path
) -> None:
    """A review flag is not a refusal: the work is written into the checkout,
    where the local publication's own filter holds it for a person."""
    from kew.dispatch_placement import _materialize_remote_work

    artifact = _artifact_dir(
        tmp_path / "artifact",
        _head(checkout),
        {".github/workflows/ci.yml": b"on: push\n", "Makefile": b"all:\n"},
    )

    _materialize_remote_work(
        checkout,
        base_sha=_head(checkout),
        artifact_dir=artifact,
        index_file=tmp_path / "index",
    )

    assert (checkout / ".github/workflows/ci.yml").read_bytes() == b"on: push\n"
    assert (checkout / "Makefile").read_bytes() == b"all:\n"


@pytest.mark.parametrize(
    "dropped",
    [
        "kew/.venv/lib/python3.13/site-packages/zz.pth",
        "node_modules/evil/package.json",
        ".mypy_cache/x/.vscode/tasks.json",
    ],
)
def test_placed_work_the_filter_drops_is_never_written(
    tmp_path: Path, checkout: Path, dropped: str
) -> None:
    """What reaches the disk is the filter's verdict, not the artifact.

    A file beneath an untracked dependency install or generated cache leaves
    the publication's scope, so writing it anyway would put bytes on the
    operator's machine that no publication carries and no person reviews —
    a ``.pth`` there runs on the next interpreter start. The flagged and
    ordinary files beside it are still written, as before.
    """
    from kew.dispatch_placement import _materialize_remote_work

    artifact = _artifact_dir(
        tmp_path / "artifact",
        _head(checkout),
        {dropped: b"import sys\n", "docs/notes.md": b"work\n", "Makefile": b"all:\n"},
    )

    _materialize_remote_work(
        checkout,
        base_sha=_head(checkout),
        artifact_dir=artifact,
        index_file=tmp_path / "index",
    )

    assert not (checkout / dropped).exists()
    assert (checkout / "docs/notes.md").read_bytes() == b"work\n"
    assert (checkout / "Makefile").read_bytes() == b"all:\n"


def test_a_driver_the_base_already_selects_never_runs_on_placed_work(
    tmp_path: Path, checkout: Path
) -> None:
    """The base's own ``.gitattributes`` names a filter the checkout defines.

    The artifact adds only a file that attribute selects, so the filter sees
    no new ``.gitattributes`` and refuses nothing. The bytes still reach the
    disk with no git checkout machinery, so the driver never runs and the
    file holds exactly the bytes the artifact carried.
    """
    from kew.dispatch_placement import _materialize_remote_work

    def git(*args: str) -> None:
        subprocess_utils.run(["git", *args], cwd=checkout, check=True)

    (checkout / ".gitattributes").write_text("*.bin filter=evil\n", encoding="utf-8")
    git("add", ".gitattributes")
    git("commit", "--quiet", "-m", "attributes")
    ran = tmp_path / "driver-ran"
    git("config", "filter.evil.smudge", f"touch {ran}; echo smudged")
    git("config", "filter.evil.clean", f"touch {ran}; echo cleaned")
    git("config", "filter.evil.required", "true")
    artifact = _artifact_dir(
        tmp_path / "artifact", _head(checkout), {"data.bin": b"payload\n"}
    )

    _materialize_remote_work(
        checkout,
        base_sha=_head(checkout),
        artifact_dir=artifact,
        index_file=tmp_path / "index",
    )

    assert not ran.exists()
    assert (checkout / "data.bin").read_bytes() == b"payload\n"


def _tree_files(checkout: Path, tree: str) -> dict[str, str]:
    """``path -> blob oid`` for every file *tree* records."""
    listing = subprocess_utils.run(
        ["git", "ls-tree", "-r", "-z", tree], cwd=checkout, check=True
    ).stdout
    files = {}
    for record in listing.split("\0"):
        if record:
            meta, _, path = record.partition("\t")
            files[path] = meta.split()[2]
    return files


def _written_oid(checkout: Path, path: str) -> str:
    """The blob id of *path*'s bytes on disk, with no attribute applied."""
    return subprocess_utils.run(
        ["git", "hash-object", "--no-filters", path], cwd=checkout, check=True
    ).stdout.strip()


@pytest.fixture
def exec_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory a hook script can actually be launched from.

    The pytest temporary filesystem may be ``noexec``, where a hook that
    "never ran" would prove nothing; the fallback is under ``HOME``.
    """
    if launchable(tmp_path):
        yield tmp_path
        return
    fallback = Path(tempfile.mkdtemp(prefix="kew-hooks-", dir=Path.home()))
    try:
        yield fallback
    finally:
        shutil.rmtree(fallback, ignore_errors=True)


class TestPlacedWorkIsWrittenAsScanned:
    """#2569 review round 3: the bytes on disk are the blob bytes the scan read.

    Git's checkout machinery applies the artifact's own ``.gitattributes`` and
    fires index hooks, so materialization does not use it at all.
    """

    @pytest.mark.parametrize(
        ("attributes", "files"),
        [
            (b"*.txt eol=crlf\n", {"notes.txt": b"payload\n"}),
            (b"*.id ident\n", {"stamp.id": b"$Id$\npayload\n"}),
            (
                b"*.enc working-tree-encoding=UTF-16\n",
                {"text.enc": b"payload\n"},
            ),
        ],
        ids=["eol-crlf", "ident", "working-tree-encoding"],
    )
    def test_no_attribute_changes_a_written_byte(
        self, tmp_path: Path, checkout: Path, attributes: bytes, files: dict[str, bytes]
    ) -> None:
        from kew.dispatch_placement import _materialize_remote_work

        artifact = _artifact_dir(
            tmp_path / "artifact",
            _head(checkout),
            {".gitattributes": attributes, **files},
        )

        tree = _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

        for path, body in files.items():
            assert (checkout / path).read_bytes() == body
        # Every file the scanned tree records is on disk as exactly its blob.
        recorded = _tree_files(checkout, tree)
        assert {path: _written_oid(checkout, path) for path in recorded} == recorded

    @pytest.mark.parametrize("where", ["hooks-path", "dot-git-hooks"])
    def test_no_index_hook_runs_anywhere_on_the_path(
        self, tmp_path: Path, checkout: Path, exec_dir: Path, where: str
    ) -> None:
        """``post-index-change`` fires on any index write, the apply's included."""
        from kew.dispatch_placement import _materialize_remote_work

        ran = tmp_path / "hook-ran"
        hooks = exec_dir / "hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch '{ran}'\n", encoding="utf-8")
        hook.chmod(0o755)
        if where == "hooks-path":
            subprocess_utils.run(
                ["git", "config", "core.hooksPath", str(hooks)],
                cwd=checkout,
                check=True,
            )
        else:
            shutil.rmtree(checkout / ".git" / "hooks", ignore_errors=True)
            (checkout / ".git" / "hooks").symlink_to(hooks)
        # The control: the hook is live, so its silence below means something.
        subprocess_utils.run(["git", "read-tree", "HEAD"], cwd=checkout, check=True)
        assert ran.exists()
        ran.unlink()
        artifact = _artifact_dir(
            tmp_path / "artifact", _head(checkout), {"data.bin": b"scanned\n"}
        )

        _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

        assert not ran.exists()
        assert (checkout / "data.bin").read_bytes() == b"scanned\n"

    def test_a_deletion_and_a_shape_change_land_as_the_tree_records(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        from kew.dispatch_placement import _materialize_remote_work

        (checkout / "pkg").mkdir()
        (checkout / "pkg" / "old.py").write_text("old\n", encoding="utf-8")
        subprocess_utils.run(["git", "add", "pkg"], cwd=checkout, check=True)
        subprocess_utils.run(
            ["git", "commit", "--quiet", "-m", "pkg"], cwd=checkout, check=True
        )
        artifact = _artifact_dir(
            tmp_path / "artifact",
            _head(checkout),
            {"pkg": b"now a file\n"},
            deletions=("pkg/old.py", "README.md"),
        )

        tree = _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

        assert (checkout / "pkg").read_bytes() == b"now a file\n"
        assert not (checkout / "README.md").exists()
        assert set(_tree_files(checkout, tree)) == {"pkg"}

    def test_a_symlinked_directory_in_the_checkout_is_refused_not_followed(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        from kew.dispatch_placement import _materialize_remote_work

        outside = tmp_path / "outside"
        outside.mkdir()
        (checkout / "sub").symlink_to(outside)
        artifact = _artifact_dir(
            tmp_path / "artifact", _head(checkout), {"sub/x.txt": b"escaped\n"}
        )

        with pytest.raises(RemoteWorkRefused, match="sub"):
            _materialize_remote_work(
                checkout,
                base_sha=_head(checkout),
                artifact_dir=artifact,
                index_file=tmp_path / "index",
            )

        assert list(outside.iterdir()) == []

    def test_a_name_a_folding_filesystem_merges_is_refused_before_any_write(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        """#2591: ``readme.md`` beside the base's ``README.md`` is one file on APFS."""
        from kew.publication_materialize import MaterializeRefused, write_tree_bytes

        base = _head(checkout)
        tree = _commit_tree(
            checkout, {"readme.md": b"artifact\n", "first.txt": b"x\n"}, base
        )

        with pytest.raises(MaterializeRefused, match="README.md"):
            write_tree_bytes(checkout, base_sha=base, tree=tree)

        # Refused as a set, before the first write rather than after it.
        assert not (checkout / "first.txt").exists()
        # By the listed name: on a folding filesystem (macOS) ``readme.md``
        # exists exactly when ``README.md`` does.
        assert "readme.md" not in {entry.name for entry in checkout.iterdir()}
        assert (checkout / "README.md").read_bytes() == b"base\n"

    def test_an_unchanged_path_the_base_attributes_converted_is_its_blob(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        """#2591: base ``*.txt eol=crlf`` leaves CRLF on disk for an untouched path.

        The publication reads the working tree raw, so those bytes would ship
        as a change the scan never saw.
        """
        from kew.dispatch_placement import _materialize_remote_work

        (checkout / ".gitattributes").write_bytes(b"*.txt eol=crlf\n")
        (checkout / "notes.txt").write_bytes(b"one\ntwo\n")
        subprocess_utils.run(["git", "add", "-A"], cwd=checkout, check=True)
        subprocess_utils.run(
            ["git", "commit", "--quiet", "-m", "attrs"], cwd=checkout, check=True
        )
        (checkout / "notes.txt").unlink()
        subprocess_utils.run(
            ["git", "checkout", "--", "notes.txt"], cwd=checkout, check=True
        )
        # The control: the base checkout really did convert the bytes.
        assert (checkout / "notes.txt").read_bytes() == b"one\r\ntwo\r\n"
        artifact = _artifact_dir(
            tmp_path / "artifact", _head(checkout), {"other.bin": b"scanned\n"}
        )

        tree = _materialize_remote_work(
            checkout,
            base_sha=_head(checkout),
            artifact_dir=artifact,
            index_file=tmp_path / "index",
        )

        assert (checkout / "notes.txt").read_bytes() == b"one\ntwo\n"
        recorded = _tree_files(checkout, tree)
        assert {path: _written_oid(checkout, path) for path in recorded} == recorded

    def test_memory_holds_less_than_one_blob(
        self, tmp_path: Path, checkout: Path
    ) -> None:
        """#2591: blobs stream to disk rather than buffering the whole batch."""
        import tracemalloc

        from kew.publication_materialize import write_tree_bytes

        size = 8 << 20
        files = {f"big{i}.bin": os.urandom(size) for i in range(3)}
        base = _head(checkout)
        tree = _commit_tree(checkout, files, base)
        del files

        tracemalloc.start()
        try:
            write_tree_bytes(checkout, base_sha=base, tree=tree)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        recorded = _tree_files(checkout, tree)
        assert {path: _written_oid(checkout, path) for path in recorded} == recorded
        assert peak < size


def _commit_tree(checkout: Path, files: dict[str, bytes], base: str) -> str:
    """A tree of *base* plus *files*, written to the object store only.

    Built with a scratch index so the checkout's files and index stay the
    base's, which is the state materialization starts from.
    """
    index = checkout / ".git" / "scratch-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess_utils.run(["git", "read-tree", base], cwd=checkout, check=True, env=env)
    for path, body in files.items():
        oid = (
            subprocess_utils.run_bytes(
                ["git", "hash-object", "-w", "--stdin"], cwd=checkout, stdin=body
            )
            .stdout.decode()
            .strip()
        )
        subprocess_utils.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}"],
            cwd=checkout,
            check=True,
            env=env,
        )
    tree = subprocess_utils.run(
        ["git", "write-tree"], cwd=checkout, check=True, env=env
    ).stdout.strip()
    index.unlink()
    return tree


class TestWhatAVmMayNeverRun:
    """The two launches no instance can run, whatever the fleet's capacity."""

    def test_the_openshell_runtime_is_refused_before_the_vm_is_started(
        self,
        tmp_path: Path,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_gcloud: Path,
    ) -> None:
        """A gateway sandbox has no container argv to place (#2526 review).

        ``build_run_argv`` — THE argv-build site — refuses the OpenShell
        runtime outright, and that refusal used to be reached on the far side
        of the wire: the instance was started and the checkout shipped, and
        then a ``SandboxRuntimeUnavailable`` escaped ``run_placed_dispatch``
        un-named, because it is not a ``HostError`` the placement can report.
        Asserted as both halves of that: the pin refuses here, and the cloud
        was never spoken to.
        """
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        config = _config(tmp_path, placement="gcp")
        openshell = config.model_copy(
            update={
                "sandbox": config.sandbox.model_copy(update={"runtime": "openshell"})
            }
        )

        with pytest.raises(DispatchPlacementRefused) as refusal:
            place_contained_dispatch(
                openshell, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
            )

        assert "openshell" in str(refusal.value)
        assert refusal.value.reason == REFUSAL_PLACEMENT_UNAVAILABLE
        # No row, no reservation, and not one word to the cloud.
        assert _calls(fake_gcloud) == []
        assert store.list_pending() == []

    def test_a_preference_runs_an_openshell_dispatch_here_instead(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preference degrades; a pin refuses. Same cause, two answers."""
        monkeypatch.setenv(CREDENTIAL, TOKEN)
        config = _config(tmp_path, placement="prefer-gcp")
        openshell = config.model_copy(
            update={
                "sandbox": config.sandbox.model_copy(update={"runtime": "openshell"})
            }
        )

        placement = place_contained_dispatch(
            openshell, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
        )

        assert placement.site == LOCAL_SITE
        assert "openshell" in placement.reason


#: The operator's own long-lived inference key, sitting in this process's
#: environment exactly as it sits in a real operator's. Nothing may carry it
#: to an instance: a contained launch on this machine does not pass it either.
AMBIENT_INFERENCE_KEY = "kew-fixture-operator-ambient-inference-key"

#: This run's bounded, per-run, spend-capped key — what a contained launch
#: substitutes for the ambient one, and so the only inference value a
#: placement may hand an instance.
BOUNDED_INFERENCE_KEY = "kew-fixture-bounded-per-run-inference-key"

#: A runner whose declared model credential IS the inference variable, which
#: is the only shape where the ambient-versus-bounded distinction exists.
OPENROUTER_RUNNER = "opencode"


def _bounded_key() -> Any:
    """This run's issued key, as ``bounded_run_credentials`` yields one."""
    from kew.credential_issuer import BoundedRunKey

    return BoundedRunKey(
        secret=BOUNDED_INFERENCE_KEY,
        key_hash="c" * 64,
        limit_usd=5.0,
        expires_at="2099-01-01T00:00:00+00:00",
    )


class TestTheCredentialAnInstanceReceives:
    """Exactly the values a contained launch HERE would pass, and no others.

    The local contained launch strips the operator's ambient
    ``OPENROUTER_API_KEY`` and substitutes this run's bounded key
    (``run_credentials.launch_environment`` with ``contained=True``). Reading
    ``os.environ`` instead handed the instance the ambient key — widening
    inference exposure, escaping the bounded key's spend cap, and doing it
    while the run row attested the issued key as what authenticated the
    attempt (#2526 review).
    """

    @pytest.fixture(autouse=True)
    def _ambient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", AMBIENT_INFERENCE_KEY)

    def test_the_operators_ambient_inference_key_is_never_offered(self) -> None:
        assert host_model_credentials(OPENROUTER_RUNNER) == {}

    def test_this_runs_bounded_key_is_what_travels(self) -> None:
        assert host_model_credentials(OPENROUTER_RUNNER, _bounded_key()) == {
            "OPENROUTER_API_KEY": BOUNDED_INFERENCE_KEY
        }

    def test_the_arguments_the_placement_is_made_with_carry_only_that_key(
        self, tmp_path: Path, store: Any, checkout: Path
    ) -> None:
        """Asserted on the wire, not only on the helper.

        ``_remote_arguments`` is the single place the mapping handed to
        ``place_dispatch_on_host`` is built, so this is the whole of what a VM
        is ever offered.
        """
        from kew.dispatch_placement import _remote_arguments

        config = _config(tmp_path, placement="gcp")
        inputs = RemoteInputs(
            run_id="r-1",
            runner=OPENROUTER_RUNNER,
            slug=SLUG,
            model="openrouter/some-model",
            image_tag=IMAGE,
            checkout_dir=checkout,
            base_sha=_head(checkout),
            log_file=tmp_path / "logs" / f"sandbox-{SLUG}.log",
            scratch=tmp_path / "logs" / "placed",
            run_kind=DISPATCH_RUN_KIND,
            credential=_bounded_key(),
        )

        arguments = _remote_arguments(
            config,
            store,
            inputs=inputs,
            on_line=lambda line: None,
            workspace=tmp_path / "workspace",
        )

        assert arguments["credentials"] == {"OPENROUTER_API_KEY": BOUNDED_INFERENCE_KEY}
        assert AMBIENT_INFERENCE_KEY not in arguments["credentials"].values()

    def test_a_per_run_key_route_is_still_placeable_without_an_ambient_one(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fence narrowed what travels, not which runs may be placed.

        A run whose inference credential is minted per attempt has nothing in
        the environment to hand over and still has a credential: refusing it
        for want of an ambient key would have made placement unreachable for
        every OpenRouter-backed runner.
        """
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        placement = place_contained_dispatch(
            _config(tmp_path, placement="gcp"),
            store,
            runner=OPENROUTER_RUNNER,
            run_kind=DISPATCH_RUN_KIND,
        )

        assert placement.site == RunSite("gcp", "agent-1")


class TestWhenAPlacementAttestsItsLaunch:
    """The claim marker and the containment boundary, each at its own moment."""

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)

    def _run(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        result: DispatchResult,
        **hooks: Any,
    ) -> int:
        config = _config(tmp_path, placement="gcp")
        return run_placed_dispatch(
            config,
            store,
            placement=Placement(RunSite("gcp", "agent-1"), "gcp", "agent-1 has room"),
            inputs=_inputs(checkout, config, tmp_path),
            result=result,
            local_launch=lambda: pytest.fail("a placed run launched on this machine"),
            **hooks,
        )

    def test_an_instance_that_was_never_reached_attests_no_launch(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """The marker is the claim's, so it may not fire ahead of the agent.

        ``on_launch`` transfers the admitted tracker claim to the runner and
        stops a refusal from rolling it back, and the local path delivers it
        only once a container handle exists. Fired before the instance was
        contacted, a placement that reached no agent at all — an unreachable
        instance here — was attested as launched (#2526 review).
        """
        _write_state(
            fake_gcloud,
            job_responses=_responses(_head(checkout)),
            describe_exit=1,
            describe_stderr="network is unreachable\n",
        )
        store.save(_pending("r-1", 2526))
        markers: list[str] = []
        result = _result()

        exit_code = self._run(
            tmp_path,
            store,
            checkout,
            result,
            on_launch=lambda: markers.append("launched"),
        )

        assert exit_code == 1
        assert result.error_category == "remote_host_interrupted"
        assert markers == []
        assert result.launched is None

    def test_the_first_line_the_agent_writes_delivers_the_marker_once(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """Two relayed lines, one marker: the delivery is idempotent."""
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        markers: list[str] = []
        result = _result()

        exit_code = self._run(
            tmp_path,
            store,
            checkout,
            result,
            on_launch=lambda: markers.append("launched"),
        )

        assert (exit_code, markers) == (0, ["launched"])
        assert result.launched is True

    def test_a_container_that_relayed_nothing_still_attests_its_launch(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """Silence is not "it never started": the daemon accepted this one.

        ``place_dispatch_on_host`` returns an outcome only for a session that
        reached its exit marker and was not refused by the daemon, so a
        container that wrote no event line still ran — and the claim marker is
        owed to it exactly as it is owed to a silent local container.
        """
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                _head(checkout), **{"match": f"--name kew-{SLUG}", "output": ""}
            ),
        )
        store.save(_pending("r-1", 2526))
        markers: list[str] = []
        result = _result()

        exit_code = self._run(
            tmp_path,
            store,
            checkout,
            result,
            on_launch=lambda: markers.append("launched"),
        )

        assert (exit_code, markers) == (0, ["launched"])
        # And still UNKNOWN rather than True: no line proves the agent spoke.
        assert result.launched is None

    def test_the_boundary_attested_is_the_instances_own_image_id(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """#1550 F1 on the far side of the wire (#2526 review).

        The containment profile has to name the bytes the agent runs. A placed
        container is launched from the mutable TAG against the instance's own
        image store, so this machine's immutable ID — resolved before the
        placement was even decided — describes a different image. What the
        placement hands its caller is the ID that instance resolved, read
        before its container starts and never the tag.
        """
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        attested: list[str] = []
        launched_when_attested: list[int] = []

        def attest(image_id: str) -> None:
            attested.append(image_id)
            launched_when_attested.append(
                len(
                    [
                        text
                        for text in _ssh_commands(fake_gcloud)
                        if f"--name kew-{SLUG}" in text
                    ]
                )
            )

        exit_code = self._run(
            tmp_path, store, checkout, _result(), attest_launch=attest
        )

        assert exit_code == 0
        assert attested == [INSTANCE_IMAGE_ID]
        # Not the tag, and not something this machine resolved for itself.
        assert IMAGE not in attested
        # Before the paid child, which is what "the boundary resolved
        # immediately before one launch" means.
        assert launched_when_attested == [0]

    def test_a_refused_attestation_stops_the_agent_from_running_at_all(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
    ) -> None:
        """An unrecordable boundary is not a launch that happens anyway."""
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))

        def refuse(image_id: str) -> None:
            raise RuntimeError("this attempt's boundary could not be recorded")

        with pytest.raises(RuntimeError, match="could not be recorded"):
            self._run(tmp_path, store, checkout, _result(), attest_launch=refuse)

        assert not [
            text for text in _ssh_commands(fake_gcloud) if f"--name kew-{SLUG}" in text
        ]


class TestTheLaunchIsOnTheAuditChain:
    """A placed run's credential crossing is attested, by name (#2526 review).

    ``record_remote_launch`` existed with no production caller, so a model
    credential reached a VM and no ``dispatch_remote_launch`` event said so —
    while the docs promised one, image ID included.
    """

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)

    def _run(self, tmp_path: Path, store: Any, checkout: Path, **hooks: Any) -> int:
        config = _config(tmp_path, placement="gcp")
        inputs = _inputs(checkout, config, tmp_path)
        return run_placed_dispatch(
            config,
            store,
            placement=Placement(RunSite("gcp", "agent-1"), "gcp", "agent-1 has room"),
            inputs=RemoteInputs(
                **{**inputs.__dict__, "issue_num": 2526, "item_id": "2526"}
            ),
            result=_result(),
            local_launch=lambda: pytest.fail("a placed run launched on this machine"),
            **hooks,
        )

    def _launch_events(self, store: Any) -> list[Any]:
        from kew.hosts.remote_dispatch import REMOTE_LAUNCH_EVENT

        return store.list_audit_events(event=REMOTE_LAUNCH_EVENT)

    def test_a_placed_dispatch_records_its_launch_before_the_agent_runs(
        self, tmp_path: Path, store: Any, checkout: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))
        launched_when_recorded: list[int] = []

        def attest(image_id: str) -> None:
            # The boundary is attested first; the event follows it before the
            # container session opens, so nothing has launched at either.
            launched_when_recorded.append(
                len(
                    [
                        text
                        for text in _ssh_commands(fake_gcloud)
                        if f"--name kew-{SLUG}" in text
                    ]
                )
            )

        assert self._run(tmp_path, store, checkout, attest_launch=attest) == 0

        events = self._launch_events(store)
        assert len(events) == 1
        event = events[0]
        assert (event.run_id, event.issue_num, event.item_id) == ("r-1", 2526, "2526")
        assert (event.actor_type, event.actor_id) == ("host", "agent-1")
        assert event.payload["instance"] == "agent-1"
        assert event.payload["credentials"] == [CREDENTIAL]
        assert event.payload["image"] == IMAGE
        assert event.payload["image_id"] == INSTANCE_IMAGE_ID
        # By name only: the value is never on the chain.
        assert TOKEN not in json.dumps(event.payload)
        assert launched_when_recorded == [0]

    def test_it_is_recorded_without_a_boundary_hook_too(
        self, tmp_path: Path, store: Any, checkout: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))

        assert self._run(tmp_path, store, checkout) == 0

        assert len(self._launch_events(store)) == 1

    def test_a_refused_boundary_records_no_launch(
        self, tmp_path: Path, store: Any, checkout: Path, fake_gcloud: Path
    ) -> None:
        """Nothing crossed, so the chain must not say a credential did."""
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))

        def refuse(image_id: str) -> None:
            raise RuntimeError("this attempt's boundary could not be recorded")

        with pytest.raises(RuntimeError, match="could not be recorded"):
            self._run(tmp_path, store, checkout, attest_launch=refuse)

        assert self._launch_events(store) == []

    def test_a_local_placement_records_no_remote_launch(
        self, tmp_path: Path, store: Any, checkout: Path
    ) -> None:
        config = _config(tmp_path, placement="local")

        exit_code = run_placed_dispatch(
            config,
            store,
            placement=Placement(LOCAL_SITE, "local", "placement is local"),
            inputs=_inputs(checkout, config, tmp_path),
            result=_result(),
            local_launch=lambda: 0,
        )

        assert exit_code == 0
        assert self._launch_events(store) == []


class TestAnInstanceAnotherJobHolds:
    """A host lock with no ledger row behind it still makes an instance busy.

    A sharded verify shard or a remediation holds an instance's lock and
    writes no dispatch row, so the ledger read it as free; the placement chose
    it and ``HostBusy`` landed after admission, in the failure arm (#2526
    review). It must be skipped before admission, as a full instance is.
    """

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)

    @staticmethod
    def _hold(config: DispatchConfig, instance: str) -> Any:
        from kew.dispatch_placement import provider_for
        from kew.hosts import host_lock, host_lock_dir

        provider = provider_for(config, RunSite("gcp", instance))
        return host_lock(host_lock_dir(), provider.lock_key)

    def test_a_gcp_pin_refuses_before_admission(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="gcp")

        with (
            self._hold(config, "agent-1"),
            pytest.raises(DispatchPlacementRefused) as refusal,
        ):
            place_contained_dispatch(
                config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
            )

        assert "gcp/agent-1" in str(refusal.value)
        assert store.list_pending() == []

    def test_a_preference_runs_it_here_instead(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="prefer-gcp")

        with self._hold(config, "agent-1"):
            placement = place_contained_dispatch(
                config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
            )

        assert placement.site == LOCAL_SITE
        assert "held by another kew job" in placement.reason

    def test_the_next_instance_in_the_pool_takes_it(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="gcp").model_copy(
            update={
                "hosts": HostsConfig(
                    gcp=GcpHostConfig(
                        project="proj",
                        zone="us-central1-a",
                        instances=["agent-1", "agent-2"],
                    )
                )
            }
        )

        with self._hold(config, "agent-1"):
            placement = place_contained_dispatch(
                config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
            )

        assert placement.site == RunSite("gcp", "agent-2")

    def test_a_released_lock_frees_the_instance_again(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="gcp")
        with self._hold(config, "agent-1"):
            pass

        placement = place_contained_dispatch(
            config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
        )

        assert placement.site == RunSite("gcp", "agent-1")

    def test_the_probe_takes_and_creates_nothing(self, tmp_path: Path) -> None:
        """Asking is not holding: no lock file appears, and a holder that
        arrives right after the probe still gets the host."""
        from kew.hosts import host_lock, host_lock_dir
        from kew.hosts.lock import host_lock_held

        directory = host_lock_dir()

        assert host_lock_held(directory, "gcp-proj-zone-agent-1") is False
        assert not directory.exists()
        with host_lock(directory, "gcp-proj-zone-agent-1"):
            assert host_lock_held(directory, "gcp-proj-zone-agent-1") is True
        assert host_lock_held(directory, "gcp-proj-zone-agent-1") is False
        with host_lock(directory, "gcp-proj-zone-agent-1"):
            pass


def _pool(config: DispatchConfig, *instances: str) -> DispatchConfig:
    return config.model_copy(
        update={
            "hosts": HostsConfig(
                gcp=GcpHostConfig(
                    project="proj", zone="us-central1-a", instances=list(instances)
                )
            )
        }
    )


class TestTwoDispatchesDecidingAtOnce:
    """A placement CLAIMS its instance before admission, not just picks it.

    The decision was a read — ledger rows and a lock probe — and the instance
    was only recorded and locked inside the remote launch, after admission.
    Two dispatch processes deciding together (the epic driver starts one per
    node) both picked the same free instance, and the loser met the host lock
    in the failure arm as ``remote_host_interrupted``: an attempt spent where
    ``prefer-gcp`` should have run it here and a pin should have waited
    (#2526 review). Each test holds the first dispatch's placement open while
    the second decides, which is exactly the window the race lived in.
    """

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CREDENTIAL, TOKEN)

    @staticmethod
    def _hold(config: DispatchConfig, store: Any) -> Any:
        return hold_contained_placement(
            config, store, runner=RUNNER, run_kind=DISPATCH_RUN_KIND
        )

    def test_the_second_preference_runs_here(self, tmp_path: Path, store: Any) -> None:
        config = _config(tmp_path, placement="prefer-gcp")

        with self._hold(config, store) as first, self._hold(config, store) as second:
            assert first.placement.site == RunSite("gcp", "agent-1")
            assert second.placement.site == LOCAL_SITE
            assert second.provider is None

    def test_the_second_pin_is_refused_before_admission(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="gcp")

        with (
            self._hold(config, store),
            pytest.raises(DispatchPlacementRefused),
            self._hold(config, store),
        ):
            pass

        assert store.list_pending() == []

    def test_the_second_takes_the_next_instance(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _pool(_config(tmp_path, placement="gcp"), "agent-1", "agent-2")

        with self._hold(config, store) as first, self._hold(config, store) as second:
            assert first.placement.site == RunSite("gcp", "agent-1")
            assert second.placement.site == RunSite("gcp", "agent-2")

    def test_a_claim_lost_after_the_probe_decides_again(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe is a snapshot; the claim is the test-and-set.

        The probe is made to answer "free" for every instance, as it would for
        a holder that arrived just after it, so only the claim stands between
        two dispatches and the same machine.
        """
        from kew.hosts.provider import HostProvider

        monkeypatch.setattr(HostProvider, "held_by_a_job", lambda self: False)
        config = _config(tmp_path, placement="prefer-gcp")

        with self._hold(config, store), self._hold(config, store) as second:
            assert second.placement.site == LOCAL_SITE
            assert "taken by another kew job" in second.placement.reason

        pinned = _config(tmp_path, placement="gcp")
        with (
            self._hold(pinned, store),
            pytest.raises(DispatchPlacementRefused),
            self._hold(pinned, store),
        ):
            pass

    def test_the_instance_is_free_again_once_the_dispatch_ends(
        self, tmp_path: Path, store: Any
    ) -> None:
        config = _config(tmp_path, placement="gcp")
        with self._hold(config, store):
            pass

        with self._hold(config, store) as again:
            assert again.placement.site == RunSite("gcp", "agent-1")

    def test_a_local_placement_claims_nothing(
        self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kew.hosts as hosts

        monkeypatch.setattr(
            hosts,
            "provider_on_instance",
            lambda *a, **k: pytest.fail("the local path built a host provider"),
        )

        with self._hold(_config(tmp_path, placement="local"), store) as held:
            assert (held.placement.site, held.provider) == (LOCAL_SITE, None)

    def test_the_launch_runs_inside_its_own_claim(
        self, tmp_path: Path, store: Any, checkout: Path, fake_gcloud: Path
    ) -> None:
        """The held instance is launched on through the claim that holds it.

        A second provider for the same instance would contend with this run's
        own hold and fail the placement it had just won.
        """
        config = _config(tmp_path, placement="gcp")
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(_pending("r-1", 2526))

        with self._hold(config, store) as held:
            exit_code = run_placed_dispatch(
                config,
                store,
                placement=held.placement,
                provider=held.provider,
                inputs=_inputs(checkout, config, tmp_path),
                result=(result := _result()),
                local_launch=lambda: pytest.fail("a placed run launched here"),
            )

        assert (exit_code, result.error_category) == (0, "")
        assert (checkout / "notes.md").read_bytes() == REMOTE_WORK


#: The run kinds that launch through ``run_tracked_agent``'s contained
#: execution rather than the dispatch launcher — never placed on an instance.
NON_DISPATCH_KINDS = (REVIEW_REMEDIATION_RUN_KIND, RESOLVE_RUN_KIND, CI_FIX_RUN_KIND)


class TestOnlyADispatchIsEverPlaced:
    """The run-kind fence, held where the kind is decided (#2526 review).

    Only a ``dispatch`` run may carry a model credential to an instance. A
    remediation, a resolve and a CI fix launch through ``run_tracked_agent``,
    whose container is built on this machine: a ``gcp`` pin governing them
    must refuse before anything is admitted, ``prefer-gcp`` must run them
    here, and a placement whose admitted row is not a dispatch must never
    reach the credential sink.
    """

    @pytest.fixture(autouse=True)
    def _credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Set, so the kind is the ONLY reason a remote placement is refused.
        monkeypatch.setenv(CREDENTIAL, TOKEN)

    @pytest.fixture(autouse=True)
    def _launch_image_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "kew.dispatch_core._require_contained_image_id",
            lambda run, **_: "sha256:" + "f" * 64,
        )

    @staticmethod
    def _runner() -> Any:
        from unittest.mock import MagicMock

        runner = MagicMock()
        runner.name = RUNNER
        runner.required_credentials = (CREDENTIAL,)
        runner.container_args.return_value = ["--model", "claude-opus-5"]
        return runner

    @pytest.mark.parametrize("run_kind", NON_DISPATCH_KINDS)
    def test_a_gcp_pin_refuses_the_kind_before_admission(
        self,
        tmp_path: Path,
        store: Any,
        monkeypatch: pytest.MonkeyPatch,
        run_kind: str,
    ) -> None:
        from kew.dispatch_core import run_tracked_agent

        monkeypatch.setattr(
            "kew.dispatch_core.run_agent",
            lambda *a, **k: pytest.fail("a pinned run launched on this machine"),
        )

        with pytest.raises(DispatchPlacementRefused) as refusal:
            run_tracked_agent(
                prompt="p",
                worktree_dir=tmp_path,
                config=_config(tmp_path, placement="gcp"),
                issue_num=42,
                store=store,
                runner=self._runner(),
                model="claude-opus-5",
                run_kind=run_kind,
                run_id="r-pinned",
                contained_image=IMAGE,
            )

        assert run_kind in str(refusal.value)
        assert refusal.value.reason == REFUSAL_PLACEMENT_UNAVAILABLE
        # Before admission: no row, so nothing to release or finalize.
        assert store.get("r-pinned") is None
        assert store.list_pending() == []

    @pytest.mark.parametrize("run_kind", NON_DISPATCH_KINDS)
    def test_prefer_gcp_runs_the_kind_on_this_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_kind: str
    ) -> None:
        from unittest.mock import patch

        from kew.dispatch_core import run_tracked_agent

        monkeypatch.setattr(
            "kew.dispatch_placement.provider_for",
            lambda *a, **k: pytest.fail("a local launch built a host provider"),
        )
        with (
            patch("kew.dispatch_core.check_pr_exists", return_value=None),
            patch("kew.dispatch_core.run_agent") as run_agent,
        ):
            run_agent.return_value = _result()
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=_config(tmp_path, placement="prefer-gcp"),
                issue_num=42,
                runner=self._runner(),
                model="claude-opus-5",
                run_kind=run_kind,
                contained_image=IMAGE,
            )

        assert run_agent.call_args.kwargs["contained"] is not None

    @pytest.mark.parametrize("run_kind", NON_DISPATCH_KINDS)
    def test_the_placement_itself_never_puts_the_kind_on_an_instance(
        self, tmp_path: Path, store: Any, run_kind: str
    ) -> None:
        with pytest.raises(DispatchPlacementRefused, match=run_kind):
            place_contained_dispatch(
                _config(tmp_path, placement="gcp"),
                store,
                runner=RUNNER,
                run_kind=run_kind,
            )
        preferred = place_contained_dispatch(
            _config(tmp_path, placement="prefer-gcp"),
            store,
            runner=RUNNER,
            run_kind=run_kind,
        )

        assert preferred.site == LOCAL_SITE
        assert run_kind in preferred.reason

    def test_a_dispatch_with_the_same_config_is_still_placed(
        self, tmp_path: Path, store: Any
    ) -> None:
        """The control: the kind, and nothing else, is what kept those local."""
        placement = place_contained_dispatch(
            _config(tmp_path, placement="gcp"),
            store,
            runner=RUNNER,
            run_kind=DISPATCH_RUN_KIND,
        )

        assert placement.site == RunSite("gcp", "agent-1")

    @pytest.mark.parametrize(
        ("row_kind", "launched_as"),
        [
            (REVIEW_REMEDIATION_RUN_KIND, DISPATCH_RUN_KIND),
            (DISPATCH_RUN_KIND, CI_FIX_RUN_KIND),
        ],
    )
    def test_a_placement_whose_row_is_not_a_dispatch_never_reaches_the_credential(
        self,
        tmp_path: Path,
        store: Any,
        checkout: Path,
        fake_gcloud: Path,
        monkeypatch: pytest.MonkeyPatch,
        row_kind: str,
        launched_as: str,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses(_head(checkout)))
        store.save(replace(_pending("r-1", 2526), run_kind=row_kind))
        monkeypatch.setattr(
            "kew.dispatch_placement.host_model_credentials",
            lambda *a, **k: pytest.fail("a credential was derived for a non-dispatch"),
        )
        config = _config(tmp_path, placement="gcp")
        result = _result()

        exit_code = run_placed_dispatch(
            config,
            store,
            placement=Placement(RunSite("gcp", "agent-1"), "gcp", "agent-1 has room"),
            inputs=replace(_inputs(checkout, config, tmp_path), run_kind=launched_as),
            result=result,
            local_launch=lambda: pytest.fail("a placed run launched on this machine"),
        )

        assert exit_code == 1
        assert result.error_category == PlacedRunKindRefused.error_category
        assert row_kind in result.stop_reason
        assert _calls(fake_gcloud) == []
        assert not (checkout / "notes.md").exists()
