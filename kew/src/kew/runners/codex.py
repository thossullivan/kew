"""Official OpenAI Codex CLI runner."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runner_fields import (
    needs_human_reason,
    reported_identity,
    reported_model_identity,
    runner_text,
    service_tier_token,
)
from kew.runners.protocol import (
    ContainmentPosture,
    LoopStyle,
    PublicationStyle,
    ResultClassification,
    StreamEvent,
)
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.tracking import DispatchResult
from kew.work_dirs import make_work_dir

_RESUME_PROMPT = (
    "The previous run was interrupted by a transient failure. "
    "Continue where you left off after checking existing file changes and test output."
)
_TRANSIENT_ERRORS = (
    "rate limit",
    "network",
    "timeout",
    "timed out",
    "overloaded",
    "temporarily unavailable",
    "connection reset",
)
_PERMANENT_ERRORS = (
    "authentication",
    "unauthorized",
    "config",
    "model not found",
    "permission denied",
    "git ",
    "sandbox",
)
CODEX_CREDENTIAL_ENV_NAMES: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    }
)
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_ACCESS_TOKEN", "_AUTH_TOKEN")
MIN_CODEX_VERSION = (0, 145, 0)
_CODEX_VERSION_RE = re.compile(r"codex-cli (\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")
_SWITCHYARD_PROVIDER = "kew_switchyard"


class CodexProjectConfigError(ValueError):
    """Project .codex content is outside the governed v1 policy."""


def parse_codex_version(output: str) -> tuple[int, int, int] | None:
    """Parse the numeric core of official ``codex --version`` output."""
    match = _CODEX_VERSION_RE.fullmatch(output.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _project_policy_error(relative: Path, reason: str) -> CodexProjectConfigError:
    return CodexProjectConfigError(f"{relative.as_posix()}: {reason}")


def _scan_directory(directory: Path, relative: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError as exc:
        raise _project_policy_error(relative, "cannot inspect safely") from exc


def _validate_allowed_tree(directory: Path, relative: Path) -> None:
    for entry in _scan_directory(directory, relative):
        child_relative = relative / entry.name
        try:
            if entry.is_symlink():
                raise _project_policy_error(child_relative, "symlink is forbidden")
            if entry.is_dir(follow_symlinks=False):
                _validate_allowed_tree(Path(entry.path), child_relative)
            elif not entry.is_file(follow_symlinks=False):
                raise _project_policy_error(child_relative, "special file is forbidden")
        except OSError as exc:
            raise _project_policy_error(
                child_relative, "cannot inspect safely"
            ) from exc


def _find_codex_entry(worktree_dir: Path) -> os.DirEntry[str] | None:
    for entry in _scan_directory(worktree_dir, Path(".")):
        if entry.name == ".codex":
            return entry
    return None


def _require_real_directory(entry: os.DirEntry[str], relative: Path) -> None:
    try:
        if entry.is_symlink():
            raise _project_policy_error(relative, "symlink is forbidden")
        if not entry.is_dir(follow_symlinks=False):
            raise _project_policy_error(relative, "must be a directory")
    except OSError as exc:
        raise _project_policy_error(relative, "cannot inspect safely") from exc


def validate_codex_project_tree(worktree_dir: Path) -> None:
    """Accept only regular content beneath project `.codex/rules` and `skills`."""
    codex_entry = _find_codex_entry(worktree_dir)
    if codex_entry is None:
        return
    _require_real_directory(codex_entry, Path(".codex"))
    codex_dir = Path(codex_entry.path)
    for entry in _scan_directory(codex_dir, Path(".codex")):
        relative = Path(".codex") / entry.name
        if entry.name not in {"rules", "skills"}:
            raise _project_policy_error(relative, "entry is not allowed")
        _require_real_directory(entry, relative)
        _validate_allowed_tree(Path(entry.path), relative)


def codex_process_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment while removing provider and cloud credentials."""
    environment = os.environ if source is None else source
    return {
        key: value
        for key, value in environment.items()
        if key not in CODEX_CREDENTIAL_ENV_NAMES
        and not key.endswith(_CREDENTIAL_SUFFIXES)
    }


