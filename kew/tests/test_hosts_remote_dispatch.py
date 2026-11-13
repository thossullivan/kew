"""Remote dispatch: one agent container on a gcp host, the work back as data.

Epic #2338 node 5. Driven entirely through the fake ``gcloud`` on PATH — no
test here touches a real cloud. The shim answers each remote command from a
JSON state file and records every argv it is handed, so the same run asserts
what kew *said* to the instance and what the instance said back.
"""

from __future__ import annotations

import base64
import io
import json
import shlex
import signal
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any

import pytest
from support.fake_gcloud import fake_gcloud_on_path
from support.fake_gcloud import read_state as _read_state
from support.fake_gcloud import recorded_calls as _calls
from support.fake_gcloud import write_state as _write_state
from support.image_resolution import (
    AGENT_IMAGE_INPUTS_LABEL,
    docker_image_probe,
    instance_image_probe,
)
from support.takeover_ledger import dead_holder

from kew import subprocess_utils
from kew.audit_chain import verify_chain
from kew.config import (
    DispatchConfig,
    GcpHostConfig,
    HostsConfig,
    LoggingConfig,
    SandboxConfig,
)
from kew.fleet_placement import PlacementNotRecorded, instance_is_occupied
from kew.host_takeovers import (
    LEASE_LAPSED_EVENT,
    LEASE_TAKEOVER_PENDING_EVENT,
    SETTLING_EVENTS,
)
from kew.hosts import remote_dispatch
from kew.hosts.errors import CredentialVariableRefused, HostError, HostTimeout
from kew.hosts.gcp import (
    DEADLINE_STOP_UNIT,
    IDLE_STOP_UNIT,
    LEASE_FILE,
    GcpProvider,
    clone_command,
    marker_command,
    parse_exit_marker,
)
from kew.hosts.remote_dispatch import (
    DISPATCH_RUN_KIND,
    HANDOFF_FAILURE_MARKER,
    REMOTE_LAUNCH_EVENT,
    ModelCredential,
    RemoteDispatchError,
    RemoteLaunch,
    RemoteRunInterrupted,
    RemoteSite,
    bundle_checkout,
    container_network,
    credential_handoff,
    credential_prelude,
    launched_container,
    permit_model_credentials,
    place_dispatch_on_host,
    placed_environment_argv,
    prompt_handoff,
    prompt_prelude,
    record_remote_launch,
    remote_agent_command,
    remote_agent_handoff,
    remote_deadline,
    sweep_remote_container,
    withheld_names,
)
from kew.publication_artifact import read_artifact
from kew.publication_filter import filter_manifest
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import (
    ALLOWLIST_NETWORK,
    PROMPT_FILENAME,
    _expected_packaged_image_pin,
    packaged_agent_image_context,
    write_prompt_handoff,
)

#: The instance's own answers. Resolved absolute path and login ids, because
#: a container's mounts and ``--user`` are the instance's, never this machine's.
REMOTE_CHECKOUT = "/home/kewrunner/kew-host/checkout"
REMOTE_ARTIFACT = "/home/kewrunner/kew-host/artifact"
REMOTE_WORKSPACE_CHECKOUT = "kew-host/checkout"
SITE_ANSWER = f"1000 1000 {REMOTE_CHECKOUT}"

BASE_SHA = "a" * 40

#: What only the idle stop's arming says: every launch also arms a
#: ``systemd-run`` timer of its own, the container's deadline (#2550).
_ARMS_IDLE_STOP = f"--unit={IDLE_STOP_UNIT} "
#: The tag kew's packaged inputs pin: a host runs no other image (#2569).
IMAGE = _expected_packaged_image_pin(packaged_agent_image_context(), "claude-code")
SLUG = "issue-2397"
#: The immutable ID the INSTANCE resolves :data:`IMAGE` to. Deliberately not
#: this machine's ID for the same tag: a placed container is launched from the
#: tag against the instance's own image store, so an attestation that named
#: the operator's ID would be describing different bytes (#2526 review).
INSTANCE_IMAGE_ID = "sha256:" + "b" * 64
#: Deliberately not shaped like a real model credential: a secret scanner
#: flagging a fixture is a gate that fails for a reason the test is not about.
#: Nothing here depends on its shape, only on where it does and does not reach.
TOKEN = "kew-fixture-model-credential-value"

#: Shaped like an Anthropic key so kew's one redaction recognises it on sight;
#: a fixture, not a credential. The remote step's stderr is retained as an
#: interruption's explanation, and the agent container writes to that stderr.
LEAKED_SHAPE = "sk-ant-" + "A" * 24

#: Two JSON event lines, exactly as a claude-code container writes them.
EVENT_LINES = (
    '{"type":"system","subtype":"init","session_id":"s-1"}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
)

#: The prompt the host writes into the checkout before a dispatch. Multi-line
#: and non-ASCII on purpose: it travels base64 on the session's stdin, and a
#: handoff that is not byte-exact is a prompt the agent was not given.
PROMPT = "Fix issue #2397.\n\n* a bullet — with an em dash\n* and a second\n"

#: The status ``docker/agent-entrypoint.sh`` exits with when the agent
#: executable is missing. A CONTAINER failure, which is why no exit status can
#: double as kew's "the handoff never completed" signal.
ENTRYPOINT_MISSING_AGENT_EXIT = 78


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake ``gcloud`` first on PATH; yields its JSON state file."""
    with fake_gcloud_on_path(tmp_path, monkeypatch) as state:
        yield state


@pytest.fixture
def config(tmp_path: Path) -> DispatchConfig:
    """A config whose dispatch runs are placed on the gcp host."""
    return DispatchConfig(
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        hosts=HostsConfig(
            gcp=GcpHostConfig(project="proj", zone="us-central1-a", instance="agent-1")
        ),
        sandbox=SandboxConfig(image=IMAGE, hosts={"dispatch": "gcp"}),
    )


@pytest.fixture
def provider(config: DispatchConfig, tmp_path: Path) -> GcpProvider:
    return GcpProvider(
        config.hosts.gcp,
        lock_dir=tmp_path / "locks",
        sleep=lambda _seconds: None,
        poll_seconds=0.0,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real git checkout with one commit, which is what a run ships."""
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
    # The prompt handoff, written by kew's own writer exactly as the dispatch
    # path writes it: into the checkout, AFTER the commit, so it is untracked
    # — which is the whole reason a git bundle cannot carry it.
    write_prompt_handoff(root, PROMPT)
    return root


