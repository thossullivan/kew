"""W1.1 node 3d-3: ``run_agent`` can execute inside a container (#1462).

The whole point of putting containment *here* is that a contained run inherits
the host lifecycle instead of getting a second one — so most of these tests
assert that something the host path already enforces still reaches an agent
running in a container.

Built before it has production callers, same as the seam in #1461: wiring review
and resolve onto it is then a wiring change against a tested surface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import MockSelector

from kew.config import DispatchConfig
from kew.orchestrator import run_agent
from kew.runners.protocol import ResultClassification, StreamEvent
from kew.sandbox import (
    PROMPT_FILENAME,
    ContainedExecution,
    build_run_argv,
    remove_prompt_handoff,
    write_prompt_handoff,
)

USAGE = StreamEvent(
    raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
)


def _cpu_cap_rejection(maximum: int) -> str:
    return (
        "docker: Error response from daemon: Range of CPUs is from "
        f"0.01 to {maximum}.00, as there are only {maximum} CPUs available.\n"
    )


CPU_CAP_REJECTION = _cpu_cap_rejection(2)


class _Runner:
    """Minimal AgentRunner double.

    ``build_command`` records that it ran: a contained run must never call it,
    because the argv is the container's and re-deriving one here is how a
    contained run would quietly execute the host command instead.
    """

    name = "claude-code"

    required_credentials = ()

    def __init__(self, events: list[StreamEvent] | None = None) -> None:
        self.build_command_called = False
        self._events = list(events or [])

    def build_command(self, *, prompt, config, env, model="", **kw):
        self.build_command_called = True
        return ["echo", prompt]

    def parse_line(self, line):
        return [self._events.pop(0)] if self._events else []

    def classify_result(self, *, exit_code, last_output, config):
        return ResultClassification(category="permanent")

    def supports_resume(self):
        return False

    def build_resume_command(self, **kw):
        raise NotImplementedError

    def enrich_result(self, result, config):
        pass


def _contained(tmp_path: Path, *, slug: str = "pr-42") -> ContainedExecution:
    """A real argv from the real builder — not a hand-written stand-in.

    A fabricated argv would let ``--name`` drift from what
    :func:`build_run_argv` actually emits, and the container a watchdog kills is
    identified by exactly that.
    """
    return ContainedExecution.from_argv(
        build_run_argv(
            runtime="docker",
            image="ghcr.io/example/agent:latest",
            checkout_dir=tmp_path,
            slug=slug,
            network="open",
            runner="claude-code",
        )
    )


def _proc(*, exit_code: int = 0, lines: list[str] | None = None) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.stdout = MagicMock()
    proc.returncode = exit_code
    proc.poll.return_value = exit_code
    proc.wait.return_value = exit_code
    proc.stdout.readline.side_effect = [*(lines or []), ""]
    return proc


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    return config


@pytest.fixture(autouse=True)
def _fake_container_runtime():
    """A contained launch may call the runtime (a pre-launch sweep of a
    recorded id, a kill) through the executable allowlist; no test here has a
    real runtime.
    A test that installs its own spy on `kew.sandbox.subprocess_utils.run`
    shadows this fake for its duration."""
    with patch("kew.subprocess_utils.run", return_value=MagicMock(returncode=0)):
        yield


def _run(tmp_path: Path, proc: MagicMock, **kwargs):
    """Drive run_agent with the subprocess layer and selector faked out."""
    runner = kwargs.pop("runner", None) or _Runner()
    with (
        patch("kew.orchestrator.popen", return_value=proc) as popen,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=MockSelector()
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="address the review comments",
            worktree_dir=tmp_path,
            config=_config(tmp_path),
            issue_num=42,
            model="claude-opus-5",
            **kwargs,
        )
    return result, popen, runner


class TestTheContainerIsWhatRuns:
    def test_the_launched_argv_is_the_containers(self, tmp_path: Path):
        contained = _contained(tmp_path)
        _, popen, runner = _run(tmp_path, _proc(), contained=contained)

        assert popen.call_args.args[0] == list(contained.argv)
        assert not runner.build_command_called

    def test_the_host_path_is_untouched(self, tmp_path: Path):
        """No `contained` means the runner still builds the command."""
        _, popen, runner = _run(tmp_path, _proc())

        assert runner.build_command_called
        assert popen.call_args.args[0] == ["echo", "address the review comments"]

    def test_attempted_cap_above_advertised_maximum_retries_contained_launch(
        self, tmp_path: Path
    ):
        contained = _contained(tmp_path)
        rejected = _proc(
            exit_code=125,
            lines=[CPU_CAP_REJECTION],
        )
        accepted = _proc(exit_code=0)
        runtime_answer = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("kew.orchestrator.popen", side_effect=[rejected, accepted]) as popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
            patch(
                "kew.sandbox.subprocess_utils.run", return_value=runtime_answer
            ) as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=2) as reprobe,
        ):
            result = run_agent(
                runner=_Runner(),
                prompt="address the review comments",
                worktree_dir=tmp_path,
                config=_config(tmp_path),
                issue_num=42,
                model="claude-opus-5",
                contained=contained,
            )

        assert result.exit_code == 0
        assert popen.call_count == 2
        initial_argv = popen.call_args_list[0].args[0]
        assert initial_argv[initial_argv.index("--cpus") + 1] == "4"
        retry_argv = popen.call_args_list[1].args[0]
        assert retry_argv[retry_argv.index("--cpus") + 1] == "1"
        reprobe.assert_called_once_with("docker")
        # #1946: a CPU-cap retry is permitted only when this launch recorded no
        # container id; the runtime is never asked about the NAME (another
        # launch may own it, and a retry under a taken name fails closed on the
        # runtime's own conflict refusal). #1963: nor is the name removed
        # before the launch — with no id recorded, nothing is sent at all.
        assert [call.args[0] for call in run.call_args_list] == []

    def test_attempted_cap_at_advertised_maximum_does_not_retry_contained_launch(
        self, tmp_path: Path
    ):
        argv = list(_contained(tmp_path).argv)
        argv[argv.index("--cpus") + 1] = "2"
        contained = ContainedExecution.from_argv(argv)
        rejected = _proc(exit_code=125, lines=[CPU_CAP_REJECTION])

        with (
            patch("kew.sandbox.subprocess_utils.run") as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            result, popen, _ = _run(tmp_path, rejected, contained=contained)

        assert result.exit_code == 125
        popen.assert_called_once()
        # No id recorded, so no pre-launch removal by name (#1963).
        assert run.call_count == 0
        reprobe.assert_not_called()

    @pytest.mark.parametrize(
        ("attempted", "maximum", "should_retry"),
        [
            ("1.5", 1, True),
            ("1.5", 2, False),
            ("2.0000000000000000000000000001", 2, True),
            ("not-a-number", 2, False),
            ("4", 1, True),
            ("1", 1, False),
        ],
        ids=[
            "fraction-above",
            "fraction-below",
            "decimal-epsilon-above",
            "nonnumeric",
            "four-above-one",
            "one-at-one",
        ],
    )
    def test_contained_retry_requires_a_numeric_cap_above_the_advertised_maximum(
        self,
        tmp_path: Path,
        attempted: str,
        maximum: int,
        should_retry: bool,
    ):
        argv = list(_contained(tmp_path).argv)
        argv[argv.index("--cpus") + 1] = attempted
        contained = ContainedExecution.from_argv(argv)
        rejected = _proc(
            exit_code=125,
            lines=[_cpu_cap_rejection(maximum)],
        )
        accepted = _proc(exit_code=0)
        runtime_answer = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "kew.orchestrator.popen",
                side_effect=[rejected, accepted],
            ) as popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
            patch(
                "kew.sandbox.subprocess_utils.run", return_value=runtime_answer
            ) as run,
            patch("kew.sandbox._daemon_cpu_count", return_value=maximum) as reprobe,
        ):
            result = run_agent(
                runner=_Runner(),
                prompt="address the review comments",
                worktree_dir=tmp_path,
                config=_config(tmp_path),
                issue_num=42,
                model="claude-opus-5",
                contained=contained,
            )

        assert result.exit_code == (0 if should_retry else 125)
        assert popen.call_count == (2 if should_retry else 1)
        # No runtime call either way: no name-based absence probe precedes a
        # retry (#1946: identity is the recorded container id only), and with
        # no id recorded nothing is removed before the launch (#1963).
        assert run.call_count == 0
        assert reprobe.call_count == (1 if should_retry else 0)
        if should_retry:
            retry_argv = popen.call_args_list[1].args[0]
            assert retry_argv[retry_argv.index("--cpus") + 1] == "1"

    @pytest.mark.parametrize(
        ("line", "exit_code"),
        [
            ("failure at /tmp/range of CPUs is from/cache\n", 125),
            (CPU_CAP_REJECTION, 1),
        ],
        ids=["bare-substring", "wrong-exit-status"],
    )
    def test_contained_launch_does_not_retry_other_refusals(
        self, tmp_path: Path, line: str, exit_code: int
    ):
        contained = _contained(tmp_path)
        rejected = _proc(exit_code=exit_code, lines=[line])
        no_container = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with (
            patch("kew.sandbox.subprocess_utils.run", return_value=no_container) as run,
            patch("kew.sandbox._daemon_cpu_count") as reprobe,
        ):
            result, popen, _ = _run(tmp_path, rejected, contained=contained)

        assert result.exit_code == exit_code
        popen.assert_called_once()
        assert run.call_count == 0
        reprobe.assert_not_called()

    def test_both_real_handle_constructors_keep_argv_identity(self, tmp_path: Path):
        """Both launch paths must keep the same name and immutable cidfile.

        ``launch_container`` and ``ContainedExecution.handle_for`` are the two
        real handle constructors. Both must parse the builder's cidfile.
        """
        argv = build_run_argv(
            runtime="podman",
            image="img",
            checkout_dir=tmp_path,
            slug="sfd-12",
            network="open",
            runner="claude-code",
        )
        with patch("kew.sandbox.subprocess_utils.popen", return_value=_proc()):
            from kew.sandbox import launch_container

            handle = launch_container(list(argv))

        contained = ContainedExecution.from_argv(argv)
        contained_handle = contained.handle_for(_proc())
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        assert (contained.runtime, contained.name) == (handle._runtime, handle._name)
        assert contained.name == "kew-sfd-12"
        assert handle._cidfile == cidfile
        assert contained_handle._cidfile == cidfile


class TestTheWatchdogsReachIntoTheContainer:
    """#1462: killing the local `docker run` client leaves the container up."""

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("budget", {"budget_check": lambda cost: cost > 0.0}),
            ("breaker", {"anomaly_threshold_usd": 0.0000001}),
            ("pause", {"should_pause": lambda: True}),
        ],
    )
    def test_a_kill_goes_to_the_container_by_its_recorded_id(
        self, tmp_path, label, kwargs
    ):
        proc = _proc(lines=["line1\n"])
        proc.poll.return_value = None  # still running when the watchdog trips
        contained = _contained(tmp_path)
        argv = list(contained.argv)
        cidfile = Path(argv[argv.index("--cidfile") + 1])
        container_id = "c" * 64

        def _docker_run(*_args, **_kwargs):
            # `docker run --cidfile` records the container's id as it creates it.
            cidfile.parent.mkdir(parents=True, exist_ok=True)
            cidfile.write_text(container_id)
            return proc

        with (
            patch("kew.sandbox.subprocess_utils.run") as sandbox_run,
            patch("kew.orchestrator.os.killpg") as killpg,
            patch("kew.orchestrator.popen", side_effect=_docker_run),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
        ):
            run_agent(
                runner=_Runner(events=[USAGE]),
                prompt="address the review comments",
                worktree_dir=tmp_path,
                config=_config(tmp_path),
                issue_num=42,
                model="claude-opus-5",
                contained=contained,
                **kwargs,
            )

        # No pre-launch removal by name (#1963); the kill, then the post-stop
        # removal once the client is gone, both name the recorded id only.
        assert [call.args[0] for call in sandbox_run.call_args_list] == [
            ["docker", "kill", container_id],
            ["docker", "rm", "-f", container_id],
        ], label
        # The client process group is NOT what holds the agent.
        killpg.assert_not_called()

    def test_a_host_run_still_kills_its_process_group(self, tmp_path: Path):
        """The container path must not have taken the host path's kill away."""
        proc = _proc(lines=["line1\n"])
        proc.poll.return_value = None

        with (
            patch("kew.sandbox.subprocess_utils.run") as sandbox_run,
            # Without this the mock's pid has no real process group, getpgid
            # raises, and the fallback branch runs — which would let this test
            # pass while asserting nothing about the group kill.
            patch("kew.orchestrator.os.getpgid", return_value=proc.pid),
            patch("kew.orchestrator.os.killpg") as killpg,
        ):
            _run(
                tmp_path,
                proc,
                runner=_Runner(events=[USAGE]),
                budget_check=lambda cost: cost > 0.0,
            )

        killpg.assert_called()
        assert not any(
            call.args and call.args[0][:2] == ["docker", "kill"]
            for call in sandbox_run.call_args_list
        )