def _bounded_text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    text = runner_text(value).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _structured_response_metadata(payload: dict[str, object]) -> dict[str, object]:
    """Read only structured runner/provider identity fields.

    Agent messages and command output are intentionally not inspected here.
    Codex's response envelope has appeared both at the event root and beneath
    a ``response`` object, so accept those two bounded locations only.
    """
    candidates: list[dict[str, object]] = []
    response = payload.get("response")
    if isinstance(response, dict):
        # The nested provider response is more specific than an event-level
        # model echo, which may be the admitted transport alias.
        candidates.append(response)
    candidates.append(payload)
    metadata: dict[str, object] = {}
    for candidate in candidates:
        model = reported_model_identity(candidate.get("model"))
        if model and "model" not in metadata:
            metadata["model"] = model
        service_tier = service_tier_token(
            candidate.get("service_tier") or candidate.get("serviceTier")
        )
        if service_tier and "service_tier" not in metadata:
            metadata["service_tier"] = service_tier
    return metadata


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _is_transient_error(error: str) -> bool:
    return any(marker in error for marker in _TRANSIENT_ERRORS) or bool(
        re.search(r"\b5\d\d\b", error)
    )


def _is_permanent_error(error: str) -> bool:
    return any(marker in error for marker in _PERMANENT_ERRORS)


def _error_kind(error: str) -> str:
    """Classify original text; retain only a fixed kind for later exit mapping."""
    lowered = error.lower()
    if _is_permanent_error(lowered):
        return "permanent"
    return "transient" if _is_transient_error(lowered) else ""


def _nonzero_result_category(
    exit_code: int, error_kind: str, config: DispatchConfig
) -> str:
    if error_kind == "permanent":
        return "permanent"
    if error_kind == "transient" or exit_code in config.retry.retriable_exit_codes:
        return "transient"
    return "permanent"


def _result_category(
    exit_code: int,
    *,
    turn_completed: bool,
    error_kind: str,
    config: DispatchConfig,
) -> str:
    if exit_code == 0:
        return "" if turn_completed else "permanent"
    if exit_code == 124:
        return "transient"
    if exit_code in (137, 143):
        return "watchdog_kill"
    return _nonzero_result_category(exit_code, error_kind, config)