def _artifact_payload() -> str:
    """A valid artifact directory, tarred and base64'd as the VM returns it."""
    content = b"the agent's work\n"
    digest = sha256(content).hexdigest()
    manifest = json.dumps(
        {
            "base": BASE_SHA,
            "entries": [{"path": "notes.md", "mode": "100644", "sha256": digest}],
            "deletions": [],
            "scope": "workspace",
        }
    ).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in (
            ("manifest.json", manifest),
            (f"blobs/{digest}", content),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(body))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _responses(*, agent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """How the instance answers each remote command, most specific first.

    Ordered deliberately: the extraction container and the agent container
    share an image and a runtime, so extraction is matched on the producer
    entrypoint before the agent is matched on its own container name.
    """
    return [
        {"match": "kew-extract-artifact", "exit": 0},
        {"match": "tar -C", "output": _artifact_payload(), "exit": 0},
        instance_image_probe(IMAGE, INSTANCE_IMAGE_ID),
        {"match": "id -u", "output": SITE_ANSWER, "exit": 0},
        agent or {"match": f"--name kew-{SLUG}", "output": EVENT_LINES, "exit": 0},
    ]


def _commands(state: Path) -> list[str]:
    """Every command the shim was asked to run over ssh, in order."""
    return [
        call[call.index("--command") + 1]
        for call in _calls(state)
        if "--command" in call
    ]


def _clone_step(commands: list[str]) -> int:
    """Where the instance cloned the shipped bundle among *commands*.

    Found by the step :func:`clone_command` builds, not by a ``git clone``
    substring: the clone runs behind an ``env`` and ``-c`` prefix that splits
    those two words apart (#2569 review).
    """
    return next(i for i, text in enumerate(commands) if clone_command() in text)


def _agent_launch(state: Path) -> str:
    """The one remote command that launched the agent container, as sent."""
    launched = [text for text in _commands(state) if f"--name kew-{SLUG}" in text]
    assert len(launched) == 1, launched
    return launched[0]


def _agent_command(state: Path) -> str:
    """The script of the one remote command that launched the agent container.

    Unwrapped from the ``sh -c`` it runs under, so its words read as the
    instance's shell will read them.
    """
    words = shlex.split(_agent_launch(state))
    at = next(
        index
        for index, word in enumerate(words[:-2])
        if word == "sh" and words[index + 1] == "-c"
    )
    return words[at + 2]


def _with_deadline(call: Any, *, hung: str, seconds: float = 120) -> dict[str, Any]:
    """Run *call* on a thread with a deadline, so a deadlock FAILS the test.

    A relay that never returns is the failure mode these tests are about, and
    asserting it in the calling thread would hang the suite instead of failing
    it. The outcome is reported either way: ``result`` or ``error``.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), hung
    return outcome


def _head(checkout: Path) -> str:
    """The commit *checkout* has checked out."""
    return subprocess_utils.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, check=True
    ).stdout.strip()


def _place(
    provider: GcpProvider,
    config: DispatchConfig,
    checkout: Path,
    workspace: Path,
    **overrides: Any,
) -> Any:
    """Place one dispatch on the host with this module's standing arguments.

    A GCP placement needs a ledger to record a lease takeover in (#2874), so
    one beside *workspace* is used unless the test passes its own ``store``.
    """
    if "store" not in overrides:
        with SqliteRunStore(str(workspace.parent / "placement-ledger.db")) as store:
            return _place(
                provider, config, checkout, workspace, store=store, **overrides
            )
    arguments: dict[str, Any] = {
        "config": config,
        "checkout_dir": checkout,
        "workspace": workspace,
        "image": IMAGE,
        "runner": "claude-code",
        "slug": SLUG,
        "model": "claude-sonnet-4-5",
        # The checkout's own commit: a ship is cut at the base, so the base has
        # to be a commit the checkout holds (#2550).
        "base_sha": _head(checkout),
        "run_kind": DISPATCH_RUN_KIND,
        "credentials": {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
    }
    arguments.update(overrides)
    return place_dispatch_on_host(provider, **arguments)


class TestALeaseTakeoverIsOnTheDispatchLedger:
    """#2874: a placement takes a dead holder's lease over only on the record."""

    def test_with_no_ledger_the_host_is_not_touched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """Refused before ``ensure_up`` could resume the instance or cancel its
        idle stop, not when the takeover finds no ledger."""
        _write_state(
            fake_gcloud,
            statuses=["SUSPENDED"],
            lease_held=True,
            lease_holder=dead_holder(),
            job_responses=_responses(),
        )

        with pytest.raises(HostError, match="no run store to record it in"):
            _place(provider, config, checkout, tmp_path / "work", store=None)

        assert _calls(fake_gcloud) == []

    def test_an_unwritable_dispatch_store_is_refused_before_any_host_call(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A ledger that will not take a write is found before the host is
        touched, not after its lease has changed."""
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=dead_holder(),
            job_responses=_responses(),
        )

        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store._conn.execute("PRAGMA query_only = ON")
            with pytest.raises(HostError, match="will not take a write"):
                _place(provider, config, checkout, tmp_path / "work", store=store)

        assert _calls(fake_gcloud) == []

    def test_the_dispatch_store_settles_the_takeover_before_the_first_copy(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        holder = dead_holder()
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            job_responses=_responses(),
        )

        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            _place(provider, config, checkout, tmp_path / "work", store=store)
            (pending,) = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
            (taken,) = store.list_audit_events(event=LEASE_LAPSED_EVENT)

        payload = taken.payload or {}
        assert payload["holder"] == holder
        assert payload["takeover_id"] == (pending.payload or {})["takeover_id"]
        first_copy = _read_state(fake_gcloud)["first_copy_at"]
        assert datetime.fromisoformat(taken.timestamp).timestamp() <= first_copy

    def test_expired_foreign_lease_is_ledgered_before_first_scp(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        holder = "foreign-operator-17-unreadable"
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_expired=True,
            lease_holder=holder,
            job_responses=_responses(),
        )

        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            _place(provider, config, checkout, tmp_path / "work", store=store)
            events = store.list_audit_events(limit=None)

        pending = [row for row in events if row.event == LEASE_TAKEOVER_PENDING_EVENT]
        terminal = [row for row in events if row.event in SETTLING_EVENTS]
        assert len(pending) == len(terminal) == 1
        assert terminal[0].event == LEASE_LAPSED_EVENT
        pending_payload = pending[0].payload or {}
        terminal_payload = terminal[0].payload or {}
        assert pending_payload["holder"] == terminal_payload["holder"] == holder
        assert pending_payload["takeover_id"] == terminal_payload["takeover_id"]
        assert pending[0].id < terminal[0].id
        first_copy = _read_state(fake_gcloud)["first_copy_at"]
        assert all(
            datetime.fromisoformat(row.timestamp).timestamp() <= first_copy
            for row in (*pending, *terminal)
        )


class TestPlacementIsConfigured:
    """``[sandbox] host`` says where an agent runs, per run kind."""

    def test_local_is_the_default_and_needs_no_cloud_account(self) -> None:
        assert DispatchConfig().sandbox.host == "local"
        assert DispatchConfig().sandbox.host_for("dispatch") == "local"

    def test_a_run_kind_override_wins_over_the_default(self) -> None:
        sandbox = SandboxConfig(host="local", hosts={"dispatch": "gcp"})

        assert sandbox.host_for("dispatch") == "gcp"
        assert sandbox.host_for("adversarial_review") == "local"

    def test_an_unknown_host_is_refused_at_load_not_at_launch(self) -> None:
        with pytest.raises(ValueError, match="sandbox.host"):
            SandboxConfig(host="aws")

    def test_an_unknown_host_in_an_override_is_refused_by_key(self) -> None:
        with pytest.raises(ValueError, match="sandbox.hosts.dispatch"):
            SandboxConfig(hosts={"dispatch": "azure"})


class TestModelCredentialAllowlist:
    """The one credential exception, and the fences around it (AC2)."""

    def test_the_runners_declared_model_credential_is_permitted(self) -> None:
        permitted = permit_model_credentials(
            {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
            runner="claude-code",
            run_kind="dispatch",
        )

        assert [credential.name for credential in permitted] == [
            "CLAUDE_CODE_OAUTH_TOKEN"
        ]
        assert permitted[0].value == TOKEN

    def test_a_github_shaped_variable_is_refused_for_the_same_job(self) -> None:
        with pytest.raises(CredentialVariableRefused, match="GH_TOKEN"):
            permit_model_credentials(
                {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "GH_TOKEN": "ghp_x"},
                runner="claude-code",
                run_kind="dispatch",
            )

    def test_another_runners_model_credential_is_refused(self) -> None:
        with pytest.raises(CredentialVariableRefused, match="OPENROUTER_API_KEY"):
            permit_model_credentials(
                {"OPENROUTER_API_KEY": "or-x"},
                runner="claude-code",
                run_kind="dispatch",
            )

    @pytest.mark.parametrize(
        "run_kind", ["adversarial_review", "review_remediation", "ci_fix"]
    )
    def test_no_credential_reaches_a_non_dispatch_job(self, run_kind: str) -> None:
        with pytest.raises(CredentialVariableRefused, match=run_kind):
            permit_model_credentials(
                {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
                runner="claude-code",
                run_kind=run_kind,
            )

    def test_an_empty_value_is_refused_rather_than_exported_blank(self) -> None:
        with pytest.raises(CredentialVariableRefused, match="empty value"):
            permit_model_credentials(
                {"CLAUDE_CODE_OAUTH_TOKEN": ""},
                runner="claude-code",
                run_kind="dispatch",
            )

    def test_asking_for_nothing_refuses_nothing(self) -> None:
        assert (
            permit_model_credentials({}, runner="claude-code", run_kind="ci_fix") == ()
        )

    def test_a_credentials_repr_carries_its_name_and_not_its_value(self) -> None:
        credential = ModelCredential(name="CLAUDE_CODE_OAUTH_TOKEN", value=TOKEN)

        assert TOKEN not in repr(credential)
        assert TOKEN not in str(credential)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in repr(credential)


class TestCredentialHandoff:
    """The value travels on stdin: never a file, never a command line."""

    def test_the_prelude_names_the_variable_and_carries_no_value(self) -> None:
        prelude = credential_prelude(
            [ModelCredential(name="CLAUDE_CODE_OAUTH_TOKEN", value=TOKEN)]
        )

        # #2569: the instance's shell holds the line and exports nothing —
        # the container takes it on its own stdin, never from ``--env``.
        assert "__kew_credential_0" in prelude
        assert "export" not in prelude
        assert TOKEN not in prelude

    def test_the_handoff_is_one_base64_line_per_credential_in_order(self) -> None:
        payload = credential_handoff(
            [
                ModelCredential(name="A_TOKEN", value="one"),
                ModelCredential(name="B_TOKEN", value="two"),
            ]
        )

        assert [base64.b64decode(line).decode() for line in payload.splitlines()] == [
            "one",
            "two",
        ]

    def test_no_credential_means_no_prelude_and_no_handoff(self) -> None:
        assert credential_prelude([]) == ""
        assert credential_handoff([]) == ""


class TestRemoteSite:
    """The instance's paths are the instance's, resolved once per placement."""

    def test_the_artifact_sits_beside_the_checkout_as_an_absolute_path(self) -> None:
        site = RemoteSite(checkout=REMOTE_CHECKOUT, uid=1000, gid=1000)

        assert site.artifact == REMOTE_ARTIFACT


class TestBundleTransport:
    """Code goes up as a git bundle; the operator's checkout is only read."""

    def test_the_bundle_clones_into_a_checkout_at_the_same_head(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=_head(checkout))
        clone = tmp_path / "clone"
        subprocess_utils.run(
            ["git", "clone", "--quiet", str(bundle), str(clone)], check=True
        )

        assert (clone / "README.md").read_text(encoding="utf-8") == "base\n"

    def test_a_directory_that_is_not_a_checkout_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(RuntimeError, match="not a git checkout"):
            bundle_checkout(tmp_path / "nowhere", tmp_path / "ship", base_sha=BASE_SHA)


class TestPlacedDispatch:
    """AC1: ship a bundle, launch the container, relay the stream, publish."""

    @pytest.fixture(autouse=True)
    def _answers(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

    def test_the_bundle_is_shipped_before_the_container_is_launched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work")

        assert _read_state(fake_gcloud)["copies"] == 1
        commands = _commands(fake_gcloud)
        cloned = _clone_step(commands)
        launched = next(
            i for i, text in enumerate(commands) if f"--name kew-{SLUG}" in text
        )
        assert cloned < launched

    def test_the_container_argv_is_the_one_kew_builds_for_a_local_run(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        from kew.dispatch_sandbox import build_agent_container_argv

        _place(provider, config, checkout, tmp_path / "work")
        command = _agent_command(fake_gcloud)
        expected = build_agent_container_argv(
            config,
            image=IMAGE,
            runner="claude-code",
            checkout_dir=Path(REMOTE_CHECKOUT),
            slug=SLUG,
            model="claude-sonnet-4-5",
            inference_transport=None,
        )

        # Every argv word kew would use locally, over the instance's checkout:
        # the caps, the egress allowlist, the read-only mounts and the
        # credential forwarded BY NAME. The cidfile is the one word that is
        # not a containment fact — it is a scratch path on whichever machine
        # runs the container — and it is asserted separately below.
        skip = {expected[expected.index("--cidfile") + 1]}
        for word in expected:
            if word in skip or word.startswith("KEW_AGENT_START_NONCE="):
                continue
            assert word in command, word
        assert f"cd {REMOTE_CHECKOUT} &&" in command
        assert "--cap-drop ALL" in command
        assert "--network kew-allowlist" in command
        assert f"-v {REMOTE_CHECKOUT}:/work" in command

    def test_the_cidfile_is_written_on_the_instance_not_on_this_machine(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A Mac's ``/var/folders`` scratch path does not exist on a Linux VM."""
        _place(provider, config, checkout, tmp_path / "work")
        command = _agent_command(fake_gcloud)
        cidfile = command.split("--cidfile ")[1].split()[0]

        assert cidfile.startswith(str(Path(REMOTE_CHECKOUT).parent) + "/")
        assert cidfile.endswith(".cid")
        assert str(tmp_path) not in command

    def test_the_event_stream_is_relayed_line_by_line_without_the_marker(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
    ) -> None:
        seen: list[str] = []

        result = _place(
            provider, config, checkout, tmp_path / "work", on_line=seen.append
        )

        assert seen == list(EVENT_LINES.splitlines(keepends=True))
        assert result.relayed == tuple(EVENT_LINES.splitlines(keepends=True))
        assert all(json.loads(line) for line in result.relayed)
        assert not any("KEW_HOST_EXIT" in line for line in result.relayed)
        assert result.exit_code == 0
        assert result.ok

    def test_the_work_is_extracted_on_the_instance_by_the_images_producer(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work")
        extraction = next(
            text for text in _commands(fake_gcloud) if "kew-extract-artifact" in text
        )

        assert "--network none" in extraction
        assert f"-v {REMOTE_CHECKOUT}:/work:ro" in extraction
        assert f"-v {REMOTE_ARTIFACT}:/artifact" in extraction
        assert "--user 1000:1000" in extraction

    def test_the_extractions_cidfile_is_on_the_instance_too(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """Extraction is a ``docker run`` like any other: its id file is a path
        on whichever machine runs it, and a Mac's ``/var/folders`` scratch path
        is one the instance's runtime cannot create — so nothing is extracted
        and the placement comes back with no work at all."""
        _place(provider, config, checkout, tmp_path / "work")
        extraction = next(
            text for text in _commands(fake_gcloud) if "kew-extract-artifact" in text
        )
        cidfile = extraction.split("--cidfile ")[1].split()[0]

        assert cidfile.startswith(str(Path(REMOTE_CHECKOUT).parent) + "/")
        assert cidfile.endswith(".cid")
        # The LOCAL scratch root the builder rooted it in, whatever it is:
        # the whole word has to have moved to the instance, and `--tmpfs /tmp`
        # is an in-container path, so the comparison is on the cidfile itself.
        assert not cidfile.startswith(str(Path(tempfile.gettempdir())) + "/")
        assert str(tmp_path) not in extraction

    def test_the_artifact_directory_is_made_by_the_login_not_by_the_daemon(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A bind-mount source the daemon has to create is created as ROOT, and
        the producer runs as ``--user 1000:1000``: it would have nowhere to
        write the work back to. The removal that keeps two artifacts from
        mixing therefore cannot be the last word on the directory."""
        _place(provider, config, checkout, tmp_path / "work")
        extraction = next(
            text for text in _commands(fake_gcloud) if "kew-extract-artifact" in text
        )
        removed = extraction.index(f"rm -rf {REMOTE_ARTIFACT}")
        made = extraction.index(f"mkdir -m 700 {REMOTE_ARTIFACT}")
        launched = extraction.index("kew-extract-artifact")

        assert removed < made < launched

    def test_the_returned_artifact_is_what_the_publication_path_consumes(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
    ) -> None:
        result = _place(provider, config, checkout, tmp_path / "work")

        # Read back by the host's own reader, with nothing remote-specific
        # about it: the same call the local container's artifact goes through.
        artifact = read_artifact(result.artifact_dir)
        assert artifact.base_sha == BASE_SHA
        assert [entry.path for entry in artifact.entries] == ["notes.md"]

        verdict = filter_manifest(
            artifact.entries,
            deletions=artifact.deletions,
            existing_modes={},
            destination_paths=(),
        )
        assert verdict.rejections == ()
        assert [entry.path for entry in verdict.approved()] == ["notes.md"]

    def test_the_host_is_released_so_no_placement_leaves_a_vm_billing(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work")

        assert any("kew-idle-stop" in text for text in _commands(fake_gcloud))


class TestTheCredentialReachesTheContainerAndNothingElse:
    """AC2: in the container's environment; in no file, no argv and no log."""

    @pytest.fixture(autouse=True)
    def _answers(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

    def test_the_container_receives_the_credential_by_name(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        result = _place(provider, config, checkout, tmp_path / "work")
        command = _agent_command(fake_gcloud)

        # #2569 item A: the name is exported INSIDE the container by its
        # handoff shell, from the container's stdin. ``--env NAME`` would have
        # the daemon store the value in the container's config on the VM.
        assert "-e CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert "export CLAUDE_CODE_OAUTH_TOKEN" in command
        assert "printf '%s\\n' \"$__kew_credential_0\" | " in command
        assert " -i --entrypoint /bin/sh " in command
        assert result.launch.credential_names == ("CLAUDE_CODE_OAUTH_TOKEN",)

    def test_the_value_is_in_no_argv_no_log_and_no_file(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        result = _place(provider, config, checkout, tmp_path / "work")

        for call in _calls(fake_gcloud):
            assert all(TOKEN not in word for word in call)
        assert all(TOKEN not in line for line in result.relayed)
        assert TOKEN not in json.dumps(result.launch.audit_payload())
        leaked = [
            path
            for path in tmp_path.rglob("*")
            if path.is_file() and TOKEN.encode() in path.read_bytes()
        ]
        assert leaked == []

    def test_a_github_shaped_variable_is_refused_before_the_vm_is_touched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        with pytest.raises(CredentialVariableRefused, match="GH_TOKEN"):
            _place(
                provider,
                config,
                checkout,
                tmp_path / "work",
                credentials={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "GH_TOKEN": "ghp_x"},
            )

        assert _calls(fake_gcloud) == []


class TestTheContainerImportsOnlyWhatWasHandedOver:
    """A bare ``-e NAME`` on an instance reads the VM's login env (#2526 review).

    The operator hands over only the names it has a value for, and the launch
    audit names exactly those. A declared name left bare on the argv would be
    filled in by the instance's daemon from whatever the VM's shell exports,
    so the container and the audit would disagree about what authenticated it.
    """

    #: Deliberately not shaped like a real credential either.
    VM_VALUE = "kew-fixture-value-the-vm-login-env-holds"

    def test_a_name_not_handed_over_is_not_on_the_argv(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        result = _place(
            provider,
            config,
            checkout,
            tmp_path / "work",
            credentials={"ANTHROPIC_API_KEY": TOKEN},
        )
        command = _agent_command(fake_gcloud)

        assert "-e ANTHROPIC_API_KEY" not in command
        assert "export ANTHROPIC_API_KEY" in command
        assert "-e CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert "export CLAUDE_CODE_OAUTH_TOKEN" not in command
        assert "unset CLAUDE_CODE_OAUTH_TOKEN" in command
        assert result.launch.credential_names == ("ANTHROPIC_API_KEY",)

    def test_a_vm_value_for_a_withheld_name_never_reaches_the_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real remote line, run by a real ``sh`` with the VM's value set.

        ``docker`` is a shell function standing in for the client and the
        daemon: it records every word it was handed — the container's config,
        which is what ``docker inspect`` prints back — and then starts the
        entrypoint in an EMPTY environment, as a container starts. The handed
        over credential must reach the agent through the container's stdin and
        appear nowhere in that config (#2569 item A); the VM's value for the
        name nobody handed over must not reach it at all. The instance's
        reaper (a ``sudo systemd-run`` timer for a named container) is not what
        this exercises, so it arms nothing here.
        """
        monkeypatch.setattr(remote_dispatch, "reaper_prelude", lambda *_a, **_k: "true")
        clone = tmp_path / "clone"
        clone.mkdir()
        subprocess_utils.ensure_tool("git")
        subprocess_utils.run(["git", "init", "--quiet"], cwd=clone, check=True)
        site = RemoteSite(checkout=str(clone), uid=1000, gid=1000)
        credentials = [ModelCredential(name="ANTHROPIC_API_KEY", value=TOKEN)]
        config_file = tmp_path / "container-config"
        command = [
            "-c",
            'printf "%s|%s" "${CLAUDE_CODE_OAUTH_TOKEN-absent}" '
            '"${ANTHROPIC_API_KEY-absent}"',
        ]
        argv = remote_dispatch.credential_stdin_argv(
            [
                "docker",
                "run",
                "--name",
                f"kew-{SLUG}",
                "-e",
                "ANTHROPIC_API_KEY",
                IMAGE,
                *command,
            ],
            image=IMAGE,
            command=command,
            handed_over=["ANTHROPIC_API_KEY"],
            entrypoint=("sh",),
        )
        daemon = (
            f"docker() {{ printf '%s\\n' \"$@\" > {config_file}; entry=; "
            'while [ "$#" -gt 0 ]; do case "$1" in '
            "--entrypoint) entry=$2; shift 2 ;; "
            f"{IMAGE}) shift; break ;; *) shift ;; esac; done; "
            'env -i PATH=/usr/bin:/bin "$entry" "$@"; }; '
        )

        session = subprocess.run(  # noqa: S603 - the remote line under a real sh
            [
                "sh",
                "-c",
                daemon
                + remote_dispatch.remote_agent_script(
                    argv,
                    site=site,
                    credentials=credentials,
                    deadline_seconds=60,
                    withheld=withheld_names("claude-code", ["ANTHROPIC_API_KEY"]),
                ),
            ],
            input=remote_agent_handoff(PROMPT, credentials),
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": self.VM_VALUE},
        )

        assert session.returncode == 0, session.stderr
        assert session.stdout == f"absent|{TOKEN}"
        assert self.VM_VALUE not in session.stdout
        container_config = config_file.read_text(encoding="utf-8")
        assert TOKEN not in container_config
        assert base64.b64encode(TOKEN.encode()).decode() not in container_config
        assert "ANTHROPIC_API_KEY\n" not in container_config.split(IMAGE)[0]

    def _launch_script(self, tmp_path: Path) -> tuple[str, Path]:
        """The real remote script, with a ``docker`` that leaves a mark."""
        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)
        site = RemoteSite(checkout=str(clone), uid=1000, gid=1000)
        credentials = [ModelCredential(name="ANTHROPIC_API_KEY", value=TOKEN)]
        argv = remote_dispatch.credential_stdin_argv(
            ["docker", "run", "--name", f"kew-{SLUG}", "-e", "ANTHROPIC_API_KEY"]
            + [IMAGE],
            image=IMAGE,
            command=[],
            handed_over=["ANTHROPIC_API_KEY"],
            entrypoint=("/usr/local/bin/kew-agent-entrypoint",),
        )
        ran = tmp_path / "docker-ran"
        # Withheld names, as every real launch has: their ``unset`` is the
        # first step after the reaper, and it is what an ungrouped ``;`` ran.
        script = remote_dispatch.remote_agent_script(
            argv,
            site=site,
            credentials=credentials,
            deadline_seconds=60,
            withheld=withheld_names("claude-code", ["ANTHROPIC_API_KEY"]),
        )
        return f"docker() {{ cat >/dev/null; : > {ran}; }}; " + script, ran

    def test_a_deadline_that_could_not_be_armed_starts_no_container(
        self, tmp_path: Path
    ) -> None:
        # #2569 review: the reaper's ``&&`` was followed by the prelude's
        # ``;``-separated steps, so docker ran after systemd-run had failed.
        script, ran = self._launch_script(tmp_path)
        credentials = [ModelCredential(name="ANTHROPIC_API_KEY", value=TOKEN)]

        session = subprocess.run(  # noqa: S603 - the remote line under a real sh
            ["sh", "-c", "sudo() { return 1; }; " + script],
            input=remote_agent_handoff(PROMPT, credentials),
            capture_output=True,
            text=True,
            check=False,
        )

        assert session.returncode != 0
        assert not ran.exists()

    def test_an_armed_deadline_does_start_the_container(self, tmp_path: Path) -> None:
        script, ran = self._launch_script(tmp_path)
        credentials = [ModelCredential(name="ANTHROPIC_API_KEY", value=TOKEN)]

        session = subprocess.run(  # noqa: S603 - the remote line under a real sh
            ["sh", "-c", "sudo() { return 0; }; " + script],
            input=remote_agent_handoff(PROMPT, credentials),
            capture_output=True,
            text=True,
            check=False,
        )

        assert session.returncode == 0, session.stderr
        assert ran.exists()

    def test_an_inherited_xtrace_never_prints_the_credential(
        self, tmp_path: Path
    ) -> None:
        # #2569 review: a VM .bashrc's xtrace would trace every expanded word
        # of the handoff. The script turns it off before anything else.
        script, ran = self._launch_script(tmp_path)
        credentials = [ModelCredential(name="ANTHROPIC_API_KEY", value=TOKEN)]

        session = subprocess.run(  # noqa: S603 - the remote line under a real sh
            ["sh", "-xc", "sudo() { return 0; }; " + script],
            input=remote_agent_handoff(PROMPT, credentials),
            capture_output=True,
            text=True,
            check=False,
        )

        assert session.returncode == 0, session.stderr
        assert ran.exists()
        encoded = base64.b64encode(TOKEN.encode()).decode()
        assert encoded not in session.stderr
        assert TOKEN not in session.stderr

    def test_the_line_runs_under_its_own_non_interactive_sh(self) -> None:
        site = RemoteSite(checkout="/home/kew/kew-host/checkout", uid=1000, gid=1000)
        arguments: dict[str, Any] = {
            "site": site,
            "credentials": [],
            "deadline_seconds": 60,
        }
        argv = ["docker", "run", "--name", f"kew-{SLUG}", IMAGE]
        script = remote_dispatch.remote_agent_script(argv, **arguments)

        assert script.startswith("set +x; ")
        assert remote_agent_command(argv, **arguments) == (
            f"sh -c {shlex.quote(script)}"
        )

    def test_a_cut_handoff_never_starts_the_agent(self) -> None:
        script = remote_dispatch.container_handoff_script(["ANTHROPIC_API_KEY"])
        ran = subprocess.run(  # noqa: S603 - the in-container handoff under sh
            ["sh", "-c", script, "kew-credential-handoff", "echo", "agent ran"],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

        assert ran.returncode == remote_dispatch.HANDOFF_FAILURE_EXIT
        assert ran.stdout.strip() == remote_dispatch.HANDOFF_FAILURE_MARKER

    def test_the_handoff_execs_the_images_own_entrypoint(self) -> None:
        argv = remote_dispatch.credential_stdin_argv(
            ["docker", "run", "-e", "A_TOKEN", "-e", "B=1", IMAGE, "--flag"],
            image=IMAGE,
            command=["--flag"],
            handed_over=["A_TOKEN"],
            entrypoint=("/usr/local/bin/kew-agent-entrypoint",),
        )

        at = argv.index(IMAGE)
        assert argv[:at] == [
            "docker",
            "run",
            "-e",
            "B=1",
            "-i",
            "--entrypoint",
            "/bin/sh",
        ]
        assert argv[at + 1] == "-c"
        assert argv[at + 3 :] == [
            "kew-credential-handoff",
            "/usr/local/bin/kew-agent-entrypoint",
            "--flag",
        ]

    def test_the_image_is_found_by_position_not_by_its_value(self) -> None:
        # #2569 review: a model name equal to the image string came LAST, so
        # a search for the value split the argv inside the command.
        command = ["--model", IMAGE]
        argv = remote_dispatch.credential_stdin_argv(
            ["docker", "run", "-e", "A_TOKEN", IMAGE, *command],
            image=IMAGE,
            command=command,
            handed_over=["A_TOKEN"],
            entrypoint=("/usr/local/bin/kew-agent-entrypoint",),
        )

        assert argv[: argv.index(IMAGE)] == [
            "docker",
            "run",
            "-i",
            "--entrypoint",
            "/bin/sh",
        ]
        assert argv[-3:] == ["/usr/local/bin/kew-agent-entrypoint", *command]

    def test_an_argv_that_does_not_end_with_image_and_command_is_refused(
        self,
    ) -> None:
        with pytest.raises(RemoteDispatchError, match="does not end with its image"):
            remote_dispatch.credential_stdin_argv(
                ["docker", "run", "-e", "A_TOKEN", IMAGE, "--flag", "extra"],
                image=IMAGE,
                command=["--flag"],
                handed_over=["A_TOKEN"],
                entrypoint=("/usr/local/bin/kew-agent-entrypoint",),
            )

    def test_the_entrypoint_is_the_one_the_packaged_image_declares(self) -> None:
        from kew.sandbox import packaged_image_entrypoint

        assert packaged_image_entrypoint("claude-code") == (
            "/usr/local/bin/kew-agent-entrypoint",
        )

    def test_a_malformed_entrypoint_is_refused_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import sandbox

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text('ENTRYPOINT ["/bin/agent"\n', encoding="utf-8")
        monkeypatch.setattr(
            sandbox, "packaged_image_dockerfile", lambda runner: dockerfile
        )

        with pytest.raises(ValueError, match="not an exec array"):
            sandbox.packaged_image_entrypoint("claude-code")

    def test_a_proxy_name_not_handed_over_is_pinned_blank_not_imported(self) -> None:
        """Pinned, not dropped: an unset proxy name is one the daemon may fill."""
        argv = [
            "docker",
            "run",
            "-e",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "-e",
            "ANTHROPIC_API_KEY",
            "-e",
            "HTTPS_PROXY",
            "-e",
            "NO_PROXY=",
            "-e",
            "KEW_ENDPOINT=http://10.0.0.1:4000",
            "image",
        ]

        assert placed_environment_argv(argv, handed_over=["ANTHROPIC_API_KEY"]) == [
            "docker",
            "run",
            "-e",
            "ANTHROPIC_API_KEY",
            "-e",
            "HTTPS_PROXY=",
            "-e",
            "NO_PROXY=",
            "-e",
            "KEW_ENDPOINT=http://10.0.0.1:4000",
            "image",
        ]

    def test_every_declared_and_proxy_name_not_handed_over_is_withheld(self) -> None:
        withheld = withheld_names("claude-code", ["ANTHROPIC_API_KEY"])

        assert "CLAUDE_CODE_OAUTH_TOKEN" in withheld
        assert "HTTPS_PROXY" in withheld
        assert "ANTHROPIC_API_KEY" not in withheld


class TestInterruption:
    """AC3: a pre-empted instance is interrupted, and a retry places it again."""

    def test_a_pre_emption_mid_run_is_interrupted_not_a_silent_success(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES,
                    "swallow": True,
                }
            ),
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert refusal.value.error_category == "remote_host_interrupted"
        # The lines that DID arrive are kept: they are the only record of how
        # far the agent got before the instance went away.
        assert refusal.value.relayed == tuple(EVENT_LINES.splitlines(keepends=True))
        assert "NOT a success" in str(refusal.value)

    def test_a_pre_emption_leaves_the_operators_checkout_untouched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "swallow": True}
            ),
        )
        before = {
            path.relative_to(checkout): path.read_bytes()
            for path in sorted(checkout.rglob("*"))
            if path.is_file()
        }

        with pytest.raises(RemoteRunInterrupted):
            _place(provider, config, checkout, tmp_path / "work")

        after = {
            path.relative_to(checkout): path.read_bytes()
            for path in sorted(checkout.rglob("*"))
            if path.is_file()
        }
        assert after == before

    def test_an_unreachable_instance_is_interrupted_with_the_same_category(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(),
            describe_exit=1,
            describe_stderr="network is unreachable\n",
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert refusal.value.error_category == "remote_host_interrupted"
        assert "agent-1" in str(refusal.value)

    def test_a_retry_after_a_pre_emption_places_the_run_again(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "swallow": True}
            ),
        )
        with pytest.raises(RemoteRunInterrupted):
            _place(provider, config, checkout, tmp_path / "work")

        # The instance recovers (a fresh spot VM); nothing about the first
        # attempt blocks the second, and the second ships its own bundle.
        _write_state(fake_gcloud, job_responses=_responses())
        result = _place(provider, config, checkout, tmp_path / "retry")

        assert result.exit_code == 0
        assert _read_state(fake_gcloud)["copies"] == 2
        assert read_artifact(result.artifact_dir).base_sha == BASE_SHA


class TestCeilings:
    """A job's own ceiling bounds the session, including a silent one."""

    #: Comfortably past one pipe buffer (64 KiB on Linux), so the handoff
    #: write BLOCKS rather than merely filling the buffer.
    BLOCKING_HANDOFF = "a line of the agent's prompt handoff\n" * 5_000

    #: How long the remote step declines to read the session's stdin, and how
    #: long the relay is given to produce a verdict anyway. The decline is the
    #: longer of the two, so a relay that only returns once the remote side
    #: lets go of stdin fails the test rather than being rescued by it.
    UNREAD_SECONDS = 30
    DEADLINE_SECONDS = 20.0

    def test_a_container_that_outruns_its_ceiling_has_no_exit_code(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "sleep": 5, "exit": 0}
            ),
        )

        with pytest.raises(HostTimeout, match="ceiling"):
            _place(provider, config, checkout, tmp_path / "work", timeout_seconds=0.3)

    def test_a_handoff_the_remote_side_never_reads_outruns_the_ceiling_too(
        self,
        provider: GcpProvider,
        fake_gcloud: Path,
    ) -> None:
        """The ceiling is armed BEFORE the handoff, not after it.

        A handoff past the pipe buffer blocks until the remote step reads it.
        Armed after the handoff, the ceiling bounds nothing in that window: a
        remote side that never reads stdin holds the claimed host — with its
        idle stop disarmed — for as long as it declines to, and the caller
        then gets whatever exit code eventually arrives instead of the
        HostTimeout its ceiling asked for.
        """
        _write_state(fake_gcloud, job_sleep=self.UNREAD_SECONDS, job_exit=0)

        outcome = _with_deadline(
            lambda: remote_dispatch.relay_remote_session(
                provider,
                f"docker run --name kew-{SLUG} {IMAGE}",
                handoff=self.BLOCKING_HANDOFF,
                timeout=0.3,
            ),
            hung=(
                "the relay never returned: the handoff blocked past the pipe "
                "buffer and no ceiling was armed around it"
            ),
            seconds=self.DEADLINE_SECONDS,
        )

        assert isinstance(outcome.get("error"), HostTimeout), outcome

    def test_an_artifact_is_never_unpacked_over_an_existing_one(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """Mixing two artifacts leaves blobs no manifest accounts for."""
        _write_state(fake_gcloud, job_responses=_responses())
        workspace = tmp_path / "work"
        _place(provider, config, checkout, workspace)

        with pytest.raises(RemoteDispatchError, match="already"):
            _place(provider, config, checkout, workspace)


class TestTheArtifactFetchCarriesTarsOwnStatus:
    """The fetch is a PIPELINE, and a pipeline's last stage is not its outcome.

    Driven through a real shell with the host contract's own wrapper
    (:func:`marker_command` / :func:`parse_exit_marker`) rather than through the
    fake instance: what is under test is which process's exit status the marker
    reports, which only a shell that actually runs the pipeline can answer.
    """

    @staticmethod
    def _marker_status(site: RemoteSite) -> tuple[int, str]:
        """Run the fetch exactly as the host contract runs it: status, stderr."""
        subprocess_utils.ensure_tool("sh")
        completed = subprocess_utils.run(
            ["sh", "-c", marker_command(remote_dispatch._artifact_fetch_command(site))],
            check=False,
        )
        exit_code, _log = parse_exit_marker(completed.stdout)
        return exit_code, completed.stderr

    def test_a_tar_that_cannot_read_the_directory_is_not_a_clean_zero(
        self, tmp_path: Path
    ) -> None:
        """base64 succeeds at encoding nothing, so tar's failure must carry.

        The explanation is asserted on the *path* tar could not read, not on
        one tar's wording: GNU tar says ``Cannot chdir: No such file or
        directory`` and the bsdtar on macOS says ``could not chdir to``, and
        which of those a developer has is not a property of kew.
        """
        site = RemoteSite(checkout=str(tmp_path / "absent" / "checkout"), uid=0, gid=0)

        exit_code, stderr = self._marker_status(site)

        assert exit_code != 0, "a failed artifact read came back as a success"
        assert site.artifact in stderr, stderr

    def test_a_readable_directory_still_comes_back_as_base64(
        self, tmp_path: Path
    ) -> None:
        """The guard must not cost the fetch its payload."""
        site = RemoteSite(checkout=str(tmp_path / "checkout"), uid=0, gid=0)
        artifact = Path(site.artifact)
        artifact.mkdir(parents=True)
        (artifact / "manifest.json").write_text("{}", encoding="utf-8")
        subprocess_utils.ensure_tool("sh")
        completed = subprocess_utils.run(
            ["sh", "-c", marker_command(remote_dispatch._artifact_fetch_command(site))],
            check=False,
        )

        exit_code, log = parse_exit_marker(completed.stdout)

        assert (exit_code, completed.stderr) == (0, "")
        payload = base64.b64decode("".join(log.split()), validate=True)
        with tarfile.open(fileobj=io.BytesIO(payload)) as bundle:
            assert "./manifest.json" in bundle.getnames()


class TestAPlacementSaysWhatRunKindItIs:
    """The credential fence reads ``run_kind``, so no caller may omit it."""

    def test_omitting_the_run_kind_is_refused_by_the_signature(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
    ) -> None:
        """A default would have granted a review caller the dispatch exception."""
        with pytest.raises(TypeError, match="run_kind"):
            place_dispatch_on_host(
                provider,
                config=config,
                checkout_dir=checkout,
                workspace=tmp_path / "work",
                image=IMAGE,
                runner="claude-code",
                slug=SLUG,
                model="claude-sonnet-4-5",
                base_sha=BASE_SHA,
                credentials={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
            )

    def test_a_review_placement_is_refused_the_runners_credential(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """And it is refused before the instance is touched."""
        _write_state(fake_gcloud, job_responses=_responses())

        with pytest.raises(CredentialVariableRefused, match="adversarial_review"):
            _place(
                provider,
                config,
                checkout,
                tmp_path / "work",
                run_kind="adversarial_review",
            )

        assert _calls(fake_gcloud) == []


class TestLaunchAudit:
    """AC4: host, instance and credential NAME, in the launch audit event."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[Any]:
        ledger = SqliteRunStore(str(tmp_path / "dispatch.db"))
        yield ledger
        ledger.close()

    def test_the_event_records_the_host_the_instance_and_the_credential_name(
        self, store: Any
    ) -> None:
        launch = RemoteLaunch(
            host="gcp",
            instance="agent-1",
            image=IMAGE,
            runner="claude-code",
            credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
        )

        record_remote_launch(store, launch, run_id="r-1", issue_num=2397)
        events = store.list_audit_events(limit=None, event=REMOTE_LAUNCH_EVENT)

        assert len(events) == 1
        payload = events[0].payload or {}
        assert payload["host"] == "gcp"
        assert payload["instance"] == "agent-1"
        assert payload["credentials"] == ["CLAUDE_CODE_OAUTH_TOKEN"]
        assert payload["runner"] == "claude-code"
        assert payload["image"] == IMAGE

    def test_the_event_keeps_the_ledger_chain_verifiable(self, store: Any) -> None:
        """A new attested fact rides in the payload, so the chain still checks."""
        launch = RemoteLaunch(
            host="gcp", instance="agent-1", image=IMAGE, runner="claude-code"
        )

        record_remote_launch(store, launch, run_id="r-1", issue_num=2397)

        assert verify_chain(list(store.read_chain_rows())).ok

    def test_the_launch_a_placement_reports_is_the_one_it_made(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        result = _place(provider, config, checkout, tmp_path / "work")
        record_remote_launch(store, result.launch, run_id="r-1", issue_num=2397)
        payload = (
            store.list_audit_events(limit=None, event=REMOTE_LAUNCH_EVENT)[0].payload
            or {}
        )

        assert payload["host"] == "gcp"
        assert payload["instance"] == "agent-1"
        assert payload["credentials"] == ["CLAUDE_CODE_OAUTH_TOKEN"]
        assert payload["image_built"] is False


class TestAPlacementRecordsWhereItWent:
    """The ledger write the idle-stop guard reads (AC3, #2398).

    ``runs.run_host`` is ``NOT NULL DEFAULT 'local'``, so a guard reading a
    column no production path writes does not read "unknown" — it reads *every
    instance is free*, which is the one answer that arms the stop under a
    working agent. The placement itself performs the write, so the writer and
    the reader are the same path.
    """

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[Any]:
        ledger = SqliteRunStore(str(tmp_path / "dispatch.db"))
        yield ledger
        ledger.close()

    @staticmethod
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

    def test_the_run_row_carries_the_host_and_instance_it_was_placed_on(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())
        store.save(self._pending("r-1", 2397))

        _place(
            provider,
            config,
            checkout,
            tmp_path / "work",
            store=store,
            run_id="r-1",
        )

        record = store.get("r-1")
        assert record is not None
        assert (record.run_host, record.run_host_instance) == ("gcp", "agent-1")

    def test_that_record_is_what_keeps_a_siblings_vm_alive(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        """AC3 end to end: two real placements, and ONE stop armed.

        Both rows are written by the placement path itself, not set by hand —
        which is the whole point. A test that sets the columns itself passes
        against a build where nothing sets them, which is exactly the build
        this covers.
        """
        _write_state(fake_gcloud, job_responses=_responses())
        store.save(self._pending("r-1", 2397))
        store.save(self._pending("r-2", 2398))

        # The sibling lands on agent-1 first and stays pending: its placement
        # is the only one in flight, so its own release does arm the stop.
        _place(
            provider,
            config,
            checkout,
            tmp_path / "work-sibling",
            store=store,
            run_id="r-2",
        )
        assert store.get("r-2").run_host_instance == "agent-1"
        armed_by_the_sibling = [
            text for text in _commands(fake_gcloud) if _ARMS_IDLE_STOP in text
        ]
        assert len(armed_by_the_sibling) == 1

        _place(
            provider,
            config,
            checkout,
            tmp_path / "work",
            store=store,
            run_id="r-1",
        )

        # The second release found r-2 still on agent-1 and armed nothing: a
        # stop here would poweroff the machine under a working agent.
        assert [
            text for text in _commands(fake_gcloud) if _ARMS_IDLE_STOP in text
        ] == armed_by_the_sibling
        # It did give up its lease, though, or r-2 would be refused the
        # instance until r-1's deadline had passed (#2627).
        last = _commands(fake_gcloud)[-1]
        assert LEASE_FILE in last and "rm -f" in last and "systemd-run" not in last

    def test_the_last_run_off_the_instance_still_arms_the_stop(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        """A recorded placement must not leave a lone run's VM billing."""
        _write_state(fake_gcloud, job_responses=_responses())
        store.save(self._pending("r-1", 2397))

        _place(
            provider,
            config,
            checkout,
            tmp_path / "work",
            store=store,
            run_id="r-1",
        )

        assert [text for text in _commands(fake_gcloud) if _ARMS_IDLE_STOP in text]

    def test_a_placement_with_no_row_to_write_refuses_before_the_instance(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        """A run_id with no row — never admitted, or already swept — cannot be
        recorded, and the UPDATE behind the write says so only in its rowcount.
        Dropped, it would put an agent on a machine the idle-stop guard reads as
        FREE, which is the one wrong answer that takes a working run down. So
        the placement refuses, and it refuses before the instance is touched.
        """
        _write_state(fake_gcloud, job_responses=_responses())

        with pytest.raises(PlacementNotRecorded, match="never-admitted"):
            _place(
                provider,
                config,
                checkout,
                tmp_path / "work",
                store=store,
                run_id="never-admitted",
            )

        assert store.get("never-admitted") is None
        assert not instance_is_occupied(store, instance="agent-1")
        assert not _commands(fake_gcloud)

    def test_nothing_is_recorded_for_a_placement_given_no_ledger(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        store: Any,
    ) -> None:
        """No store, no write, and no host touched: with no ledger to record a
        lease takeover in, a GCP placement is refused before the instance is
        (#2874), so the single-run path N5 shipped never reaches it."""
        _write_state(fake_gcloud, job_responses=_responses())
        store.save(self._pending("r-1", 2397))

        with pytest.raises(HostError, match="no run store to record it in"):
            _place(provider, config, checkout, tmp_path / "work", store=None)

        record = store.get("r-1")
        assert record is not None
        assert (record.run_host, record.run_host_instance) == ("local", None)
        assert not _commands(fake_gcloud)


def _packaged_pin() -> str:
    """The tag kew's packaged inputs pin for the default runner."""
    from kew.sandbox import _expected_packaged_image_pin, packaged_agent_image_context

    return _expected_packaged_image_pin(packaged_agent_image_context(), "claude-code")


class TestImageOnTheInstance:
    """The pinned image exists there, built from kew's PACKAGED inputs (#2569)."""

    def test_an_absent_pin_is_built_and_the_launch_says_so(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        answers = _responses()
        # The probe fails once, the build runs, and the same probe then names
        # the bytes the build produced — which is what the instance really
        # does, and what the launch has to attest.
        answers.insert(2, {"match": "image inspect", "exit": 1, "once": True})
        answers.insert(3, {"match": "docker build", "exit": 0})
        _write_state(fake_gcloud, job_responses=answers)
        pin = _packaged_pin()

        result = _place(provider, config, checkout, tmp_path / "work", image=pin)

        assert result.launch.image_built is True
        # The ID comes off the INSTANCE, after its build, so the launch names
        # the bytes a container there runs (#2526 review).
        assert result.launch.image_id == INSTANCE_IMAGE_ID
        builds = [text for text in _commands(fake_gcloud) if "docker build" in text]
        assert len(builds) == 1
        assert f"-t {pin} -" in builds[0]
        assert "-f docker/agent.Dockerfile" in builds[0]

    def test_nothing_in_the_shipped_checkout_is_executed_to_build(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        answers = _responses()
        answers.insert(2, {"match": "image inspect", "exit": 1, "once": True})
        answers.insert(3, {"match": "docker build", "exit": 0})
        _write_state(fake_gcloud, job_responses=answers)

        _place(provider, config, checkout, tmp_path / "work", image=_packaged_pin())

        build = next(t for t in _commands(fake_gcloud) if "docker build" in t)
        assert "uv run" not in build
        assert "kew sandbox build" not in build
        assert REMOTE_WORKSPACE_CHECKOUT not in build

    def test_the_instance_build_labels_the_image_with_its_inputs_digest(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        from kew.image_inputs import inputs_label
        from kew.sandbox import packaged_agent_image_context

        answers = _responses()
        answers.insert(2, {"match": "image inspect", "exit": 1, "once": True})
        answers.insert(3, {"match": "docker build", "exit": 0})
        _write_state(fake_gcloud, job_responses=answers)

        _place(provider, config, checkout, tmp_path / "work", image=_packaged_pin())

        build = next(t for t in _commands(fake_gcloud) if "docker build" in t)
        label = inputs_label(packaged_agent_image_context())
        assert f" --label {label} -t {_packaged_pin()} -" in build

    def test_a_pin_carrying_its_inputs_label_is_launched_without_a_build(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.launch.image_built is False
        assert result.launch.image_id == INSTANCE_IMAGE_ID
        assert not [t for t in _commands(fake_gcloud) if "docker build" in t]

    @pytest.mark.parametrize(
        "labels",
        [
            None,
            {"org.opencontainers.image.version": "2"},
            {AGENT_IMAGE_INPUTS_LABEL: "f" * 64},
            {AGENT_IMAGE_INPUTS_LABEL: "not-a-digest"},
        ],
        ids=["no-labels-key", "other-labels", "other-inputs", "malformed"],
    )
    def test_a_stale_pin_on_the_instance_is_rebuilt_before_launch(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        labels: dict[str, str] | None,
    ) -> None:
        """The instance holds the pin tag, but its image was not built from the
        inputs the tag names: built before the label existed, or retagged. kew
        builds the pin there itself, so it rebuilds it from its packaged inputs
        rather than launching what the tag no longer describes."""
        stale = docker_image_probe("sha256:" + "e" * 64, labels)
        answers = _responses()
        answers.insert(2, {**stale, "once": True})
        answers.insert(3, {"match": "docker build", "exit": 0})
        _write_state(fake_gcloud, job_responses=answers)

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.launch.image_built is True
        assert result.launch.image_id == INSTANCE_IMAGE_ID
        builds = [t for t in _commands(fake_gcloud) if "docker build" in t]
        assert len(builds) == 1

    @pytest.mark.parametrize(
        "labels",
        [None, {"org.opencontainers.image.version": "2"}],
        ids=["no-labels-key", "other-labels"],
    )
    def test_a_rebuilt_pin_still_without_its_inputs_label_is_never_launched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        labels: dict[str, str] | None,
    ) -> None:
        """The refusal says the label is absent, in Docker's own two shapes of
        an image without it: no ``Labels`` at all, or other labels only."""
        answers = _responses()
        answers.insert(2, docker_image_probe(INSTANCE_IMAGE_ID, labels))
        answers.insert(3, {"match": "docker build", "exit": 0})
        _write_state(fake_gcloud, job_responses=answers)

        with pytest.raises(RemoteDispatchError) as refused:
            _place(provider, config, checkout, tmp_path / "work")

        message = str(refused.value)
        assert f"carries no {AGENT_IMAGE_INPUTS_LABEL} label" in message, message
        assert "<no" not in message
        assert not [t for t in _commands(fake_gcloud) if f"--name kew-{SLUG}" in t]

    @pytest.mark.parametrize(
        "labels",
        [None, {"org.opencontainers.image.version": "2"}],
        ids=["no-labels-key", "other-labels"],
    )
    def test_an_unlabelled_image_that_is_no_pin_is_used_without_a_build(
        self, provider: GcpProvider, fake_gcloud: Path, labels: dict[str, str] | None
    ) -> None:
        """Only a pin tag names inputs. An image under any other tag that
        carries no inputs label is there, and is what the instance runs."""
        image_id = "sha256:" + "c" * 64
        _write_state(fake_gcloud, job_responses=[docker_image_probe(image_id, labels)])

        found = remote_dispatch.ensure_agent_image(
            provider, image="my-agent:v1", runner="claude-code", runtime="docker"
        )

        assert found == remote_dispatch.RemoteImage(image_id=image_id, built=False)
        assert not [t for t in _commands(fake_gcloud) if "docker build" in t]

    def test_the_build_context_is_kews_packaged_inputs(self) -> None:
        from kew.image_inputs import inputs_label
        from kew.sandbox import packaged_agent_image_context

        dockerfile, encoded, label = remote_dispatch.packaged_build_context(
            "claude-code", _packaged_pin()
        )
        context = packaged_agent_image_context()
        # The label is the one those same packaged inputs give.
        assert label == inputs_label(context)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded))) as archive:
            names = {m.name for m in archive.getmembers() if m.isfile()}
            shipped = archive.extractfile(dockerfile)
            assert shipped is not None
            assert shipped.read() == (context / dockerfile).read_bytes()
        assert names == {
            path.relative_to(context).as_posix()
            for path in (context / "docker").rglob("*")
            if path.is_file()
        }

    def test_a_tag_the_packaged_inputs_do_not_pin_is_never_built(self) -> None:
        with pytest.raises(RemoteDispatchError, match="packaged image inputs"):
            remote_dispatch.packaged_build_context("claude-code", "my-agent:custom")

    def test_an_image_that_is_not_the_packaged_one_is_never_placed(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        # #2569 review: the handoff starts the PACKAGED entrypoint, so a custom
        # image would have its own silently replaced. Refused before the
        # instance is even started, let alone anything launched on it.
        _write_state(fake_gcloud, job_responses=_responses())

        with pytest.raises(RemoteDispatchError, match="only kew's packaged image"):
            _place(provider, config, checkout, tmp_path / "work", image="my-agent:v1")

        assert _calls(fake_gcloud) == []

    def test_a_build_that_fails_refuses_the_placement_rather_than_launching(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        answers = _responses()
        answers[2] = {"match": "image inspect", "exit": 1}
        answers.insert(2, {"match": "docker build", "exit": 2, "output": "no inputs"})
        _write_state(fake_gcloud, job_responses=answers)

        with pytest.raises(RemoteDispatchError, match="could not build"):
            _place(provider, config, checkout, tmp_path / "work", image=_packaged_pin())

        assert not any(f"--name kew-{SLUG}" in text for text in _commands(fake_gcloud))


class TestTheEgressNetworkOnTheInstance:
    """The other instance prerequisite, and one a placement never creates.

    ``[sandbox] network = "allowlist"`` attaches the ``kew-allowlist``
    network. The provisioning recipe creates it now (#2607), but an instance
    built before that, or by hand, may lack it. Nothing probed it on an
    instance either, so a correctly
    provisioned instance refused the first placement with the daemon's own
    exit 125 — after the bundle, the clone and an image build that may take an
    hour (#2437 review).
    """

    PROBE = f"network inspect {ALLOWLIST_NETWORK}"

    #: What a daemon that HAS answered says about a network it does not have.
    ABSENT = f"Error: No such network: {ALLOWLIST_NETWORK}"

    #: What the client says when there is no daemon to ask — a cold instance
    #: whose dockerd has not finished starting, or a login that is not in the
    #: ``docker`` group. Quoted, because this exits non-zero exactly the way a
    #: missing network does and the words are the only thing that tells them
    #: apart (#2437 review).
    NO_DAEMON = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    )

    DAEMON_PROBE = "docker info"

    def _missing_network(
        self,
        state: Path,
        *,
        image_present: bool = True,
        output: str | None = None,
        daemon: bool = True,
    ) -> None:
        """Answer the network probe the way a daemon without it answers.

        ``daemon=False`` makes the instance's client reach no daemon at all,
        which is the other way this probe exits non-zero.
        """
        answers = _responses()
        if not image_present:
            answers[2] = {"match": "image inspect", "exit": 1}
        if not daemon:
            answers.insert(
                0,
                {"match": self.DAEMON_PROBE, "exit": 1, "output": self.NO_DAEMON},
            )
        answers.insert(
            0,
            {
                "match": self.PROBE,
                "exit": 1,
                "output": self.NO_DAEMON if not daemon else (output or self.ABSENT),
            },
        )
        _write_state(state, job_responses=answers)

    def test_the_network_the_argv_names_is_probed_before_the_launch(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        _place(provider, config, checkout, tmp_path / "work")

        commands = _commands(fake_gcloud)
        probed = next(i for i, text in enumerate(commands) if self.PROBE in text)
        launched = next(
            i for i, text in enumerate(commands) if f"--name kew-{SLUG}" in text
        )
        assert probed < launched

    def test_a_missing_network_refuses_the_placement_naming_the_fix(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        self._missing_network(fake_gcloud)

        with pytest.raises(RemoteDispatchError) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert ALLOWLIST_NETWORK in str(refusal.value)
        assert "network create --driver bridge" in str(refusal.value)
        # The daemon's own words travel with it: the probe's log is the whole
        # of the diagnosis on this path, so it is never discarded.
        assert self.ABSENT in str(refusal.value)
        # And the launch the daemon would have refused never happened.
        assert not any(f"--name kew-{SLUG}" in text for text in _commands(fake_gcloud))

    def test_a_daemon_that_answers_nothing_is_not_reported_as_a_missing_network(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """The probe's exit status alone does not prove the network is absent.

        A cold-started instance refuses ``network inspect`` with the same
        non-zero status a missing network earns, and this probe is now the
        first runtime command of a placement — so there is no later image
        probe or build whose log would have carried the real reason. Telling
        that operator to create a network is the wrong fix, and printing it
        instead of the daemon's sentence hides the right one (#2437 review).
        """
        self._missing_network(fake_gcloud, daemon=False)

        with pytest.raises(RemoteDispatchError) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert "Cannot connect to the Docker daemon" in str(refusal.value)
        assert "network create --driver bridge" not in str(refusal.value)
        assert not any(f"--name kew-{SLUG}" in text for text in _commands(fake_gcloud))

    def test_the_daemon_is_only_asked_when_the_network_probe_failed(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A placement that gets its answer pays for no second round trip."""
        _write_state(fake_gcloud, job_responses=_responses())

        _place(provider, config, checkout, tmp_path / "work")

        commands = _commands(fake_gcloud)
        assert any(self.PROBE in text for text in commands)
        assert not any(self.DAEMON_PROBE in text for text in commands)

    def test_the_probe_comes_before_an_image_build_is_paid_for(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A build may take an hour; the gap kew cannot fix is refused first."""
        self._missing_network(fake_gcloud, image_present=False)

        with pytest.raises(RemoteDispatchError, match=ALLOWLIST_NETWORK):
            _place(provider, config, checkout, tmp_path / "work")

        assert not any("docker build" in text for text in _commands(fake_gcloud))

    def test_open_egress_consults_no_network_at_all(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """``network = "open"`` omits ``--network``: there is nothing to ask."""
        _write_state(fake_gcloud, job_responses=_responses())
        opened = config.model_copy(
            update={
                "sandbox": SandboxConfig(
                    image=IMAGE, hosts={"dispatch": "gcp"}, network="open"
                )
            }
        )

        result = _place(provider, opened, checkout, tmp_path / "work")

        assert result.exit_code == 0
        assert not any("network inspect" in text for text in _commands(fake_gcloud))

    @pytest.mark.parametrize("name", sorted(remote_dispatch.BUILT_IN_NETWORKS))
    def test_a_runtimes_own_network_is_never_probed(self, name: str) -> None:
        """``--network none`` is the extraction container's, and cannot be absent."""
        assert container_network(["docker", "run", "--network", name, IMAGE]) is None

    def test_an_argv_with_no_network_flag_names_nothing_to_probe(self) -> None:
        assert container_network(["docker", "run", IMAGE]) is None
        assert container_network(["docker", "run", IMAGE, "--network"]) is None

    def test_the_probed_name_is_the_one_the_argv_names(self) -> None:
        """Read off the argv, so the probe cannot drift from the launch."""
        argv = ["docker", "run", "--network", "egress-locked", IMAGE]

        assert container_network(argv) == "egress-locked"


class TestThePromptHandoffReachesTheInstance:
    """A git bundle carries commits; the prompt is deliberately not one.

    ``git bundle create HEAD`` writes committed objects, and the dispatch path
    writes ``.kew-prompt.txt`` into the checkout as an UNTRACKED file so no
    agent commits it into its own PR branch (#725). So the one file the
    container's entrypoint must read is exactly the one the bundle cannot
    carry, and without it the entrypoint exits 64 — on an instance kew has
    already started, claimed and disarmed the idle stop on.
    """

    @pytest.fixture(autouse=True)
    def _answers(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

    def test_the_bundle_does_not_carry_the_prompt_and_the_handoff_does(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=_head(checkout))
        clone = tmp_path / "clone"
        subprocess_utils.run(
            ["git", "clone", "--quiet", str(bundle), str(clone)], check=True
        )

        # The premise of the whole handoff: the bundle's clone has no prompt.
        assert not (clone / PROMPT_FILENAME).exists()
        assert base64.b64decode(prompt_handoff(PROMPT)).decode("utf-8") == PROMPT

    def test_the_launch_writes_the_prompt_into_the_instances_checkout(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work")
        command = _agent_command(fake_gcloud)

        assert f"{REMOTE_CHECKOUT}/{PROMPT_FILENAME}" in command
        # Off stdin, like the credential: a dispatch prompt is longer than an
        # argv is guaranteed to hold, and a review prompt embeds comment
        # bodies anyone with access to the pull request can author.
        for call in _calls(fake_gcloud):
            assert all(PROMPT not in word for word in call)

    def test_a_missing_prompt_is_refused_before_the_vm_is_touched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        (checkout / PROMPT_FILENAME).unlink()

        with pytest.raises(RemoteDispatchError, match="prompt handoff"):
            _place(provider, config, checkout, tmp_path / "work")

        assert _calls(fake_gcloud) == []

    def test_an_empty_prompt_is_refused_rather_than_exiting_64_on_the_vm(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        (checkout / PROMPT_FILENAME).write_text("   \n", encoding="utf-8")

        with pytest.raises(RemoteDispatchError, match="empty"):
            _place(provider, config, checkout, tmp_path / "work")

        assert _calls(fake_gcloud) == []

    def test_the_prelude_lands_the_prompt_in_a_real_clone_and_excludes_it(
        self, tmp_path: Path
    ) -> None:
        """The prelude, run by a real ``sh`` against a real clone.

        Two facts at once, and both are the point: the prompt arrives
        byte-exact for the entrypoint to read, and the instance's clone gets
        the same worktree-local exclude the local path writes — ``git clone``
        carries none, and the artifact producer enumerates untracked work with
        ``--exclude-standard``, so without it the prompt kew just wrote comes
        back as the agent's work and is published into the branch (#725).
        """
        clone = tmp_path / "clone"
        clone.mkdir()
        subprocess_utils.ensure_tool("git")
        subprocess_utils.run(["git", "init", "--quiet"], cwd=clone, check=True)
        site = RemoteSite(checkout=str(clone), uid=1000, gid=1000)

        session = subprocess.run(  # noqa: S603 - the prelude under a real sh
            ["sh", "-c", prompt_prelude(site)],
            input=prompt_handoff(PROMPT),
            capture_output=True,
            text=True,
            check=False,
        )

        assert session.returncode == 0, session.stderr
        assert (clone / PROMPT_FILENAME).read_text(encoding="utf-8") == PROMPT
        assert (clone / PROMPT_FILENAME).stat().st_mode & 0o777 == 0o600
        untracked = subprocess_utils.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=clone,
            check=True,
        )
        assert untracked.stdout.strip() == ""

    def test_a_prelude_whose_handoff_never_arrives_says_so_on_stdout(
        self, tmp_path: Path
    ) -> None:
        """The signal is a line, not a status — see TestHandoffFailure."""
        site = RemoteSite(checkout=str(tmp_path / "clone"), uid=1000, gid=1000)

        session = subprocess.run(  # noqa: S603 - the prelude under a real sh
            ["sh", "-c", prompt_prelude(site)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

        assert session.returncode != 0
        assert session.stdout.strip() == HANDOFF_FAILURE_MARKER


class TestHandoffFailure:
    """ "The handoff failed" is a marker line, because no exit status can be it.

    ``docker/agent-entrypoint.sh`` exits 78 when the agent executable is
    missing, so an exit status of 78 is something the CONTAINER produces.
    Reading it as a failed handoff reported a container failure as
    ``RemoteRunInterrupted``/``remote_host_interrupted``, which is retried
    instead of failed.
    """

    def test_the_prelude_announces_the_failure_rather_than_only_exiting(
        self,
    ) -> None:
        prelude = credential_prelude(
            [ModelCredential(name="CLAUDE_CODE_OAUTH_TOKEN", value=TOKEN)]
        )

        assert HANDOFF_FAILURE_MARKER in prelude

    def test_a_container_that_exits_78_is_a_failure_and_not_an_interruption(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES,
                    "exit": ENTRYPOINT_MISSING_AGENT_EXIT,
                }
            ),
        )

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.exit_code == ENTRYPOINT_MISSING_AGENT_EXIT
        assert not result.ok

    def test_the_marker_before_any_event_line_is_an_interruption(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": f"{HANDOFF_FAILURE_MARKER}\n",
                    "exit": ENTRYPOINT_MISSING_AGENT_EXIT,
                }
            ),
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert refusal.value.error_category == "remote_host_interrupted"
        assert "never started" in str(refusal.value)

    def test_the_same_bytes_from_the_agent_mid_run_are_an_event_line(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """The prelude prints it BEFORE any container is launched, so a line
        that arrives after the agent has spoken is the agent's own output."""
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES + f"{HANDOFF_FAILURE_MARKER}\n",
                    "exit": 0,
                }
            ),
        )

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.exit_code == 0
        assert f"{HANDOFF_FAILURE_MARKER}\n" in result.relayed


class TestTheSessionsStderrIsDrained:
    """Its own pipe, and that pipe is READ.

    stderr stays off stdout because stdout is the stream the host parses as
    the agent's events (#2053) — but a second pipe nobody reads blocks the
    remote step as soon as the kernel buffer fills, and under the default
    unbounded ceiling it blocks for as long as the host's claim and its
    disarmed idle stop last, which is to say for ever.
    """

    #: Comfortably past one pipe buffer (64 KiB on Linux), so an undrained
    #: pipe stops the writer rather than merely filling.
    FLOOD = "a daemon notice on stderr\n" * 20_000

    HUNG = (
        "the relay never returned: the session's stderr pipe filled and "
        "nothing was reading it"
    )

    def test_a_step_that_floods_stderr_does_not_deadlock_the_relay(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES,
                    "stderr": self.FLOOD,
                    "exit": 0,
                }
            ),
        )

        outcome = _with_deadline(
            lambda: _place(provider, config, checkout, tmp_path / "work"),
            hung=self.HUNG,
        )

        assert "error" not in outcome, outcome.get("error")
        result = outcome["result"]
        assert result.exit_code == 0
        # And none of it reached the stream the host reads as agent events.
        assert result.relayed == tuple(EVENT_LINES.splitlines(keepends=True))

    def test_an_interruptions_stderr_tail_is_kept_as_its_explanation(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A lost session's only account of itself is on stderr."""
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES,
                    "swallow": True,
                }
            ),
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert "ssh: connect failed" in str(refusal.value)

    def test_a_credential_on_that_tail_is_redacted_out_of_the_verdict(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """The tail is retained text, so it goes through kew's one redaction.

        The agent container's own stderr is part of what the remote step
        writes, and this tail is kept verbatim in the interruption's retained
        message — the same surface every other container stream is redacted on
        the way into.
        """
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": EVENT_LINES,
                    "stderr": f"agent: request refused for key {LEAKED_SHAPE}\n",
                    "swallow": True,
                }
            ),
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert LEAKED_SHAPE not in str(refusal.value)
        assert "<ANTHROPIC_KEY redacted>" in str(refusal.value)
        # And the tail is still the explanation it was kept for.
        assert "ssh: connect failed" in str(refusal.value)


class TestADaemonRefusalIsNotAVerdict:
    """A launch the instance's daemon refused is not an agent's exit code.

    Docker and Podman reserve 125 for "the daemon refused this", and the whole
    explanation is on the session's stderr: nothing reaches stdout, because no
    container ran. Handed back as a bare exit 125 with an empty event stream,
    the daemon's account of itself was thrown away and a placement that never
    started read as an agent that failed its task (#2437 review).
    """

    #: What a daemon without the allowlist network actually says, quoted.
    DAEMON_REFUSAL = (
        "docker: Error response from daemon: network kew-allowlist not found.\n"
    )

    def _refused(
        self, state: Path, *, stderr: str, exit_code: int = 125, output: str = ""
    ) -> None:
        """Answer the agent launch the way a daemon that refused it answers."""
        _write_state(
            state,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "output": output,
                    "stderr": stderr,
                    "exit": exit_code,
                }
            ),
        )

    def test_the_refusal_ends_the_placement_naming_what_the_daemon_said(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        self._refused(fake_gcloud, stderr=self.DAEMON_REFUSAL)

        with pytest.raises(RemoteDispatchError) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert "refused the agent container" in str(refusal.value)
        assert "network kew-allowlist not found" in str(refusal.value)
        assert "125" in str(refusal.value)

    def test_a_name_the_daemon_still_holds_is_named_as_that(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """The local rule reads the same stderr: a taken name says so."""
        self._refused(
            fake_gcloud,
            stderr=(
                f"docker: Error response from daemon: Conflict. The container name "
                f'"/kew-{SLUG}" is already in use by container "abc".\n'
            ),
        )

        with pytest.raises(RemoteDispatchError, match="name already in use"):
            _place(provider, config, checkout, tmp_path / "work")

    def test_a_credential_on_the_refusals_tail_is_redacted(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A retained tail is a retained tail, on this surface as on the others."""
        self._refused(fake_gcloud, stderr=f"docker: refused with {LEAKED_SHAPE}\n")

        with pytest.raises(RemoteDispatchError) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert LEAKED_SHAPE not in str(refusal.value)
        assert "<ANTHROPIC_KEY redacted>" in str(refusal.value)

    def test_an_agent_that_streamed_events_and_exited_125_keeps_its_verdict(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """125 from a container that RAN is a verdict, and verdicts are the
        caller's: only a session that relayed nothing can be a refused launch."""
        self._refused(fake_gcloud, stderr="agent: giving up\n", output=EVENT_LINES)

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.exit_code == 125
        assert result.relayed == tuple(EVENT_LINES.splitlines(keepends=True))

    def test_a_failing_exit_keeps_the_stderr_that_explains_it(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """The entrypoint's own failure exits 78 and says why on stderr.

        Not a daemon refusal — the container ran — so it keeps its exit code,
        and the only account of it travels with that code rather than being
        dropped on the floor.
        """
        self._refused(
            fake_gcloud,
            stderr="agent-entrypoint.sh: claude: not found\n",
            exit_code=ENTRYPOINT_MISSING_AGENT_EXIT,
        )

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.exit_code == ENTRYPOINT_MISSING_AGENT_EXIT
        assert result.relayed == ()
        assert "claude: not found" in result.stderr


class TestTheOrphanContainerIsStopped:
    """The ceiling ends the local ssh process group, and nothing else.

    The container was started by a ``docker run`` on the instance, which the
    instance's daemon owns: a killed session leaves it running, holding the
    CPU the next placement came for and holding the ``--name kew-<slug>`` the
    next attempt asks for, which the runtime then refuses outright.
    """

    #: How long the ssh child the wrapper leaves behind holds the session's
    #: stdout, and how long the relay is given to produce a verdict anyway.
    #: The hold is the longer of the two, so a relay still waiting on it when
    #: the deadline passes fails the test rather than being rescued by it.
    ORPHAN_SECONDS = 30
    DEADLINE_SECONDS = 20.0

    def _stop_commands(self, state: Path) -> list[str]:
        """The removals that come AFTER the launch — the cleanup's, not the
        pre-launch sweep's, which frees the same name just before it."""
        commands = _commands(state)
        launched = next(
            i for i, text in enumerate(commands) if f"--name kew-{SLUG}" in text
        )
        return [
            text for text in commands[launched + 1 :] if f"rm -f kew-{SLUG}" in text
        ]

    def test_the_container_argv_names_the_container_the_cleanup_stops(self) -> None:
        site = RemoteSite(checkout=REMOTE_CHECKOUT, uid=1000, gid=1000)
        argv = ["docker", "run", "--name", f"kew-{SLUG}", "--cidfile", "/tmp/x.cid"]

        container = launched_container(argv, site=site)

        assert container is not None
        assert container.runtime == "docker"
        assert container.name == f"kew-{SLUG}"
        # Relocated onto the instance, like the argv's own word.
        assert container.cidfile == f"{Path(REMOTE_CHECKOUT).parent}/x.cid"

    def test_an_argv_that_names_no_container_leaves_the_verdict_alone(self) -> None:
        site = RemoteSite(checkout=REMOTE_CHECKOUT, uid=1000, gid=1000)

        assert launched_container(["docker", "run", "img"], site=site) is None

    def test_a_ceiling_stops_the_container_it_left_running(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "sleep": 5, "exit": 0}
            ),
        )

        with pytest.raises(HostTimeout, match="ceiling"):
            _place(provider, config, checkout, tmp_path / "work", timeout_seconds=0.3)

        stops = self._stop_commands(fake_gcloud)
        assert len(stops) == 1, _commands(fake_gcloud)
        assert "docker rm -f" in stops[0]
        # And the id file the local argv asked for, re-rooted onto the
        # instance, goes with it rather than accumulating in the workspace.
        assert ".cid" in stops[0]

    def test_an_ssh_child_outliving_the_wrapper_does_not_outlive_the_ceiling(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """``gcloud compute ssh`` is a wrapper; its ``ssh`` holds the pipe.

        Ending only the pid kew holds leaves that child with the write end of
        the session's stdout, so the relay's read never reaches EOF: no
        HostTimeout is raised, the cleanup below never runs, and the host's
        claim is held with its idle stop disarmed for as long as the child
        lives. The ceiling therefore ends the session's whole process group.
        """
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={
                    "match": f"--name kew-{SLUG}",
                    "orphan": self.ORPHAN_SECONDS,
                }
            ),
        )

        outcome = _with_deadline(
            lambda: _place(
                provider, config, checkout, tmp_path / "work", timeout_seconds=0.3
            ),
            hung=(
                "the ceiling fired but the relay never returned: an ssh child "
                "outlived the pid it killed and held the session's stdout open"
            ),
            seconds=self.DEADLINE_SECONDS,
        )

        assert isinstance(outcome.get("error"), HostTimeout), outcome
        assert len(self._stop_commands(fake_gcloud)) == 1, _commands(fake_gcloud)

    def test_an_interrupted_session_stops_the_container_too(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "swallow": True}
            ),
        )

        with pytest.raises(RemoteRunInterrupted):
            _place(provider, config, checkout, tmp_path / "work")

        assert len(self._stop_commands(fake_gcloud)) == 1

    def test_the_cleanup_is_ordered_before_the_host_is_released(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """Inside the claim: a release arms the idle stop, and a container
        still running past it is a container running on a stopping host."""
        _write_state(
            fake_gcloud,
            job_responses=_responses(
                agent={"match": f"--name kew-{SLUG}", "swallow": True}
            ),
        )

        with pytest.raises(RemoteRunInterrupted):
            _place(provider, config, checkout, tmp_path / "work")

        commands = _commands(fake_gcloud)
        launched = commands.index(_agent_launch(fake_gcloud))
        stopped = next(
            i
            for i, t in enumerate(commands)
            if i > launched and f"rm -f kew-{SLUG}" in t
        )
        # The ARMING of the idle stop: the disarm the claim already ran
        # names the same unit, and it runs before the container is launched.
        released = next(i for i, t in enumerate(commands) if _ARMS_IDLE_STOP in t)
        assert stopped < released

    def test_a_cleanup_the_dead_instance_refuses_keeps_the_runs_verdict(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A pre-empted instance has no daemon left to ask, and the
        interruption is still what the caller is told."""
        _write_state(
            fake_gcloud,
            job_responses=[
                *_responses(agent={"match": f"--name kew-{SLUG}", "swallow": True}),
                {"match": f"rm -f kew-{SLUG}", "exit": 1, "output": "no such host"},
            ],
        )

        with pytest.raises(RemoteRunInterrupted) as refusal:
            _place(provider, config, checkout, tmp_path / "work")

        assert refusal.value.error_category == "remote_host_interrupted"


class TestTheStaleContainerNameIsSwept:
    """The per-item name is freed on the instance BEFORE the launch asks for it.

    :func:`stop_remote_container` runs in the operator process, so a placement
    whose operator was killed leaves ``kew-<slug>`` on the instance with
    nothing left to remove it. Every later placement for that item is then
    refused by the daemon with exit 125 before the agent writes a line — an
    interruption with an empty event stream, for a reason nothing in the run's
    own output names.
    """

    @pytest.fixture(autouse=True)
    def _answers(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

    def test_the_name_is_freed_before_the_container_is_launched(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work")
        commands = _commands(fake_gcloud)
        launched = commands.index(_agent_launch(fake_gcloud))
        swept = [i for i, t in enumerate(commands) if f"rm -f kew-{SLUG}" in t]

        assert swept, commands
        assert swept[0] < launched
        # The NAME only: this launch's own id file does not exist yet, and a
        # previous launch's is not a path this argv can name.
        assert ".cid" not in commands[swept[0]]
        assert f"docker rm -f kew-{SLUG}" in commands[swept[0]]
        assert commands[swept[0]].count("rm -f") == 1

    def test_a_sweep_the_instance_refuses_does_not_fail_the_placement(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """A removal that exits non-zero is the ORDINARY case — no such
        container — so it cannot be what refuses the run."""
        _write_state(
            fake_gcloud,
            job_responses=[
                {
                    # The sweep job alone: the launch names the same removal
                    # in its deadline timer, which this must not answer.
                    "match": f"sh -c 'docker rm -f kew-{SLUG}'",
                    "exit": 1,
                    "output": "no such container",
                },
                *_responses(),
            ],
        )

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.ok
        assert result.relayed == tuple(EVENT_LINES.splitlines(keepends=True))

    def test_an_instance_that_cannot_be_asked_still_gets_its_launch(self) -> None:
        """The sweep is best effort: a provider that raises leaves the
        placement to make its own verdict, as the cleanup's failure does."""
        asked: list[str] = []

        class _Refusing:
            name = "gcp"
            address = "agent-1"

            def run(self, job: Any) -> Any:
                asked.append(job.name)
                raise HostError("could not reach the instance")

        container = launched_container(
            ["docker", "run", "--name", f"kew-{SLUG}", "img"],
            site=RemoteSite(checkout=REMOTE_CHECKOUT, uid=1000, gid=1000),
        )

        sweep_remote_container(_Refusing(), container)  # type: ignore[arg-type]

        assert asked == ["sweep-container"]

    def test_an_argv_that_names_no_container_sweeps_nothing(self) -> None:
        class _Unasked:
            name = "gcp"
            address = "agent-1"

            def run(self, job: Any) -> Any:
                raise AssertionError(f"nothing to sweep, yet {job.name} ran")

        sweep_remote_container(_Unasked(), None)  # type: ignore[arg-type]


class _InterruptedStdin:
    """A session stdin that escapes mid-write, as a blocked handoff can.

    A handoff past the pipe buffer blocks until the remote step reads it, so
    the operator's ``KeyboardInterrupt`` lands here rather than in the relay's
    own read loop.
    """

    def __init__(self) -> None:
        self.closed = False

    def write(self, _payload: str) -> int:
        raise KeyboardInterrupt

    def flush(self) -> None:
        raise AssertionError("the interrupted write never reaches the flush")

    def close(self) -> None:
        self.closed = True


class _StubSession:
    """Just enough of a session for the leaves-nothing-running rule."""

    def __init__(
        self, *, pid: int, running: bool, stdin: _InterruptedStdin | None = None
    ) -> None:
        self.pid = pid
        self.stdout = self.stderr = None
        self.stdin = stdin
        self.killed = False
        self._running = running

    def poll(self) -> int | None:
        return None if self._running else 0

    def kill(self) -> None:
        self.killed = True


class TestTheRelayLeavesNoSessionRunning:
    """The session is its own, so no terminal signal reaches it any more.

    Starting the session in a new one is what lets the ceiling end the whole
    group — and it is also what takes the session out of kew's own group, so a
    ``KeyboardInterrupt`` at the operator's terminal no longer ends it the way
    it used to. However the relay leaves, it ends a session still running, or
    it leaves an ssh session and the container behind it alive on the instance.
    """

    @pytest.fixture
    def signalled(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(remote_dispatch.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(
            remote_dispatch.os, "killpg", lambda pgid, sig: sent.append((pgid, sig))
        )
        return sent

    def test_a_session_still_running_is_ended_by_its_group(
        self, signalled: list[tuple[int, int]]
    ) -> None:
        session = _StubSession(pid=4242, running=True)

        remote_dispatch._close_session(session)  # type: ignore[arg-type]

        assert signalled == [(4242, signal.SIGKILL)]
        assert session.killed

    def test_a_session_that_already_exited_is_not_signalled(
        self, signalled: list[tuple[int, int]]
    ) -> None:
        """Its pid may belong to something else by now."""
        session = _StubSession(pid=4242, running=False)

        remote_dispatch._close_session(session)  # type: ignore[arg-type]

        assert signalled == []
        assert not session.killed

    def test_an_escape_during_the_handoff_is_inside_the_cleanup_guard(
        self,
        provider: GcpProvider,
        signalled: list[tuple[int, int]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The handoff runs BEFORE the relay's read loop and blocks once the
        payload passes the pipe buffer. An escape in that window has to reach
        the same cleanup, or the session kew just took out of its own process
        group stays detached — ssh and the remote container both still alive,
        with no terminal signal left to end either."""
        session = _StubSession(pid=4242, running=True, stdin=_InterruptedStdin())
        monkeypatch.setattr(
            remote_dispatch, "_ssh_session", lambda _provider, _command: session
        )

        with pytest.raises(KeyboardInterrupt):
            remote_dispatch.relay_remote_session(
                provider, f"docker run --name kew-{SLUG} {IMAGE}", handoff=PROMPT
            )

        assert signalled == [(4242, signal.SIGKILL)]
        assert session.killed


def _commit(checkout: Path, name: str, text: str) -> str:
    """Commit *text* as *name* in *checkout* and return the new head."""
    (checkout / name).write_text(text, encoding="utf-8")
    subprocess_utils.run(["git", "add", name], cwd=checkout, check=True)
    subprocess_utils.run(
        ["git", "commit", "--quiet", "-m", f"add {name}"], cwd=checkout, check=True
    )
    return _head(checkout)


def _clone_as_the_instance(bundle: Path, home: Path) -> Path:
    """Run the instance's own clone step against *bundle*, in a fake home."""
    from kew.hosts.gcp import REMOTE_CHECKOUT as CHECKOUT
    from kew.hosts.gcp import REMOTE_WORKSPACE, clone_command

    (home / REMOTE_WORKSPACE).mkdir(parents=True)
    shipped = home / "shm" / "repo.bundle"
    shipped.parent.mkdir(parents=True)
    shipped.write_bytes(bundle.read_bytes())
    subprocess.run(  # noqa: S603 — the provider's own fixed step
        ["/bin/sh", "-c", clone_command(str(shipped))], cwd=home, check=True
    )
    return home / CHECKOUT


class TestTheShipIsShallow:
    """#2550 finding 5: only the base and the branch head travel, then go."""

    def test_history_behind_the_base_is_not_shipped(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        old = _head(checkout)
        base = _commit(checkout, "base.txt", "base\n")
        head = _commit(checkout, "work.txt", "work\n")

        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess_utils.run(["git", *args], cwd=clone, check=False)

        assert git("rev-parse", "HEAD").stdout.strip() == head
        assert git("log", "--format=%H").stdout.split() == [head, base]
        assert git("cat-file", "-e", f"{old}^{{commit}}").returncode != 0
        assert (clone / ".git" / "shallow").read_text().split() == [base]
        # The work is still extractable against its base on the instance.
        assert git("diff", "--name-only", base, "HEAD").stdout.split() == ["work.txt"]

    def test_the_bundle_is_deleted_once_it_is_cloned(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        from kew.hosts.gcp import REMOTE_WORKSPACE

        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=_head(checkout))
        home = tmp_path / "home"
        _clone_as_the_instance(bundle, home)

        assert not (home / "shm" / "repo.bundle").exists()
        assert not (home / REMOTE_WORKSPACE / "repo.bundle").exists()

    def test_the_instance_clone_runs_no_filter_lfs_fetch_or_hook(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2569 review: the branch's .gitattributes choose the filters a
        # checkout runs, and the clone checks it out on the instance's host.
        base = _head(checkout)
        pointer = "version https://git-lfs.github.com/spec/v1\nsize 12\n"
        # data.bin before the attribute that names its filter: this host's own
        # git (a system git-lfs) would otherwise clean it on `git add`, and
        # the commit would hold a pointer to it rather than these bytes.
        _commit(checkout, "data.bin", pointer)
        _commit(checkout, ".gitattributes", "*.bin filter=lfs\n")
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        home = tmp_path / "home"
        home.mkdir()
        ran = tmp_path / "ran"
        (home / ".gitconfig").write_text(
            f'[filter "lfs"]\n\tsmudge = touch {ran}; echo fetched\n'
            "\trequired = true\n"
            f"[core]\n\thooksPath = {tmp_path / 'hooks'}\n",
            encoding="utf-8",
        )
        hook = tmp_path / "hooks" / "post-checkout"
        hook.parent.mkdir()
        hook.write_text(f"#!/bin/sh\ntouch {ran}\n", encoding="utf-8")
        hook.chmod(0o755)
        monkeypatch.setenv("HOME", str(home))

        clone = _clone_as_the_instance(bundle, home)

        assert not ran.exists()
        assert (clone / "data.bin").read_text(encoding="utf-8") == pointer

    @pytest.mark.parametrize("carrier", ["count", "parameters"])
    def test_command_scope_git_config_from_the_login_runs_no_filter(
        self,
        checkout: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        carrier: str,
    ) -> None:
        # #2569 review round 2: GIT_CONFIG_COUNT/KEY/VALUE and
        # GIT_CONFIG_PARAMETERS define config at command scope, which the
        # NOSYSTEM/GLOBAL settings never reach.
        base = _head(checkout)
        _commit(checkout, ".gitattributes", "*.bin filter=evil\n")
        _commit(checkout, "data.bin", "payload\n")
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        ran = tmp_path / "ran"
        smudge = f"touch {ran}; cat"
        if carrier == "count":
            monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
            monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.smudge")
            monkeypatch.setenv("GIT_CONFIG_VALUE_0", smudge)
            monkeypatch.setenv("GIT_CONFIG_KEY_1", "filter.evil.required")
            monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")
        else:
            monkeypatch.setenv(
                "GIT_CONFIG_PARAMETERS",
                f"'filter.evil.smudge'='{smudge}' 'filter.evil.required'='true'",
            )

        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        assert not ran.exists()
        assert (clone / "data.bin").read_text(encoding="utf-8") == "payload\n"

    def test_a_base_the_head_does_not_descend_from_is_refused(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(RemoteDispatchError, match="cannot bundle"):
            bundle_checkout(checkout, tmp_path / "ship", base_sha="b" * 40)
        assert not (tmp_path / "ship" / "repo.bundle").exists()

    def test_a_held_credential_above_the_base_is_never_shipped(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        held = "kew-fixture-forge-credential-0123456789abcdef"
        monkeypatch.setenv("GH_TOKEN", held)
        base = _head(checkout)
        _commit(checkout, "leak.txt", f"token = {held}\n")

        with pytest.raises(RemoteDispatchError, match="secret scan found"):
            bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        assert not (tmp_path / "ship" / "repo.bundle").exists()


class TestTheShipCarriesOnlyWhatWasScanned:
    """#2569 defects 3, 4 and 6: the pack is the scanned range, cut once."""

    def test_a_merge_from_behind_the_base_ships_nothing_behind_it(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        behind = _head(checkout)

        def run(*args: str) -> None:
            subprocess_utils.run(["git", *args], cwd=checkout, check=True)

        run("checkout", "--quiet", "-b", "side")
        side = _commit(checkout, "side.txt", "side\n")
        run("checkout", "--quiet", "-")
        base = _commit(checkout, "base.txt", "base\n")
        _commit(checkout, "work.txt", "work\n")
        run("merge", "--quiet", "--no-edit", side)
        head = _head(checkout)

        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess_utils.run(["git", *args], cwd=clone, check=False)

        assert git("rev-parse", "HEAD").stdout.strip() == head
        # The commit the side branch forked from is older than the base: a
        # depth that reaches the base reaches it too, and it must not travel.
        assert git("cat-file", "-e", f"{behind}^{{commit}}").returncode != 0
        assert sorted((clone / ".git" / "shallow").read_text().split()) == sorted(
            [base, side]
        )
        assert git("fsck", "--connectivity-only").returncode == 0
        assert sorted(git("diff", "--name-only", base, "HEAD").stdout.split()) == [
            "side.txt",
            "work.txt",
        ]

    def test_a_merge_from_behind_the_base_carries_the_blobs_the_base_replaced(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # The side branch's tree still holds the version of shared.txt the
        # base replaced: that blob is reachable only from history behind the
        # base, yet a shipped commit's tree names it, so it must travel.
        behind = _commit(checkout, "shared.txt", "old\n")

        def run(*args: str) -> None:
            subprocess_utils.run(["git", *args], cwd=checkout, check=True)

        run("checkout", "--quiet", "-b", "side")
        side = _commit(checkout, "side.txt", "side\n")
        run("checkout", "--quiet", "-")
        base = _commit(checkout, "shared.txt", "new\n")
        run("merge", "--quiet", "--no-edit", "-X", "ours", side)
        head = _head(checkout)

        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess_utils.run(["git", *args], cwd=clone, check=False)

        assert git("rev-parse", "HEAD").stdout.strip() == head
        assert git("cat-file", "-e", f"{behind}^{{commit}}").returncode != 0
        assert git("fsck", "--connectivity-only").returncode == 0
        assert git("show", f"{side}:shared.txt").stdout == "old\n"
        assert (clone / "shared.txt").read_text(encoding="utf-8") == "new\n"

    def test_a_held_credential_the_base_removed_is_scanned_where_it_ships(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2569 review: the push's scan trusts everything the base's history
        # reaches, but a side commit merged in from behind the base names a
        # blob the base removed, and the bundle packs that blob. What ships is
        # what is scanned, so the credential in it refuses the ship.
        held = "kew-fixture-forge-credential-0123456789abcdef"
        monkeypatch.setenv("GH_TOKEN", held)
        _commit(checkout, "shared.txt", f"token = {held}\n")

        def run(*args: str) -> None:
            subprocess_utils.run(["git", *args], cwd=checkout, check=True)

        run("checkout", "--quiet", "-b", "side")
        side = _commit(checkout, "side.txt", "side\n")
        run("checkout", "--quiet", "-")
        base = _commit(checkout, "shared.txt", "clean\n")
        run("merge", "--quiet", "--no-edit", "-X", "ours", side)

        with pytest.raises(RemoteDispatchError, match="secret scan found"):
            bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        assert not (tmp_path / "ship" / "repo.bundle").exists()

    def test_a_blob_the_base_deleted_ships_only_as_a_scanned_object(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #2569 review round 2: the exact-deletion shape. The base deleted
        # gone.txt; a side branch forked behind it still holds the blob and is
        # merged in, so the bundle carries it. Every object the bundle carries
        # is either one the scan read or the base's own (published) state.
        from kew import publication_scan

        gone = _commit(checkout, "gone.txt", "removed by the base\n")

        def run(*args: str) -> None:
            subprocess_utils.run(["git", *args], cwd=checkout, check=True)

        run("checkout", "--quiet", "-b", "side")
        side = _commit(checkout, "side.txt", "side\n")
        run("checkout", "--quiet", "-")
        run("rm", "--quiet", "gone.txt")
        run("commit", "--quiet", "-m", "base deletes gone.txt")
        base = _head(checkout)
        run("merge", "--quiet", "--no-edit", side)
        real_scan = publication_scan.scan_output
        scanned: list[str] = []

        def recording_scan(target: Any, values: Any) -> Any:
            scanned.extend(target.objects)
            return real_scan(target, values)

        monkeypatch.setattr(publication_scan, "scan_output", recording_scan)
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        def objects(repo: Path, *args: str) -> set[str]:
            return set(
                subprocess_utils.run(
                    ["git", *args], cwd=repo, check=True
                ).stdout.split()
            )

        shipped = objects(
            clone,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        )
        # The base commit and its whole tree: what the scan trusts as published.
        published = {
            line.split(" ", 1)[0]
            for line in subprocess_utils.run(
                ["git", "rev-list", "--objects", "--no-walk", base],
                cwd=checkout,
                check=True,
            ).stdout.splitlines()
        }
        deleted_blob = subprocess_utils.run(
            ["git", "rev-parse", f"{gone}:gone.txt"], cwd=checkout, check=True
        ).stdout.strip()

        assert (
            subprocess_utils.run(
                ["git", "cat-file", "-e", f"{base}:gone.txt"], cwd=checkout, check=False
            ).returncode
            != 0
        )
        assert deleted_blob in shipped
        assert deleted_blob in scanned
        assert shipped - published == set(scanned)
        assert (
            subprocess_utils.run(
                ["git", "show", f"{side}:gone.txt"], cwd=clone, check=True
            ).stdout
            == "removed by the base\n"
        )

    def test_the_bundle_carries_the_head_the_scan_read(
        self, checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = _head(checkout)
        scanned = _commit(checkout, "work.txt", "work\n")
        real_scan = remote_dispatch._scan_shipped
        seen: list[str] = []

        def scan_then_move(
            source: Path, base_sha: str, head: str, objects: Sequence[str]
        ) -> None:
            real_scan(source, base_sha, head, objects)
            seen.append(head)
            # HEAD moves after the scan: the unscanned commit must not ship.
            _commit(checkout, "late.txt", "late\n")

        monkeypatch.setattr(remote_dispatch, "_scan_shipped", scan_then_move)
        bundle = bundle_checkout(checkout, tmp_path / "ship", base_sha=base)
        clone = _clone_as_the_instance(bundle, tmp_path / "home")

        head = subprocess_utils.run(["git", "rev-parse", "HEAD"], cwd=clone)
        assert seen == [scanned]
        assert head.stdout.strip() == scanned
        assert not (clone / "late.txt").exists()

    def test_a_stale_bundle_is_removed_before_a_refused_ship(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        stale = tmp_path / "ship" / "repo.bundle"
        stale.parent.mkdir()
        stale.write_bytes(b"an earlier run's bundle")

        with pytest.raises(RemoteDispatchError):
            bundle_checkout(checkout, tmp_path / "ship", base_sha="b" * 40)
        assert not stale.exists()

    def test_the_instance_bundle_lives_on_tmpfs_and_every_ship_clears_it(
        self,
    ) -> None:
        from kew.hosts import gcp

        assert gcp._REMOTE_BUNDLE.startswith("/dev/shm/")
        prepare = gcp._PREPARE_SHIP
        assert prepare.startswith("rm -rf ")
        removed = prepare.split("&&")[0].split()[2:]
        assert gcp._REMOTE_BUNDLE_DIR in removed
        assert gcp._LEGACY_REMOTE_BUNDLE in removed
        assert f"mkdir -m 700 {gcp._REMOTE_BUNDLE_DIR}" in prepare


class TestTheContainerHasAnInstanceDeadline:
    """#2550 finding 4: the instance itself removes the container on time."""

    @pytest.fixture(autouse=True)
    def _answers(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

    def test_the_launch_arms_a_removal_before_the_container_starts(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _place(provider, config, checkout, tmp_path / "work", timeout_seconds=600)
        command = _agent_command(fake_gcloud)

        unit = f"{remote_dispatch.REAPER_UNIT_PREFIX}kew-{SLUG}"
        seconds = int(600 + remote_dispatch.REAPER_GRACE_SECONDS)
        arm = (
            f"sudo systemd-run --quiet --collect --on-active={seconds}s "
            f"--unit={unit} "
            f"docker rm -f kew-{SLUG} && "
        )
        assert arm in command
        # Chained before the launch: a deadline that cannot be armed starts
        # nothing, and a stale timer of the same name is stopped first.
        assert command.index(arm) < command.index("docker run")
        assert command.index(f"systemctl stop {unit}.timer") < command.index(arm)
        assert command.index(f"reset-failed {unit}.timer") < command.index(arm)

    def test_a_run_with_no_ceiling_still_gets_one(self) -> None:
        assert remote_dispatch.remote_deadline(None) == (
            remote_dispatch.REMOTE_DEADLINE_SECONDS
            + remote_dispatch.REAPER_GRACE_SECONDS
        )

    def test_a_launch_naming_no_container_is_refused(self) -> None:
        with pytest.raises(RemoteDispatchError, match="names no container"):
            remote_dispatch.reaper_prelude(None, deadline_seconds=60)


class TestACleanExitLeavesNothingOnTheInstance:
    """#2609: the reaper timer, checkout, artifact and id file go with the run."""

    def _clearing(self, state: Path) -> list[str]:
        marker = f"is-active --quiet {remote_dispatch.REAPER_UNIT_PREFIX}"
        return [text for text in _commands(state) if marker in text]

    def test_the_last_step_disarms_the_reaper_and_removes_the_leftovers(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        _place(provider, config, checkout, tmp_path / "work")

        clearing = self._clearing(fake_gcloud)
        assert len(clearing) == 1, _commands(fake_gcloud)
        command = clearing[0]
        unit = f"{remote_dispatch.REAPER_UNIT_PREFIX}kew-{SLUG}"
        workspace = str(Path(REMOTE_CHECKOUT).parent)
        cidfile = _agent_command(fake_gcloud).split("--cidfile ")[1].split()[0]
        # The container name first, so nothing outlives its deadline; then the
        # timer is stopped and its state reset; then the three leftovers, and
        # only then is the timer asked whether it is really gone.
        removal = command.index(f"docker rm -f kew-{SLUG}")
        stop = command.index(f"sudo systemctl stop {unit}.timer {unit}.service")
        reset = command.index(f"sudo systemctl reset-failed {unit}.timer")
        removed = command.index(
            f"rm -rf -- {REMOTE_CHECKOUT} {workspace}/"
            f"{remote_dispatch.REMOTE_ARTIFACT_DIR} {cidfile} && "
        )
        checked = command.index(f"! systemctl is-active --quiet {unit}.timer")
        assert removal < stop < reset < removed < checked
        # After the artifact came back: the fetch reads what this removes.
        commands = _commands(fake_gcloud)
        fetched = next(i for i, text in enumerate(commands) if "tar -C" in text)
        assert fetched < commands.index(command)

    def test_a_failed_clearing_keeps_the_verdict_and_says_so(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=[
                {
                    "match": f"is-active --quiet {remote_dispatch.REAPER_UNIT_PREFIX}",
                    "output": "busy",
                    "exit": 1,
                },
                *_responses(),
            ],
        )

        with caplog.at_level("WARNING", logger=remote_dispatch.__name__):
            result = _place(provider, config, checkout, tmp_path / "work")

        assert result.ok
        assert "next ship" in caplog.text

    def test_an_unreachable_instance_keeps_the_verdict(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Unreachable:
            address = "10.0.0.9"

            def run(self, job: Any) -> Any:
                raise HostError("ssh: connection refused")

        site = RemoteSite(checkout=REMOTE_CHECKOUT, uid=1000, gid=1000)
        with caplog.at_level("WARNING", logger=remote_dispatch.__name__):
            remote_dispatch.clear_finished_run(_Unreachable(), None, site=site)  # type: ignore[arg-type]

        assert "connection refused" in caplog.text

    def test_a_failed_run_is_not_cleared(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """No artifact on this machine yet: the checkout is the only copy."""
        _write_state(
            fake_gcloud,
            job_responses=[
                {"match": "kew-extract-artifact", "exit": 3},
                *_responses(),
            ],
        )

        with pytest.raises(RemoteDispatchError):
            _place(provider, config, checkout, tmp_path / "work")

        assert self._clearing(fake_gcloud) == []


class TestTheInstanceLendsNoCloudIdentity:
    """#2550 finding 2: an instance with a service account runs nothing."""

    @pytest.mark.parametrize(
        ("exit_code", "reason"),
        [(3, "has a service account attached"), (4, "could not prove")],
    )
    def test_a_metadata_token_refuses_before_anything_is_shipped(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
        exit_code: int,
        reason: str,
    ) -> None:
        _write_state(
            fake_gcloud,
            job_responses=[
                {"match": "computeMetadata", "exit": exit_code, "output": "default/"},
                *_responses(),
            ],
        )

        with pytest.raises(RemoteDispatchError, match=reason):
            _place(provider, config, checkout, tmp_path / "work")

        assert not [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "scp"]
        ]
        assert not [text for text in _commands(fake_gcloud) if "docker run" in text]

    @pytest.mark.parametrize(
        ("status", "headers", "curl_exit", "verdict"),
        [
            # The metadata server's own "no such account": the one clean answer.
            ("404", "Metadata-Flavor: Google", 0, 0),
            # A proxy's refusal, or a 404 the server did not sign: unknown.
            ("403", "Metadata-Flavor: Google", 0, 4),
            ("404", "Server: squid", 0, 4),
            # A listing, even an empty one, is never "no account".
            ("200", "Metadata-Flavor: Google", 0, 3),
            # No server answered: unknown, never clean.
            ("000", "", 7, 4),
        ],
    )
    def test_the_probe_reads_only_the_servers_refusal_as_clean(
        self, status: str, headers: str, curl_exit: int, verdict: int
    ) -> None:
        # A shell function stands in for curl: ``command -v`` finds it, and it
        # needs no executable bit on a temporary filesystem that may lack one.
        # It answers the way ``-D - -w`` makes curl answer: headers, then the
        # status line the probe asked for.
        curl_body = (
            f"printf 'HTTP/1.1 {status} X\\r\\n{headers}\\r\\n\\r\\n'; "
            f"printf '\\n{remote_dispatch._IDENTITY_STATUS}{status}\\n'; "
            f"exit {curl_exit}"
        )
        ran = subprocess.run(  # noqa: S603 — the module's own fixed probe
            [
                "/bin/sh",
                "-c",
                f"curl() {{ {curl_body}; }}; {remote_dispatch._identity_probe()}",
            ],
            check=False,
        )
        assert ran.returncode == verdict

    def test_the_probe_goes_to_the_server_and_nowhere_else(self) -> None:
        probe = remote_dispatch._identity_probe()
        request = probe[probe.index("curl -q") :]
        # ``-q`` must be the FIRST option or a .curlrc is still read.
        assert request.startswith("curl -q --noproxy '*' ")
        assert " -L" not in request
        assert "-f " not in request
        # The link-local IP, never a name the instance's resolver could repoint.
        assert (
            "' http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/)"
            in request
        )
        assert "metadata.google.internal" not in probe

    def test_the_probe_runs_outside_the_checkout_it_precedes(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        # #2569 defect 5: as a job, the probe cd'd into a checkout that ship()
        # had not made yet, so a fresh instance refused every placement.
        _write_state(fake_gcloud, job_responses=_responses())
        _place(provider, config, checkout, tmp_path / "work")

        probes = [t for t in _commands(fake_gcloud) if "computeMetadata" in t]
        assert len(probes) == 1
        assert REMOTE_WORKSPACE_CHECKOUT not in probes[0]
        commands = _commands(fake_gcloud)
        assert commands.index(probes[0]) < _clone_step(commands)


def test_the_streamed_session_runs_without_the_agent_or_a_forge_token(
    provider: GcpProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2550 finding 1, for the session that carries the model credential."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "kew-fixture-forge-credential")
    monkeypatch.setattr(remote_dispatch.GcpProvider, "ssh_argv", lambda _s, c: [c])
    handed: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        handed.update(kwargs)
        raise OSError("no session in this test")

    monkeypatch.setattr(remote_dispatch.subprocess_utils, "popen", popen)
    with pytest.raises(OSError):
        remote_dispatch._ssh_session(provider, "true")

    assert "SSH_AUTH_SOCK" not in handed["env"]
    assert "GITHUB_TOKEN" not in handed["env"]


class TestTheInstanceHoldsItselfPastTheOperator:
    """#2627 — the hold lives on the instance; a failed idle stop is returned."""

    def test_the_lease_covers_the_container_deadline_before_it_launches(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """An operator killed mid-run leaves the instance leased for as long
        as its container may live, and the backstop power-off armed behind it."""
        _write_state(fake_gcloud, job_responses=_responses())

        _place(provider, config, checkout, tmp_path / "work", timeout_seconds=600)

        commands = _commands(fake_gcloud)
        launched = commands.index(_agent_launch(fake_gcloud))
        seconds = ceil(remote_deadline(600))
        held = [
            text
            for text in commands[:launched]
            if f"$((now + {seconds}))" in text and DEADLINE_STOP_UNIT in text
        ]
        assert len(held) == 1

    def test_the_last_lease_before_the_launch_is_the_container_deadline(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """No job between the hold and the launch shrinks the lease back to its
        own short ceiling — the pre-launch sweep is one (#2687 review)."""
        _write_state(fake_gcloud, job_responses=_responses())

        _place(provider, config, checkout, tmp_path / "work", timeout_seconds=3600)

        commands = _commands(fake_gcloud)
        launched = commands.index(_agent_launch(fake_gcloud))
        holds = [
            text
            for text in commands[:launched]
            if DEADLINE_STOP_UNIT in text and "$((now + " in text
        ]
        assert f"$((now + {ceil(remote_deadline(3600))}))" in holds[-1]

    def test_an_idle_stop_that_could_not_be_armed_is_on_the_result(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses(), release_exit=1)

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.ok, "the run's verdict stands"
        assert "may keep running and billing" in result.release_error
        assert "agent-1" in result.release_error

    def test_an_armed_idle_stop_leaves_no_release_error(
        self,
        provider: GcpProvider,
        config: DispatchConfig,
        checkout: Path,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        _write_state(fake_gcloud, job_responses=_responses())

        result = _place(provider, config, checkout, tmp_path / "work")

        assert result.release_error == ""
        assert [text for text in _commands(fake_gcloud) if _ARMS_IDLE_STOP in text]