class TestThePromptHandoff:
    """#695 in, #725 out."""

    def test_the_prompt_is_a_mounted_file_and_never_argv(self, tmp_path: Path):
        contained = _contained(tmp_path)
        seen: dict[str, str] = {}

        def _capture(argv, **kw):
            seen["prompt"] = (tmp_path / PROMPT_FILENAME).read_text(encoding="utf-8")
            return _proc()

        with (
            patch("kew.orchestrator.popen", side_effect=_capture),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
        ):
            run_agent(
                runner=_Runner(),
                prompt="address the review comments",
                worktree_dir=tmp_path,
                config=_config(tmp_path),
                issue_num=42,
                model="claude-opus-5",
                contained=contained,
            )

        assert seen["prompt"] == "address the review comments"
        assert "address the review comments" not in " ".join(contained.argv)

    def test_the_file_does_not_survive_the_run(self, tmp_path: Path):
        """The caller publishes this checkout next — a leftover lands in the PR."""
        _run(tmp_path, _proc(), contained=_contained(tmp_path))

        assert not (tmp_path / PROMPT_FILENAME).exists()

    def test_the_file_does_not_survive_a_failed_launch(self, tmp_path: Path):
        with (
            patch("kew.orchestrator.popen", side_effect=OSError("no runtime")),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            pytest.raises(OSError, match="no runtime"),
        ):
            run_agent(
                runner=_Runner(),
                prompt="p",
                worktree_dir=tmp_path,
                config=_config(tmp_path),
                issue_num=42,
                contained=_contained(tmp_path),
            )

        assert not (tmp_path / PROMPT_FILENAME).exists()

    def test_a_host_run_writes_no_handoff_file(self, tmp_path: Path):
        _run(tmp_path, _proc())

        assert not (tmp_path / PROMPT_FILENAME).exists()