class CodexRunner:
    transport_capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )

    def __init__(self) -> None:
        self._thread_id: str | None = None
        self._turn_completed = False
        self._last_error = ""
        self._last_error_kind = ""
        self._final_message = ""
        self._needs_human = False
        self._human_reason = ""
        self._stop_reason = ""
        self._temp_dir: Path | None = None

    @property
    def turn_completed(self) -> bool:
        """Whether the current parsed stream emitted ``turn.completed``."""
        return self._turn_completed

    @property
    def name(self) -> str:
        return "codex"

    @property
    def loop_style(self) -> LoopStyle:
        return "native"

    @property
    def publication_style(self) -> PublicationStyle:
        return "host"

    def containment_posture(
        self, *, runtime: str | None = None
    ) -> ContainmentPosture | None:
        """Codex's own seatbelt — except where kew has a stronger boundary.

        Two answers, because there are two different questions (epic
        ``openshell-runtime``, node N10):

        ``"native"`` with no runtime, and on ``docker``/``podman``. Codex's own
        ``workspace-write`` sandbox is the only boundary available on those
        lanes: ``docker/agent-codex.Dockerfile`` is OpenShell-only by
        construction (its launcher reads the first container arg as the startup
        nonce and refuses under ``docker run``), so claiming anything else
        would make Codex undispatchable rather than contained.

        ``None`` on ``openshell``, which is the whole point of this node. There
        kew owns a real container boundary for this runner — the agent image,
        the packaged egress policy (``docker/openshell/policy-codex.yaml``) and
        a gateway launch path — and the credential never enters the sandbox at
        all. Keeping the ``"native"`` claim there would short-circuit
        :func:`kew.sandbox.resolve_effective_sandbox` into the host path and
        silently decline the boundary the operator asked for by naming the
        runtime. Codex still applies its own sandbox INSIDE that container, so
        this is not a posture downgrade: it is the outer boundary being
        reported as the one kew holds.
        """
        return None if runtime == OPENSHELL_RUNTIME else "native"

    @property
    def uncontainable_reason(self) -> str | None:
        return None

    @property
    def required_credentials(self) -> tuple[str, ...]:
        # Host auth stays in the official CLI keychain/cache. No file is mounted.
        return ()

    def _reset_attempt(self) -> None:
        self._thread_id = None
        self._turn_completed = False
        self._last_error = ""
        self._last_error_kind = ""
        self._final_message = ""
        self._needs_human = False
        self._human_reason = ""
        self._stop_reason = ""

    def _own_temp_dir(self, env: dict[str, str]) -> tuple[str, ...]:
        """Give a host run a private ``TMPDIR`` instead of the shared ones (#2023).

        Codex's workspace-write sandbox otherwise adds ``$TMPDIR`` and ``/tmp``
        to its writable roots, which every other native run on the host shares.
        The previous attempt's directory is released first: its process is gone.
        """
        self._release_temp_dir()
        self._temp_dir = make_work_dir("kew-codex-tmp-")
        env["TMPDIR"] = str(self._temp_dir)
        return (
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            "sandbox_workspace_write.writable_roots="
            + json.dumps([str(self._temp_dir)]),
        )

    def _release_temp_dir(self) -> None:
        temp_dir, self._temp_dir = self._temp_dir, None
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _require_model(model: str) -> str:
        """Every Codex launch names a model, host or contained.

        Checked in :meth:`_root_args`, which is the one place all three argv
        builders pass through — ``build_command``, ``build_resume_command`` and
        ``container_args``. It used to sit in the two host builders only, so a
        contained launch with an unrouted model produced an argv with no
        ``--model`` in it and Codex silently ran its own default: billed and
        attested as the routed model, executed as something else. The OpenShell
        launcher refuses such an argv too
        (:func:`kew.openshell_launch.require_model_flag`), but that is a
        backstop at the gateway seam, not the runner telling the truth about
        what it can build.
        """
        if not model:
            raise ValueError("codex runner requires an explicit model")
        return model

    def _root_args(
        self, config: DispatchConfig, model: str, sandbox: tuple[str, ...] = ()
    ) -> list[str]:
        """``sandbox`` follows the empty writable-roots override, so a host
        argv still carries every container flag in order and its own
        ``writable_roots`` override, applied later, is the one Codex keeps."""
        args = [
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.writable_roots=[]",
            *sandbox,
            "-c",
            "web_search=disabled",
            "-c",
            "features.apps=false",
            "-c",
            "features.hooks=false",
            "-c",
            "features.remote_plugin=false",
            "-c",
            "features.network_proxy=false",
            "-c",
            "features.skill_mcp_dependency_install=false",
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
        ]
        args.extend(["--model", self._require_model(model)])
        if config.inference_transport.mode == "switchyard":
            endpoint = config.inference_transport.switchyard.endpoint
            responses_base_url = f"{endpoint}/v1"
            args.extend(
                [
                    "-c",
                    f'model_provider="{_SWITCHYARD_PROVIDER}"',
                    "-c",
                    f'model_providers.{_SWITCHYARD_PROVIDER}.name="Kew Switchyard"',
                    "-c",
                    f"model_providers.{_SWITCHYARD_PROVIDER}.base_url="
                    f"{json.dumps(responses_base_url)}",
                    "-c",
                    f'model_providers.{_SWITCHYARD_PROVIDER}.wire_api="responses"',
                    "-c",
                    f"model_providers.{_SWITCHYARD_PROVIDER}.requires_openai_auth=true",
                ]
            )
        effort = config.agent.codex.reasoning_effort
        if effort:
            args.extend(["-c", f"model_reasoning_effort={effort}"])
        if config.agent.codex.service_tier == "fast":
            args.extend(["-c", 'service_tier="fast"', "-c", "features.fast_mode=true"])
        return args

    def container_args(self, *, config: DispatchConfig, model: str = "") -> list[str]:
        """The host argv minus ``codex`` and minus the prompt — ONE ``exec``.

        The root flags come first and the ``exec`` subcommand once, exactly as
        :meth:`build_command` orders them. The image contributes no second
        ``exec``: ``docker/agent-codex.Dockerfile`` installs
        ``docker/kew-openshell-launch`` and no agent entrypoint of its own, and
        that launcher adds no flags for ``codex`` — the deleted reference
        entrypoint ran ``codex exec "$@"``, which combined with this list into
        ``codex exec ... exec --json``. ``tests/test_runner_container_args.py``
        pins the count, because the duplicate is invisible until a real launch.
        """
        return [
            *self._root_args(config, model),
            "exec",
            "--json",
            "--ignore-user-config",
        ]

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        # Built BEFORE the reset: an unroutable model refuses without having
        # cleared the previous attempt's thread id and error state.
        self._require_model(model)
        root = self._root_args(config, model, self._own_temp_dir(env))
        self._reset_attempt()
        return [
            "codex",
            *root,
            "exec",
            "--json",
            "--ignore-user-config",
            prompt,
        ]

    def parse_line(self, line: str) -> list[StreamEvent]:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []

        event_type = payload.get("type")
        if event_type == "thread.started":
            return self._parse_thread_started(line, payload)
        if event_type == "item.completed":
            return self._parse_completed_item(line, payload)
        if event_type == "turn.completed":
            return self._parse_turn_completed(line, payload)
        if event_type == "turn.failed":
            return self._parse_turn_failed(line, payload)
        return []

    def _parse_thread_started(
        self, raw_line: str, payload: dict[str, object]
    ) -> list[StreamEvent]:
        thread_id = reported_identity(
            _bounded_text(payload.get("thread_id"), 500), "session_id"
        )
        if not thread_id:
            return []
        self._thread_id = thread_id
        metadata = {"session_id": thread_id, **_structured_response_metadata(payload)}
        return [
            StreamEvent(
                raw_line=raw_line,
                event_type="init",
                metadata=metadata,
            )
        ]

    def _parse_completed_item(
        self, raw_line: str, payload: dict[str, object]
    ) -> list[StreamEvent]:
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if item_type == "agent_message":
            return self._parse_agent_message(raw_line, item.get("text"))
        if item_type == "command_execution":
            command = _bounded_text(item.get("command"))
            if item.get("status") != "completed" or not command:
                return []
            return [
                StreamEvent(
                    raw_line=raw_line,
                    event_type="tool_use",
                    message="command_execution",
                    tool_uses=1,
                    metadata={"target": command},
                )
            ]
        return []

    def _parse_agent_message(self, raw_line: str, value: object) -> list[StreamEvent]:
        if not isinstance(value, str) or not value.strip():
            return []
        original = value.strip()
        reason = needs_human_reason(original)
        self._needs_human = reason is not None
        self._human_reason = reason or ""
        self._final_message = _bounded_text(value, 4096)
        return [
            StreamEvent(
                raw_line=raw_line,
                event_type="activity",
                message=_bounded_text(value),
            )
        ]

    def _parse_turn_completed(
        self, raw_line: str, payload: dict[str, object]
    ) -> list[StreamEvent]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return []
        total_input = _nonnegative_int(usage.get("input_tokens"))
        cached_input = min(
            _nonnegative_int(usage.get("cached_input_tokens")), total_input
        )
        output = _nonnegative_int(usage.get("output_tokens"))
        self._turn_completed = True
        self._last_error = ""
        self._last_error_kind = ""
        self._stop_reason = "completed"
        uncached_input = total_input - cached_input
        identity = _structured_response_metadata(payload)
        return [
            StreamEvent(
                raw_line=raw_line,
                event_type="usage",
                input_tokens=uncached_input,
                output_tokens=output,
                cache_read_tokens=cached_input,
                metadata=identity,
            ),
            StreamEvent(
                raw_line=raw_line,
                event_type="step",
                input_tokens=uncached_input,
                output_tokens=output,
                cache_read_tokens=cached_input,
                metadata={"cost_usd": None, **identity},
            ),
            StreamEvent(
                raw_line=raw_line,
                event_type="result",
                metadata={
                    "session_id": self._thread_id,
                    "stop_reason": "completed",
                    "cost_usd": None,
                    **identity,
                },
            ),
        ]

    def _parse_turn_failed(
        self, raw_line: str, payload: dict[str, object]
    ) -> list[StreamEvent]:
        error = payload.get("error")
        if not isinstance(error, dict):
            return []
        message = error.get("message")
        if not isinstance(message, str) or not message.strip():
            return []
        self._turn_completed = False
        self._last_error_kind = _error_kind(message)
        self._last_error = _bounded_text(message, 1000)
        self._stop_reason = "failed"
        return [
            StreamEvent(
                raw_line=raw_line,
                event_type="activity",
                message=_bounded_text(message),
            )
        ]

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification:
        if self._needs_human:
            return ResultClassification(
                category="needs_human",
                session_id=self._thread_id,
                subtype=self._human_reason or "unspecified",
                stop_reason=self._stop_reason or "incomplete",
            )
        return ResultClassification(
            category=_result_category(
                exit_code,
                turn_completed=self._turn_completed,
                error_kind=self._last_error_kind,
                config=config,
            ),
            session_id=self._thread_id,
            stop_reason=self._stop_reason or "incomplete",
        )

    def supports_resume(self) -> bool:
        return True

    def build_resume_command(
        self,
        *,
        session_id: str,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        self._require_model(model)
        root = self._root_args(config, model, self._own_temp_dir(env))
        self._reset_attempt()
        self._thread_id = session_id
        return [
            "codex",
            *root,
            "exec",
            "resume",
            "--json",
            "--ignore-user-config",
            session_id,
            _RESUME_PROMPT,
        ]

    def enrich_result(self, result: DispatchResult, config: DispatchConfig) -> None:
        self._release_temp_dir()
