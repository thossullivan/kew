"""The container-runtime seam: one backend per runtime, or a named refusal.

``[sandbox] runtime`` used to be a free string read directly by launch,
monitoring and recovery, each carrying its own ``{"docker", "podman"}``
literal. These tests pin the two halves of the replacement: every supported
runtime resolves to exactly one backend, and a supported runtime with no
backend raises instead of being quietly treated as drivable.
"""

from unittest.mock import patch

import pytest

from kew.openshell import (
    OpenShellGatewayUnreachable,
    OpenShellNotFound,
    OpenShellTimeout,
    Sandbox,
)
from kew.runtime_backend import (
    BACKED_RUNTIMES,
    DOCKER_COMPATIBLE_RUNTIMES,
    SUPPORTED_RUNTIMES,
    DockerBackend,
    OpenShellBackend,
    RuntimeBackendUnavailable,
    backend_for,
)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_backend_for_returns_the_docker_backend_for_both_cli_runtimes(runtime: str):
    """Podman is a drop-in for every call in the backend, so one class serves both."""
    backend = backend_for(runtime)

    assert isinstance(backend, DockerBackend)
    assert backend.runtime == runtime


def test_backend_for_resolves_the_openshell_backend_now_that_it_lands():
    """The gateway runtime is drivable from a run row (epic node N8).

    It resolves to its OWN backend rather than the docker one: every call it
    answers is a gateway call, and sending ``docker inspect`` at a sandbox name
    would read a live run as unknown forever.
    """
    backend = backend_for("openshell")

    assert isinstance(backend, OpenShellBackend)
    assert backend.runtime == "openshell"


def test_the_refusal_is_the_valueerror_existing_callers_fail_closed_on():
    """Callers already answer "unknown" on ValueError; the seam reuses that."""
    assert issubclass(RuntimeBackendUnavailable, ValueError)


def test_an_unvalidated_runtime_name_is_refused_too():
    """Recorded runtimes predate validation, so backend_for cannot trust them."""
    with pytest.raises(RuntimeBackendUnavailable) as raised:
        backend_for("/usr/local/bin/docker")

    assert "docker, podman, openshell" in str(raised.value)


def test_every_supported_runtime_has_a_backend():
    """The lists are the contract: config validates one, this build serves one."""
    assert set(DOCKER_COMPATIBLE_RUNTIMES) <= set(BACKED_RUNTIMES)
    assert set(SUPPORTED_RUNTIMES) == set(BACKED_RUNTIMES)
    assert set(BACKED_RUNTIMES) - set(DOCKER_COMPATIBLE_RUNTIMES) == {"openshell"}


class TestDockerBackendWrapsTheExistingCalls:
    """The backend must not invent a second way to talk to docker."""

    _IDENTITY = "a" * 64

    def test_probe_resolves_the_runtime_binary(self):
        with patch("shutil.which", return_value="/usr/bin/podman") as which:
            assert backend_for("podman").probe() == "/usr/bin/podman"
        which.assert_called_once_with("podman")

    def test_probe_is_none_when_the_host_lacks_the_runtime(self):
        with patch("shutil.which", return_value=None):
            assert backend_for("docker").probe() is None

    def test_state_delegates_to_the_recorded_identity_inspection(self):
        with patch(
            "kew.stop_recovery.container_state", return_value="running"
        ) as inspect:
            assert backend_for("docker").state(self._IDENTITY, "kew-agent-1") == (
                "running"
            )
        inspect.assert_called_once_with("docker", self._IDENTITY, "kew-agent-1")

    def test_kill_signals_the_identity_through_the_subprocess_allowlist(self):
        with patch("kew.subprocess_utils.run") as run:
            backend_for("docker").kill(self._IDENTITY)
        run.assert_called_once_with(
            ["docker", "kill", self._IDENTITY], check=False, timeout=10
        )

    def test_cleanup_force_removes_the_identity(self):
        with patch("kew.subprocess_utils.run") as run:
            backend_for("podman").cleanup(self._IDENTITY)
        run.assert_called_once_with(
            ["podman", "container", "rm", "--force", self._IDENTITY],
            check=False,
            timeout=10,
        )