class TestTheHandoffCannotBeRedirected:
    """A planted symlink must not turn the handoff into a host write primitive.

    Every caller writes into a directory an agent can modify. The prompt is not
    inert either — a review prompt embeds PR comment bodies, which anyone with
    access to the pull request can author.
    """

    def test_the_write_does_not_follow_a_planted_symlink(self, tmp_path: Path):
        victim = tmp_path / "victim"
        victim.write_text("operator content", encoding="utf-8")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / PROMPT_FILENAME).symlink_to(victim)

        write_prompt_handoff(checkout, "PROMPT")

        assert victim.read_text(encoding="utf-8") == "operator content"
        assert not (checkout / PROMPT_FILENAME).is_symlink()
        assert (checkout / PROMPT_FILENAME).read_text(encoding="utf-8") == "PROMPT"

    def test_the_removal_does_not_delete_a_symlink_target(self, tmp_path: Path):
        victim = tmp_path / "victim"
        victim.write_text("operator content", encoding="utf-8")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / PROMPT_FILENAME).symlink_to(victim)

        remove_prompt_handoff(checkout)

        assert victim.read_text(encoding="utf-8") == "operator content"

    def test_a_contained_run_does_not_follow_it_either(self, tmp_path: Path):
        """The end-to-end path, not just the helper in isolation."""
        victim = tmp_path / "victim"
        victim.write_text("operator content", encoding="utf-8")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / PROMPT_FILENAME).symlink_to(victim)

        with (
            patch("kew.orchestrator.popen", return_value=_proc()),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
        ):
            run_agent(
                runner=_Runner(),
                prompt="address the review comments",
                worktree_dir=checkout,
                config=_config(tmp_path),
                issue_num=42,
                contained=_contained(checkout),
            )

        assert victim.read_text(encoding="utf-8") == "operator content"

    @pytest.mark.policy
    def test_no_flow_writes_the_handoff_path_itself(self):
        """One safe site per flow is how an unsafe fifth gets added unnoticed.

        ``dispatch_core`` is allowed the NAME (it writes it into
        ``.git/info/exclude``, never a file at that path); ``sandbox`` owns the
        helpers. ``hosts/remote_dispatch`` is the one flow that cannot use
        them: its write lands on ANOTHER machine, over a shell, because a git
        bundle carries committed objects and this file is deliberately
        untracked (#2397). It carries the same rule in shell — ``rm -f`` then
        ``set -C`` for ``write_prompt_handoff``'s unlink-then-``O_EXCL`` — and
        :func:`test_the_remote_prelude_carries_the_same_unlink_then_o_excl_rule`
        holds it to that. Anything ELSE reaching for the constant is building
        its own write, which is the bug this closes.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "kew"
        referencing = {
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            if "PROMPT_FILENAME" in path.read_text(encoding="utf-8")
        }

        assert referencing == {
            "sandbox.py",
            "dispatch_core.py",
            "hosts/remote_dispatch.py",
        }

    def test_the_remote_prelude_carries_the_same_unlink_then_o_excl_rule(self):
        """The remote flow's own write, held to the local primitive's rule.

        A plain ``>`` follows a symlink, and the prompt is written into a
        directory an agent can modify. So the prelude unlinks first (which
        removes a link and never its target) and writes under ``set -C``,
        whose ``O_EXCL`` refuses a link planted in the gap.
        """
        from kew.hosts.remote_dispatch import RemoteSite, prompt_prelude

        prelude = prompt_prelude(
            RemoteSite(checkout="/home/runner/kew-host/checkout", uid=1, gid=1)
        )
        target = f"/home/runner/kew-host/checkout/{PROMPT_FILENAME}"

        assert f"rm -f {target} &&" in prelude
        assert "set -C" in prelude
        assert prelude.index("rm -f") < prelude.index(f"> {target}")


class TestTheSharedLifecycleStillApplies:
    def test_a_container_gets_the_same_wall_clock_as_a_host_run(self, tmp_path: Path):
        """An unbounded contained run is the gap this seam exists to close."""
        contained = _contained(tmp_path)
        config = _config(tmp_path)

        with (
            patch("kew.orchestrator.popen", return_value=_proc()) as popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value="/usr/bin/timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=MockSelector(),
            ),
        ):
            run_agent(
                runner=_Runner(),
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                contained=contained,
                timeout_override_minutes=7,
            )

        assert popen.call_args.args[0][:5] == [
            "/usr/bin/timeout",
            "--foreground",
            "-k",
            "30s",
            "7m",
        ]
        assert popen.call_args.args[0][5:] == list(contained.argv)

    def test_the_run_is_accounted_for_like_any_other(self, tmp_path: Path):
        """Same DispatchResult, so budget and reporting need no second path."""
        result, _, _ = _run(
            tmp_path,
            _proc(lines=["line1\n"]),
            runner=_Runner(events=[USAGE]),
            contained=_contained(tmp_path),
        )

        assert result.exit_code == 0
        assert result.input_tokens == 100
        assert result.output_tokens == 50


def test_round9_orchestrator_scrubs_ambient_key_only_for_containers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "round9-operator")
    _, host_launch, _ = _run(tmp_path, _proc())
    assert (
        host_launch.call_args.kwargs["env"]["OPENROUTER_API_KEY"] == "round9-operator"
    )
    _, container_launch, _ = _run(tmp_path, _proc(), contained=_contained(tmp_path))
    assert "OPENROUTER_API_KEY" not in container_launch.call_args.kwargs["env"]