class TestOpenShellBackendMapsTheGatewayOntoTheFourStates:
    """``sandbox get`` is the only question; the mapping is where runs get reaped.

    Each case is a capacity decision. ``running`` and ``unknown`` hold a
    reservation; ``stopped`` and ``absent`` release one and let an orphan be
    finalized — so a status word this build has never seen must land on the
    holding side, and only a gateway that said "no such sandbox" may free it.
    """

    _NAME = "kew-run-7f3c1a"

    @staticmethod
    def _sandbox(name: str, status: str) -> Sandbox:
        return Sandbox(
            name=name,
            status=status,
            identity="sbx_01HN4Q8ZJ2WME5T",
            image="kew-agent:docker-6ed2f5d69acd",
            labels={},
            raw={},
        )

    def _state(self, answer, *, name: str | None = None) -> str:
        """Ask the backend with ``sandbox_get`` replaced by one answer."""
        target = self._NAME if name is None else name

        def sandbox_get(_client, asked: str) -> Sandbox:
            assert asked == target
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch("kew.openshell.OpenShellClient.sandbox_get", sandbox_get):
            return OpenShellBackend().state(target)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("running", "running"),
            ("Running", "running"),
            ("stopped", "stopped"),
            ("exited", "stopped"),
            ("deleted", "stopped"),
            ("terminated", "stopped"),
            ("failed", "stopped"),
            ("creating", "unknown"),
            ("starting", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_each_reported_status_maps_to_one_state(self, status: str, expected: str):
        assert self._state(self._sandbox(self._NAME, status)) == expected

    def test_a_gateway_that_says_no_such_sandbox_is_the_only_absence(self):
        gone = OpenShellNotFound(["openshell", "sandbox", "get"], reason="gone")

        assert self._state(gone) == "absent"

    @pytest.mark.parametrize(
        "failure",
        [
            OpenShellGatewayUnreachable(["openshell"], reason="unreachable"),
            OpenShellTimeout(["openshell"], reason="deadline"),
            ValueError("sandbox name cannot be blank"),
        ],
    )
    def test_a_gateway_that_could_not_answer_leaves_the_sandbox_unproven(
        self, failure: Exception
    ):
        """Unknown pins the reservation; absent would free a live agent's slot."""
        assert self._state(failure) == "unknown"

    def test_a_sandbox_answering_under_another_name_states_nothing(self):
        """The name is the identity here, so a reply about another one is no answer."""
        other = self._sandbox("kew-run-91bd20", "running")

        get = "kew.openshell.OpenShellClient.sandbox_get"
        with patch(get, lambda _client, _name: other):
            assert OpenShellBackend().state(self._NAME) == "unknown"

    def test_a_sandbox_with_another_gateway_id_states_nothing(self):
        """The name answers, but with an id this run never recorded (#2625).

        Two projects' runs once shared a name on one gateway; a state read
        through the name alone let one's recovery download and delete the
        other's sandbox. The recorded id is the evidence, so a mismatch is no
        answer — and a match, or a row with no id yet, is.
        """
        running = self._sandbox(self._NAME, "running")

        get = "kew.openshell.OpenShellClient.sandbox_get"
        with patch(get, lambda _client, _name: running):
            backend = OpenShellBackend()
            assert backend.state(self._NAME, "sbx_SOMEONEELSE") == "unknown"
            assert backend.state(self._NAME, "sbx_01HN4Q8ZJ2WME5T") == "running"
            assert backend.state(self._NAME) == "running"

    def test_a_blank_identity_is_never_sent_to_the_gateway(self):
        """A row with no recorded sandbox name has nothing to ask about."""

        def refuse(_self, _name):
            raise AssertionError("no gateway call may be made without a name")

        with patch("kew.openshell.OpenShellClient.sandbox_get", refuse):
            assert OpenShellBackend().state("  ") == "unknown"

    def test_probe_resolves_the_openshell_cli(self):
        with patch("shutil.which", return_value="/usr/local/bin/openshell") as which:
            assert backend_for("openshell").probe() == "/usr/local/bin/openshell"
        which.assert_called_once_with("openshell")

    def test_kill_and_cleanup_are_both_the_one_idempotent_delete(self):
        """Ending the workload and removing the container are one gateway call."""
        deleted: list[str] = []

        with patch(
            "kew.openshell.OpenShellClient.sandbox_delete",
            lambda _self, name: deleted.append(name),
        ):
            backend_for("openshell").kill(self._NAME)
            backend_for("openshell").cleanup(self._NAME)

        assert deleted == [self._NAME, self._NAME]

    @pytest.mark.parametrize(
        "failure",
        [
            OpenShellNotFound(["openshell"], reason="gone"),
            OpenShellGatewayUnreachable(["openshell"], reason="unreachable"),
        ],
    )
    def test_a_delete_that_failed_never_raises_at_the_caller(self, failure: Exception):
        """The recovery sweeps call this while finalizing a run; a raise would
        replace the outcome they were writing with a cleanup error."""

        def fail(_self, _name):
            raise failure

        with patch("kew.openshell.OpenShellClient.sandbox_delete", fail):
            assert backend_for("openshell").cleanup(self._NAME) is None

    def test_configured_settings_decide_the_gateway_that_is_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A caller holding the config must not have its endpoint replaced by
        the CLI default — an answer from another gateway is about another
        sandbox."""
        from kew.config import OpenShellConfig

        monkeypatch.delenv("OPENSHELL_GATEWAY", raising=False)
        settings = OpenShellConfig(gateway="https://gateway.internal:8443")
        asked: list[str] = []

        def sandbox_get(self, name: str) -> Sandbox:
            asked.append(self.gateway.endpoint)
            return TestOpenShellBackendMapsTheGatewayOntoTheFourStates._sandbox(
                name, "running"
            )

        with patch("kew.openshell.OpenShellClient.sandbox_get", sandbox_get):
            assert OpenShellBackend(settings).state(self._NAME) == "running"
            assert OpenShellBackend().state(self._NAME) == "running"

        assert asked == ["https://gateway.internal:8443", ""]
