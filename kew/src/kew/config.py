"""Configuration loading and validation for kew.

Reads kew.toml, applies environment variable
overrides, and validates the merged config with Pydantic.
"""

import ipaddress
import json
import logging
import math
import os
import re
import stat
import tomllib
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from kew import work_dirs
from kew.model_eol_snapshot import (
    LifecycleEntry,
    RegistryError,
)
from kew.model_eol_snapshot import (
    merge_snapshot_into_raw as merge_model_eol_snapshot_into_raw,
)
from kew.pricing import is_free_model
from kew.runtime_names import SUPPORTED_RUNTIMES
from kew.sandbox_caps import validated_max_cpus, validated_max_memory
from kew.triage.schema import TRIAGE_BACKENDS, TRIAGE_TYPES

if TYPE_CHECKING:
    from kew.run_store import RunStore


ConfigErrorKind = Literal[
    "malformed_toml",
    "invalid_value",
    "unknown_key",
    "unreadable",
    "not_a_file",
    "encoding",
    "provenance",
    "env_override",
    "snapshot",
    "unread_config_file",
]

# A bare pytest marker name — what `pytest -m <name>` accepts without quoting.
_PYTEST_MARK_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Keys that moved. An unknown key is a hard load failure (the models forbid
# extras), so naming the replacement turns a stalled kew into a one-line edit.
_RENAMED_KEYS = {
    "[commands].test_policy_paths": (
        "commands.test_policy_mark — the governed remediation's policy set is"
        " the pytest mark the gates carry, not a hand list of paths (#2174)"
    ),
    "[review].min_severity_to_block": (
        "review.stop_rule_max_severity — the severity rule kew enforces; the"
        " floor never blocked anything (#2017)"
    ),
}


class ConfigError(ValueError):
    """Structured configuration failure for operator-facing renderers."""

    def __init__(
        self,
        path: Path | str,
        key: str,
        expectation: str,
        fix: str,
        kind: ConfigErrorKind,
        related: tuple["ConfigError", ...] = (),
    ) -> None:
        self.path = path
        self.key = key
        self.expectation = expectation
        self.fix = fix
        self.kind = kind
        self.related = related
        super().__init__(self._render())

    def _render(self) -> str:
        """Render one failure in three short operator-facing lines."""
        return (
            f"{self.path}\nkey {self.key}: expected {self.expectation}\nfix: {self.fix}"
        )

    def __str__(self) -> str:
        """Render every validation failure without losing structured fields."""
        return "\n".join(error._render() for error in (self, *self.related))

    def to_dict(self) -> dict[str, object]:
        """Return the stable envelope for a future structured renderer."""
        return {
            "path": str(self.path),
            "key": self.key,
            "expectation": self.expectation,
            "fix": self.fix,
            "kind": self.kind,
            "related": [error.to_dict() for error in self.related],
        }

    def __reduce__(self) -> tuple[type["ConfigError"], tuple[Any, ...]]:
        """Preserve structured fields across a pickle round trip."""
        return (
            type(self),
            (self.path, self.key, self.expectation, self.fix, self.kind, self.related),
        )


def _config_key(location: tuple[int | str, ...]) -> str:
    """Render a Pydantic location as a TOML-style key."""
    parts = [str(part) for part in location]
    if not parts:
        return "-"
    key = f"[{parts[0]}]"
    if len(parts) > 1:
        key += "." + ".".join(parts[1:])
    return key


def _unknown_key_fix(key: str) -> str:
    """Name the replacement for a key that moved, not just 'remove this'."""
    replacement = _RENAMED_KEYS.get(key)
    if replacement is None:
        return "remove this key or use a documented key"
    return f"replace it with {replacement}"


def _invalid_value_fix(key: str, error: Mapping[str, Any], expectation: str) -> str:
    """Build a concrete repair from Pydantic's type and context."""
    error_type = str(error["type"])
    if error_type.startswith("int_"):
        return f"use an integer for {key}"
    if error_type.startswith(("float_", "decimal_")):
        return f"use a number for {key}"
    if error_type == "literal_error" and error.get("ctx", {}).get("expected"):
        return f"choose {error['ctx']['expected']} for {key}"
    return f"use a value that satisfies: {expectation}"


def _validation_config_error(path: Path, exc: ValidationError) -> ConfigError:
    """Translate every Pydantic failure into stable CLI envelopes."""
    failures: list[ConfigError] = []
    for error in exc.errors(include_url=False):
        key = _config_key(error["loc"])
        if error["type"] == "extra_forbidden":
            failures.append(
                ConfigError(
                    path=path,
                    key=key,
                    expectation="a supported configuration key",
                    fix=_unknown_key_fix(key),
                    kind="unknown_key",
                )
            )
            continue
        expectation = str(error["msg"]).removeprefix("Value error, ")
        failures.append(
            ConfigError(
                path=path,
                key=key,
                expectation=expectation,
                fix=_invalid_value_fix(key, error, expectation),
                kind="invalid_value",
            )
        )
    first, *related = failures
    first.related = tuple(related)
    return first


#: Parsed-kew.toml cache. The console re-reads config on every
#: ``/api/config/*`` and ``/api/report/spend`` request, and each read parsed
#: the file again (C3/#2296). Key = the file's identity and last-write stamp,
#: so ANY save invalidates it; the entry is a parse, never a resolved config,
#: so DB overrides and env vars are still layered per call and a change to
#: either is never masked. The window this CAN mask is a rewrite that lands on
#: the same inode with the same size inside one mtime tick — the next write
#: clears it. Bounded in size because a process may serve several projects.
_TOML_CACHE_MAX = 8
_toml_cache: dict[tuple[str, int, int, int], dict[str, Any]] = {}


def _toml_identity(path: Path) -> tuple[str, int, int, int] | None:
    """The file's (path, mtime, size, inode), or None when it cannot be stat'd.

    None means "this file has no cacheable identity", and the caller reads it
    uncached. The reason is logged rather than swallowed: a config file whose
    metadata cannot be read is worth a line even though the read itself still
    goes ahead.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        _log.warning(
            "config file %s could not be stat'd (%s); parsing it uncached", path, exc
        )
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _read_toml_cached(path: Path) -> dict[str, Any]:
    """:func:`_read_toml`, memoized on the file's last-write identity.

    Returns a deep copy: ``load_config`` mutates the raw mapping in place
    (log-dir anchoring, snapshot merges, env overrides), and a shared entry
    must not accumulate one caller's merges for the next one. A file that
    cannot be stat'd is read uncached rather than guessed at.
    """
    key = _toml_identity(path)
    if key is None:
        return _read_toml(path)
    cached = _toml_cache.get(key)
    if cached is None:
        cached = _read_toml(path)
        if len(_toml_cache) >= _TOML_CACHE_MAX:
            _toml_cache.clear()
        _toml_cache[key] = cached
    return deepcopy(cached)


def _read_toml(path: Path) -> dict[str, Any]:
    """Read one TOML file and translate filesystem and syntax failures."""
    try:
        with open(path, "rb") as file:
            return tomllib.load(file)
    except IsADirectoryError:
        raise ConfigError(
            path=path.resolve(),
            key="-",
            expectation="a regular TOML file",
            fix="replace this directory with a readable kew.toml file",
            kind="not_a_file",
        ) from None
    except PermissionError:
        raise ConfigError(
            path=path.resolve(),
            key="-",
            expectation="a readable TOML file",
            fix="grant read permission for this file",
            kind="unreadable",
        ) from None
    except UnicodeDecodeError:
        raise ConfigError(
            path=path.resolve(),
            key="-",
            expectation="UTF-8 encoded TOML",
            fix="save this file as UTF-8",
            kind="encoding",
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            path=path.resolve(),
            key="-",
            expectation="valid TOML",
            fix=f"correct the syntax near {exc}",
            kind="malformed_toml",
        ) from None


def _validate_dispatch_config(raw: dict[str, Any], path: Path) -> "DispatchConfig":
    """Validate the merged mapping and translate its first failure."""
    try:
        return DispatchConfig.model_validate(raw)
    except ValidationError as exc:
        raise _validation_config_error(path.resolve(), exc) from None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ProjectConfig(BaseModel):
    """GitHub project identification."""

    model_config = {"extra": "forbid"}

    name: str = ""
    repo: str = ""


class LinearTrackerConfig(BaseModel):
    """Linear tracker settings (read surface only for now).

    ``in_review_state`` disambiguates Linear's type-`started` states (a team
    can run several); the API key is env-only (LINEAR_API_KEY), never config.
    """

    model_config = {"extra": "forbid"}

    team: str = ""
    in_review_state: str = "In Review"
    # W2 (#894): once Linear webhooks are delivering (rows land in
    # `tracker_events`), the driver / `kew epic status` short-circuit the
    # GraphQL fetch when nothing changed. A full fetch still runs at least this
    # often to heal any missed webhook delivery — generous by default so the
    # cold, event-driven case almost never hits it. Zero (or negative) disables
    # the short-circuit entirely (always fetch, i.e. today's polling).
    reconcile_interval_seconds: int = 600


class TrackerConfig(BaseModel):
    """Which work tracker this project reads from (one per project)."""

    model_config = {"extra": "forbid"}

    type: Literal["github", "linear"] = "github"
    linear: LinearTrackerConfig = LinearTrackerConfig()


class ClaudeCodeConfig(BaseModel):
    """Claude Code runner-specific settings."""

    model_config = {"extra": "forbid"}

    skip_permissions: bool = True
    autocompact_pct: int = 70


class OpenCodeConfig(BaseModel):
    """OpenCode runner-specific settings."""

    model_config = {"extra": "forbid"}

    extra_args: list[str] = []
    agent: str = ""


class CodexConfig(BaseModel):
    """Official Codex CLI runner settings."""

    model_config = {"extra": "forbid"}

    reasoning_effort: Literal[
        "", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = ""
    service_tier: Literal["", "fast"] = ""


class PiConfig(BaseModel):
    """Pi runner-specific settings (pi.dev / @earendil-works/pi-coding-agent)."""

    model_config = {"extra": "forbid"}

    extra_args: list[str] = []


class HermesConfig(BaseModel):
    """Hermes runner-specific settings (OpenRouter-only headless agent)."""

    model_config = {"extra": "forbid"}

    extra_args: list[str] = []


class LemonadeConfig(BaseModel):
    """Lemonade runner settings — kew's native loop over a local endpoint.

    Targets any OpenAI-compatible server: AMD Lemonade (default port 13305),
    llama-server, LM Studio, vLLM, or Ollama's ``/v1``. ``api_key`` is usually
    unnecessary for a local server; leave it blank. Local inference has no
    per-token dollar price (see ``BillingConfig``); ``max_steps`` bounds the
    agent loop and the outer dispatch budget gate still applies as a wall.
    """

    model_config = {"extra": "forbid"}

    base_url: str = "http://localhost:13305/v1"
    api_key: str = ""
    max_steps: int = 40
    extra_args: list[str] = []

    @field_validator("max_steps")
    @classmethod
    def _validate_max_steps(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_steps must be at least 1")
        return v


class AgentConfig(BaseModel):
    """Agent execution constraints and model settings."""

    model_config = {"extra": "forbid"}

    runner: str = ""
    timeout_minutes: int = 120
    # Minutes a contained run's stream may stay silent before its container is
    # stopped (#2240); 0 turns the watchdog off.
    no_output_minutes: int = 15
    max_budget_usd: float = 25.0
    # Grace window (seconds) after the budget watchdog SIGTERMs a run for it to
    # wrap up and commit WIP before SIGKILL (#859). Longer than the default
    # terminate grace so a graceful budget stop lands a commit, not a discard.
    budget_stop_grace_seconds: int = 30
    model: str = ""
    claude_code: ClaudeCodeConfig = ClaudeCodeConfig()
    opencode: OpenCodeConfig = OpenCodeConfig()
    codex: CodexConfig = CodexConfig()
    pi: PiConfig = PiConfig()
    hermes: HermesConfig = HermesConfig()
    lemonade: LemonadeConfig = LemonadeConfig()

    @field_validator("timeout_minutes")
    @classmethod
    def validate_timeout_minutes(cls, v: int) -> int:
        if v < 1:
            raise ValueError("timeout_minutes must be at least 1")
        return v

    @field_validator("no_output_minutes")
    @classmethod
    def validate_no_output_minutes(cls, v: int) -> int:
        if v < 0:
            raise ValueError("no_output_minutes must be 0 (off) or more")
        return v

    @field_validator("max_budget_usd")
    @classmethod
    def validate_max_budget_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_budget_usd must be a finite number")
        if v <= 0:
            raise ValueError("max_budget_usd must be greater than 0")
        return v

    @field_validator("budget_stop_grace_seconds")
    @classmethod
    def validate_budget_stop_grace_seconds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("budget_stop_grace_seconds must be at least 1")
        return v


class RouteEntry(BaseModel):
    """A routing target: runner + optional model override."""

    model_config = {"extra": "forbid"}

    runner: str = ""
    model: str = ""


class CacheConfig(BaseModel):
    """Cache-economics routing configuration ([routing.cache], Phase 2+3)."""

    model_config = {"extra": "forbid"}

    governor_threshold_usd: float = 0.02
    order_aware: bool = False
    max_reorder_lookahead: int = Field(default=10, gt=0)

    @field_validator("governor_threshold_usd")
    @classmethod
    def _validate_threshold(cls, v: float) -> float:
        if v < 0:
            raise ValueError("governor_threshold_usd must be >= 0")
        return v


class RoutingConfig(BaseModel):
    """Per-complexity routing configuration."""

    model_config = {"extra": "forbid"}

    default: RouteEntry | str
    simple: RouteEntry | str | None = None
    medium: RouteEntry | str | None = None
    complex: RouteEntry | str | None = None
    cache_aware: bool = False
    cache: CacheConfig = CacheConfig()

    @model_validator(mode="after")
    def _validate_entries_not_empty(self) -> "RoutingConfig":
        """Ensure no route entry is empty (RouteEntry or string shorthand)."""
        fields = {
            "default": self.default,
            "simple": self.simple,
            "medium": self.medium,
            "complex": self.complex,
        }
        for field_name, entry in fields.items():
            if entry is None:
                continue
            if isinstance(entry, str) and not entry.strip():
                raise ValueError(
                    f"routing.{field_name}: route string must not be empty"
                )
            if (
                isinstance(entry, RouteEntry)
                and entry.runner == ""
                and entry.model == ""
            ):
                raise ValueError(
                    f"routing.{field_name}: RouteEntry must have at least one of "
                    "'runner' or 'model' set"
                )
        return self


_DEFAULT_WORKTREE_BASE_OVERRIDE: str | None = None


def _default_worktree_base() -> str:
    """Return the production checkout root or the test-only default."""
    if _DEFAULT_WORKTREE_BASE_OVERRIDE is not None:
        return _DEFAULT_WORKTREE_BASE_OVERRIDE
    return ".worktrees"


class BranchConfig(BaseModel):
    """Git branch naming and worktree settings."""

    model_config = {"extra": "forbid"}

    prefix: str = "agent/issue-"
    base: str = ""
    worktree_base: str = Field(default_factory=_default_worktree_base)


class ComplexityLevel(BaseModel):
    """Token budget and scope for a complexity tier."""

    model_config = {"extra": "forbid"}

    tokens: str = ""
    scope: str = ""


class LabelConfig(BaseModel):
    """GitHub label names for the dispatch lifecycle."""

    model_config = {"extra": "forbid"}

    ready: str = "ready"
    in_progress: str = "in-progress"
    agent_working: str = "agent-working"
    agent_failed: str = "agent-failed"
    agent_pr: str = "agent-pr"
    # A human adds this to an agent PR to let the self-hosted CI runner test
    # its current head (#2033). The host removes it before it pushes new agent
    # commits, so every new agent head needs fresh approval.
    ci_approved: str = "ci-approved"
    pr_pending_review: str = "pr-pending-review"
    blocked: str = "blocked"
    completed_no_changes: str = "agent-completed-no-changes"
    agent_needs_help: str = "agent-needs-help"
    incomplete_run: str = "incomplete-run"
    budget_exceeded: str = "budget-exceeded"
    budget_stopped: str = "budget-stopped"
    budget_stop: str = "budget-stop"
    large_pr: str = "large-pr"
    ready_to_merge: str = "ready-to-merge"
    overlap_hold: str = "overlap-hold"
    priorities: list[str] = ["p0-critical", "p1-high", "p2-medium", "p3-low"]
    complexity: dict[str, ComplexityLevel] = {
        "simple": ComplexityLevel(tokens="~30k", scope="1-2 files"),
        "medium": ComplexityLevel(tokens="~60k", scope="2-4 files"),
        "complex": ComplexityLevel(tokens="~100k", scope="cross-cutting"),
    }


class CommandsConfig(BaseModel):
    """Shell commands for CI, formatting, and commit conventions."""

    model_config = {"extra": "forbid"}

    ci: str = "make test"
    format: str = ""
    verify: str = ""
    # Scoped test invocation the governed remediation brief renders (#2115).
    # That brief forbids the full suite, so it cannot name `ci`; without this it
    # named no runner at all and the agent guessed one. Must carry `{paths}` —
    # a bare runner (`uv run pytest`) would run everything, which is exactly
    # what the governed round refuses. Empty = the brief says so out loud.
    test_targeted: str = ""
    # Pytest mark naming the repo-wide policy gates the governed brief ALWAYS
    # adds to that scoped set (#2140, #2174). Targeted selection picks tests by
    # the changed files, so a gate the change does not import never runs: PR
    # #2138 added three silent `except` handlers, reviewed READY on a green
    # targeted round, and failed `tests/test_silence_on_failure.py` on the merge
    # verify. The hand list of paths that fixed it then drifted the other way
    # (#2174) — it named the silence gate but not the id sweep, so PR #2154
    # shipped an unguarded work-item id coercion past a green targeted round. A
    # mark the gate itself carries cannot drift: a new gate joins the set the
    # moment it is written. Empty (the default) adds nothing.
    test_policy_mark: str = ""
    # The merge gate's static checks (ruff, ruff format, mypy, lint-imports,
    # bandit, semgrep, xenon — whatever the project's gate runs), run on an
    # agent head BEFORE any adversarial review (#3201). OPERATOR-CONFIG-ONLY: it
    # runs inside the approved image with no credentials and no network, never
    # on the host — mypy loads plugins the candidate's pyproject.toml names, so
    # the command executes candidate code. A failure is a remediation round
    # carrying its output, and no reviewer launches. Empty = no static step.
    static: str = ""
    # The browser checks for the surfaces a head touches, run in the same
    # container on a throwaway merge of the current base into the head (#3201).
    # Must carry `{modules}`: kew replaces it with the ui-check modules the head
    # adds or changes and the modules that own its changed files. Empty = none.
    ui_check: str = ""
    verify_loop: str = ""  # Command the loop verification GATE runs (kew executes
    # it via `sh -c`, exit 0 = goal met). OPERATOR-CONFIG-ONLY: never set from
    # issue/agent text (it is arbitrary code). Distinct from `verify`, which is
    # prompt text the agent reads. Empty = driver falls back to `ci`. See
    # first-class-loops.
    # Which stopping condition the loop verification GATE uses (#289).
    # "command" runs verify_loop/ci as sh -c; "ci_green" polls GitHub CI for the
    # pushed HEAD commit (kew.loops.ci.wait_for_ci) and replaces the command.
    verify_loop_mode: Literal["command", "ci_green"] = "command"
    # Operator-only declaration. The disposable verification checkout is read-only
    # unless this flag is true. The container root remains read-only.
    verify_loop_needs_writes: bool = False
    # Max seconds wait_for_ci polls CI before -> timeout -> needs_human (#289).
    ci_poll_timeout_seconds: int = Field(default=1800, gt=0)
    commit_prefix: str = "feat(#{issue}): "
    co_author: str = ""  # Co-authored-by trailer. Empty = omit.

    @field_validator("test_targeted")
    @classmethod
    def validate_test_targeted(cls, v: str) -> str:
        if v and "{paths}" not in v:
            raise ValueError(
                "commands.test_targeted must contain the {paths} placeholder"
                ' (e.g. "uv run pytest {paths}") — without it the command'
                " would run the full suite, which a governed remediation refuses"
            )
        return v

    @field_validator("ui_check")
    @classmethod
    def validate_ui_check(cls, v: str) -> str:
        if v.strip() and "{modules}" not in v:
            raise ValueError(
                "commands.ui_check must contain the {modules} placeholder"
                ' (e.g. "cd frontend && npm run ui-check -- {modules}") — without'
                " it the command would run every browser check, not the ones the"
                " head touches"
            )
        return v

    @field_validator("test_policy_mark")
    @classmethod
    def validate_test_policy_mark(cls, v: str) -> str:
        cleaned = v.strip()
        if cleaned and not _PYTEST_MARK_RE.fullmatch(cleaned):
            raise ValueError(
                "commands.test_policy_mark must be a bare pytest marker name"
                ' (e.g. "policy") — kew renders it into the test command the'
                " remediation runs, so a mark expression or shell text here"
                " would name a command no agent can run"
            )
        return cleaned


class PromptConfig(BaseModel):
    """Prompt template and content toggles."""

    model_config = {"extra": "forbid"}

    template: str = ""
    append: str = ""
    include_safety_rules: bool = True
    include_execution_strategy: bool = True
    include_coderabbit: bool = False
    include_dispatch_report: bool = True
    include_repo_map: bool = True
    repo_map_tokens: int = 1024
    # "hint" (default, current behavior): map generated with the issue
    # title/body as a hint, so file ordering — and thus repo_map_hash /
    # prefix_key — varies per issue. "stable": map generated with no hint
    # (deterministic path-alphabetical ordering), so the hash stays constant
    # between main merges. The hint-vs-stable trade-off is decided by the
    # pre-registered Phase-1 experiment, never by flipping this default
    # (spec D1/D4/D11).
    repo_map_mode: Literal["hint", "stable"] = "hint"

    @field_validator("repo_map_tokens")
    @classmethod
    def validate_repo_map_tokens(cls, v: int) -> int:
        if v < 1:
            raise ValueError("repo_map_tokens must be at least 1")
        return v


class SecurityConfig(BaseModel):
    """Patterns for blocking dangerous shell commands."""

    model_config = {"extra": "forbid"}

    blocked_prefix_patterns: list[str] = []
    blocked_exact_patterns: list[str] = []


class OpenShellConfig(BaseModel):
    """``[sandbox.openshell]`` — the gateway, the CLI, and the credential map.

    Read by :mod:`kew.openshell`, the only module that shells out to the
    ``openshell`` CLI, and by :mod:`kew.openshell_broker`, which decides
    admission and attestation from ``providers``. Every key is inert unless
    ``[sandbox] runtime`` selects ``openshell``.
    """

    model_config = {"extra": "forbid"}

    #: The gateway endpoint. Empty leaves the CLI's own default in place, and
    #: ``OPENSHELL_GATEWAY`` — the CLI's own variable — wins over this value.
    gateway: str = ""

    #: The CLI version this build's client is written against. kew compares it
    #: to the installed CLI and refuses a mismatch rather than driving an
    #: unknown argv surface; raise it deliberately when a new one is verified.
    version: str = "0.1.2"

    #: ``<runner>`` = ``<provider name>``: which gateway credential provider
    #: authenticates each runner. On this runtime no credential VALUE reaches
    #: the agent's environment — the provider holds it and the gateway injects
    #: it at the endpoint — so this map, not the host environment, is what
    #: admission checks (:func:`kew.openshell_broker.provider_admission`).
    #:
    #: An unmapped runner cannot authenticate, so it refuses the dispatch
    #: naming the missing entry; it is never a silent fall-through to another
    #: runner's provider, which would spend the wrong account.
    providers: dict[str, str] = {}

    @field_validator("gateway", "version")
    @classmethod
    def strip_value(cls, value: str) -> str:
        """Trim surrounding whitespace; a padded endpoint resolves to nothing."""
        return value.strip()

    @field_validator("version")
    @classmethod
    def require_version(cls, value: str) -> str:
        """A blank pin cannot be compared, so it would accept any CLI."""
        if not value:
            raise ValueError(
                "sandbox.openshell.version cannot be blank — it is the CLI "
                "version the client is written against"
            )
        return value

    @field_validator("providers")
    @classmethod
    def validate_providers(cls, v: dict[str, str]) -> dict[str, str]:
        """Reject a mapping that could never resolve, at load.

        Three ways this map can be set and still be inert, each checked here
        because each one otherwise surfaces much later as "no credential":
        a key no runner is ever called, a blank half, and a provider name the
        ledger's ``auth_path`` grammar would refuse at profile composition —
        after the dispatch was already paid for.

        Imported inside the validator for the same reason
        :meth:`SandboxConfig.validate_images` is: ``kew.runners.*`` imports
        this module, so a top-level import is a cycle.
        """
        from kew.openshell_broker import PROVIDER_NAME_RE
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        normalized: dict[str, str] = {}
        for runner, provider in v.items():
            key, name = runner.strip(), provider.strip()
            if not key or not name:
                raise ValueError(
                    "sandbox.openshell.providers entries must be non-empty "
                    f'"<runner>" = "<provider>" pairs, got '
                    f"{runner!r} = {provider!r}"
                )
            if key not in KNOWN_RUNNER_NAMES:
                known = ", ".join(sorted(KNOWN_RUNNER_NAMES))
                raise ValueError(
                    f"sandbox.openshell.providers key {runner!r} is not a known "
                    f"runner, so nothing would ever resolve to it. Known "
                    f"runners: {known}."
                )
            if not PROVIDER_NAME_RE.match(name):
                raise ValueError(
                    f"sandbox.openshell.providers[{key}] = {provider!r} is not a "
                    "usable provider name: letters, digits, '.', '_' and '-' "
                    "only, starting with a letter or digit, at most 64 "
                    "characters. The name is attested in the run's auth path, "
                    "which refuses anything else."
                )
            if key in normalized:
                raise ValueError(
                    f"sandbox.openshell.providers maps runner {key!r} more than "
                    "once (keys differing only by surrounding whitespace)"
                )
            normalized[key] = name
        return normalized


#: Place every run on the operator machine. Today's behaviour, and the only
#: policy that needs no cloud account — which is why it stays the default.
PLACEMENT_LOCAL = "local"

#: Place every run on a gcp instance. A run arriving when every instance is at
#: ``[hosts.gcp] max_runs`` WAITS: an explicit pin is not a preference, so
#: falling back to the Mac here would quietly undo the operator's decision.
PLACEMENT_GCP = "gcp"

#: Place on gcp while an instance has room, and on the operator machine when
#: none does. The policy that keeps the queue moving through a full cloud.
PLACEMENT_PREFER_GCP = "prefer-gcp"

#: Every placement policy kew carries out.
KNOWN_PLACEMENTS = frozenset({PLACEMENT_LOCAL, PLACEMENT_GCP, PLACEMENT_PREFER_GCP})


def _known_sandbox_host(key: str, value: str) -> str:
    """*value* as a host kew knows, or a refusal naming *key* and the two that exist.

    One refusal for ``[sandbox] host`` and every ``[sandbox] hosts`` entry, so
    the override cannot admit a name the default refuses.
    """
    from kew.run_store import KNOWN_RUN_HOSTS

    name = value.strip().lower()
    if name not in KNOWN_RUN_HOSTS:
        known = " or ".join(f'"{host}"' for host in sorted(KNOWN_RUN_HOSTS))
        raise ValueError(f"{key} must be {known}, got {value!r}")
    return name


def _known_run_kind(key: str, kind: str) -> str:
    """*kind* as a run kind kew launches, or a refusal naming the ones it does.

    The host half of a ``[sandbox] hosts`` entry was checked and the key half
    was not, so a misspelled run kind passed load-time validation and matched
    no placement — leaving the kind it meant to move on the default host, which
    is the same invisible failure one level up from the one the load-time check
    exists to prevent (#2437 review).

    The taxonomy is imported lazily, and only once an override exists: this
    module keeps ``kew.run_store`` out of its module scope, exactly as
    :func:`validate_capability_pin` does for the schema version.
    """
    from kew.run_store import KNOWN_RUN_KINDS

    name = kind.strip()
    if name not in KNOWN_RUN_KINDS:
        known = ", ".join(sorted(KNOWN_RUN_KINDS))
        raise ValueError(
            f"{key} keys must each name a run kind kew runs ({known}), got {kind!r}"
        )
    return name


class SandboxConfig(BaseModel):
    """Sandboxed dispatch container settings.

    ``enabled`` is tri-state:
    - ``"auto"`` (default): sandbox when a container runtime is detected. When
      one is NOT detected, kew refuses rather than downgrading — see
      ``dangerously_skip_containment``.
    - ``True``: hard-require a runtime; refuse to dispatch without one.
    - ``False``: do not sandbox. Uncontained, so it also requires
      ``dangerously_skip_containment``.

    No value of ``enabled`` reaches uncontained host execution on its own. That
    used to be the default behaviour of ``"auto"``: a machine with no container
    runtime silently ran the agent directly on the host, and the only signal was
    a one-line notice among the dispatch output. An operator could be in the
    unsafe posture without ever having chosen it, which is why the choice now
    has a name.

    ``images`` maps a runner name (``claude-code``, ``opencode``, ``pi``,
    ``hermes``, ``lemonade``) to the container image that implements the
    entrypoint contract for it. Keys are stripped and checked against the
    runner registry at load, because an unknown key is silently inert — it can
    never be looked up, so it degrades to "unmapped" at dispatch while looking
    set in the config. ``image`` stays the back-compat default for
    ``claude-code`` — every committed config sets it, and the stock kew-agent
    image ships only the claude CLI. A runner with neither an ``images`` entry
    nor that fallback cannot be contained (see ``resolve_runner_image``).

    The ``image`` default names the digest pin for the packaged build inputs.
    Nothing publishes a kew agent image and nothing will (2026-08-07 ruling,
    W3.8), so a registry default described a pull that could only ever fail —
    and the failure surfaced as a registry error, which reads as "kew is
    broken" rather than "build the image". ``kew sandbox build`` uses the
    Dockerfiles shipped in the package. Every launch passes ``--pull=never``
    (``kew.sandbox.build_run_argv``), so the runtime never resolves a missing
    local image through a registry.
    """

    model_config = {"extra": "forbid"}

    enabled: bool | Literal["auto"] = "auto"
    # Only enabled=True activates this exception for remediation flows.
    allow_uncontained_flows: bool | None = Field(default=None, strict=True)
    runtime: str = "docker"
    image: str = "kew-agent:docker-d148816b388f"
    images: dict[str, str] = {}
    #: The image the pre-review gate's static and ui-check halves and the
    #: resolver's checks run in (#3281): the approved agent image with the
    #: repository's dependencies built in (``scripts/build-check-image.sh``
    #: builds kew's). Empty runs them in the runner image. A tag the runtime
    #: cannot resolve refuses the checks; it never falls back.
    check_image: str = ""
    #: The files :attr:`check_image` is built from, as ``[epic]
    #: merge_gate_paths`` globs (same full-match semantics). A head that
    #: changes one of them, and that the merge queue therefore holds for a
    #: person, has its failures in the check image skipped rather than judged
    #: by the base: the image's dependencies predate the head's. Empty falls
    #: back to every dependency manifest and lock in the tree, which is broad;
    #: set it to what the image's build actually copies.
    check_image_inputs: list[str] = []
    network: str = "allowlist"
    proxy_environment: str = "none"

    #: Which host an agent container runs on: ``"local"`` (the default, and
    #: the only one that needs no cloud account) or ``"gcp"``, which ships the
    #: run to a Compute Engine instance (:mod:`kew.hosts.remote_dispatch`).
    #:
    #: Here rather than in ``[hosts] provider`` for the reason ``[verify]
    #: host`` is: the provider is which machine kew *has*, and this is which
    #: machine an agent *runs on*, so an operator can keep a configured gcp
    #: host and still run agents on the laptop in front of them.
    host: str = "local"

    #: Per-run-kind overrides of :attr:`host`, e.g. ``hosts = {dispatch =
    #: "gcp"}`` to place implementation runs on the VM while reviews stay on
    #: this machine. A kind with no entry falls back to :attr:`host`.
    hosts: dict[str, str] = {}

    #: The fleet's placement POLICY, which :attr:`host` is the one-host case of
    #: (#2398). ``"local"`` keeps every run on the operator machine (the
    #: default, and the only policy that needs no cloud account); ``"gcp"``
    #: places every run on a Compute Engine instance and makes a run WAIT when
    #: every instance is at ``[hosts.gcp] max_runs``; ``"prefer-gcp"`` places on
    #: gcp while an instance has room and falls back to this machine when none
    #: does — so a full cloud never idles the queue.
    #:
    #: Empty (the default) means "whatever :attr:`host` says", so an operator
    #: who already pinned ``[sandbox] host = "gcp"`` keeps that behaviour
    #: without learning a second key. A per-kind :attr:`hosts` entry is an
    #: explicit pin and still wins over the policy for that kind.
    placement: str = ""
    openshell: OpenShellConfig = OpenShellConfig()

    #: Lower the per-container memory cap from the hard 8g ceiling, so the
    #: operator can fit ``max_workers`` containers inside a container VM that
    #: is smaller than the host. Lower-only and never clamped — see
    #: :mod:`kew.sandbox_caps`. Unset keeps the ceiling.
    max_memory: str | None = None
    #: Lower the per-container CPU cap from the hard 4-CPU ceiling. The
    #: effective value is still clamped to the daemon's own CPU count at
    #: launch, because a cap above it is rejected outright.
    max_cpus: int | None = None

    #: Run agents directly on the host, with no container boundary.
    #:
    #: Named after Claude Code's ``--dangerously-skip-permissions`` rather than
    #: "yolo mode": kew *drives* Codex as a runner and yolo is Codex's own term,
    #: so one session would carry two different ones. The danger travels in the
    #: name, so a copy-pasted config line warns the next person to read it.
    #:
    #: The framing is what kew already does elsewhere — the agent runs with
    #: permissions skipped BECAUSE the container is the boundary. Skipping the
    #: container too is the dangerous part, and this says so.
    #:
    #: Supported, not deprecated: no-Docker machines and local-model runners
    #: that expose a host shell genuinely need it. It must be *chosen*.
    dangerously_skip_containment: bool = False

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str) -> str:
        """Reject a runtime kew has no backend seam for.

        It was a free string, so a typo — or a path, where only a bare name is
        resolvable through the subprocess allowlist — reached dispatch and was
        reported as "runtime not found": a missing-Docker message for what is
        really a config error. The runtime is also the container boundary, so
        an inert value here is a containment question answered by accident.
        """
        if value not in SUPPORTED_RUNTIMES:
            supported = ", ".join(repr(name) for name in SUPPORTED_RUNTIMES)
            raise ValueError(f"sandbox.runtime must be one of {supported}")
        return value

    @field_validator("max_memory", mode="before")
    @classmethod
    def validate_max_memory(cls, value: object) -> object:
        """Accept only a lowering of the hard memory cap, never a raise."""
        return value if value is None else validated_max_memory(value)

    @field_validator("max_cpus", mode="before")
    @classmethod
    def validate_max_cpus(cls, value: object) -> object:
        """Accept only a lowering of the hard CPU cap, never a raise."""
        return value if value is None else validated_max_cpus(value)

    @field_validator("host")
    @classmethod
    def known_agent_host(cls, value: str) -> str:
        """A host with no provider behind it must fail at load, not at launch."""
        return _known_sandbox_host("sandbox.host", value)

    @field_validator("hosts")
    @classmethod
    def known_agent_hosts(cls, value: dict[str, str]) -> dict[str, str]:
        """Every override names one run kind kew runs and one host kew knows.

        Both halves are checked at load because an override is read once, at
        placement: a misspelled host would silently fall back to this machine,
        and a misspelled run kind matches no placement at all — so the kind the
        operator meant to move keeps the default host. Neither is visible to
        the operator who asked for a VM and got this machine.
        """
        resolved: dict[str, str] = {}
        for kind, host in value.items():
            name = _known_run_kind("sandbox.hosts", kind)
            resolved[name] = _known_sandbox_host(f"sandbox.hosts.{name}", host)
        return resolved

    @field_validator("placement")
    @classmethod
    def known_placement(cls, value: str) -> str:
        """A policy kew cannot carry out must fail at load, not at placement.

        Checked here for the same reason :attr:`host` is: the policy is read
        once, when a run is placed, and a misspelling there is indistinguishable
        from "the operator wanted this machine" — which is exactly the silent
        fallback an operator who asked for a VM would never see.
        """
        name = value.strip().lower()
        if name and name not in KNOWN_PLACEMENTS:
            known = ", ".join(sorted(KNOWN_PLACEMENTS))
            raise ValueError(f"sandbox.placement must be one of {known}, got {value!r}")
        return name

    def host_for(self, run_kind: str) -> str:
        """The placement governing *run_kind*: its override, else the policy.

        A per-kind :attr:`hosts` entry is an explicit pin and outranks the
        policy — an operator who wrote ``hosts = {review = "local"}`` asked for
        this machine by name. Everything else reads
        :meth:`placement_policy`, NOT :attr:`host` directly: two readers that
        could disagree about the same run kind is how an operator who set
        ``placement`` and left ``host`` alone would find half of kew placing
        work one way and half the other.
        """
        return self.hosts.get(run_kind.strip(), self.placement_policy())

    def placement_policy(self) -> str:
        """The resolved placement policy: :attr:`placement`, else :attr:`host`.

        One reader for the two keys, so no caller has to know that ``[sandbox]
        host`` predates the policy it is now the single-host case of.
        """
        return self.placement or self.host

    @field_validator("proxy_environment")
    @classmethod
    def validate_proxy_environment(cls, value: str) -> str:
        """Require an explicit choice before forwarding host proxy credentials."""
        if value not in {"none", "inherit"}:
            raise ValueError("sandbox.proxy_environment must be 'none' or 'inherit'")
        return value

    @property
    def uncontained_flows_allowed(self) -> bool:
        """Return whether the explicit remediation exception takes effect."""
        return self.enabled is True and self.allow_uncontained_flows is True

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        """``allowlist``, ``open``, or a real Docker network name — never blank.

        A blank name would inspect nothing, report an empty network as absent,
        and later hand ``docker run --network ""`` a value it refuses.
        """
        if not v or not v.strip():
            raise ValueError(
                "[sandbox] network must be 'allowlist', 'open', or a Docker "
                "network name; it cannot be blank"
            )
        return v.strip()

    @model_validator(mode="before")
    @classmethod
    def reject_removed_on_missing_runtime(cls, data: object) -> object:
        """``on_missing_runtime`` is gone; point at what replaced it.

        It selected between "downgrade to the host" and "refuse" when no runtime
        was found. Refusing is now the only default, so the knob's sole
        remaining meaning was the unsafe half — expressed here under a name that
        says what it does. Intercepted before ``extra="forbid"`` so the operator
        gets a migration instead of "Extra inputs are not permitted".
        """
        if isinstance(data, dict) and "on_missing_runtime" in data:
            previous = data["on_missing_runtime"]
            replacement = (
                "set `dangerously_skip_containment = true` (and read what it means)"
                if previous == "host"
                else "delete the key — refusing is now the default"
            )
            raise ValueError(
                f"[sandbox] on_missing_runtime = {previous!r} has been removed. "
                f"kew no longer runs agents on the host without being told to: "
                f"{replacement}."
            )
        return data

    @model_validator(mode="after")
    def reject_contradictory_posture(self) -> "SandboxConfig":
        """``enabled = true`` and ``dangerously_skip_containment`` cannot both hold.

        ``enabled = true`` means "hard-require containment; refuse without it".
        The flag means "run with no container boundary". Together they are a
        direct contradiction, and resolution silently favoured the flag — a
        config asking for a guarantee got the opposite, with no signal.

        Refusing at load is the only honest answer: kew cannot know which half
        the operator meant, and guessing wrong in one direction dispatches an
        uncontained agent from a config that demanded containment.
        """
        if self.enabled is True and self.dangerously_skip_containment:
            raise ValueError(
                "[sandbox] enabled = true requires containment, and "
                "dangerously_skip_containment = true skips it — these "
                "contradict. Remove one: keep `enabled = true` to require a "
                "container, or drop it to run on the host deliberately."
            )
        return self

    @field_validator("check_image_inputs")
    @classmethod
    def _validate_check_image_inputs(cls, v: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in v]
        if any(not entry for entry in cleaned):
            raise ValueError("sandbox.check_image_inputs entries must not be blank")
        return cleaned

    @field_validator("images")
    @classmethod
    def validate_images(cls, v: dict[str, str]) -> dict[str, str]:
        # A mapping is looked up by EXACT runner name at dispatch time, so any
        # key the registry doesn't know — a typo, a stray space, the wrong case
        # — is silently inert: the runner resolves to something this map has no
        # entry for and the dispatch fails closed as "unmapped", pointing at a
        # key the operator can see they already set. Normalize what is
        # unambiguous (surrounding whitespace) and reject what is not, at config
        # load where the fix is obvious.
        #
        # Imported inside the validator, not at module scope: kew.runners.*
        # imports this module (via the package __init__ -> registry), so a
        # top-level import here is a cycle.
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        normalized: dict[str, str] = {}
        for runner, image in v.items():
            key, ref = runner.strip(), image.strip()
            if not key or not ref:
                raise ValueError(
                    "sandbox.images entries must be non-empty "
                    f'"<runner>" = "<image ref>" pairs, got {runner!r} = {image!r}'
                )
            if key not in KNOWN_RUNNER_NAMES:
                known = ", ".join(sorted(KNOWN_RUNNER_NAMES))
                raise ValueError(
                    f"sandbox.images key {runner!r} is not a known runner, so "
                    "nothing would ever resolve to it. Known runners: "
                    f"{known}."
                )
            if key in normalized:
                raise ValueError(
                    f"sandbox.images maps runner {key!r} more than once "
                    "(keys differing only by surrounding whitespace)"
                )
            normalized[key] = ref
        return normalized

    @field_validator("enabled", mode="before")
    @classmethod
    def validate_enabled(cls, v: object) -> bool | str:
        # Back-compat: existing bool TOML keeps working; "auto" is the new
        # tri-state default. Accept case-insensitive string forms too.
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if low == "auto":
                return "auto"
            if low in ("true", "false"):
                return low == "true"
        raise ValueError(f'sandbox.enabled must be true, false, or "auto", got {v!r}')


class GcpHostConfig(BaseModel):
    """``[hosts.gcp]`` — which Compute Engine instance runs a verification job.

    Read only by :class:`kew.hosts.GcpProvider`, which drives the ``gcloud``
    CLI. ``gcloud`` is an optional external tool like ``docker`` and ``gh``:
    probed at use, reported by ``kew doctor``, never a Python dependency.
    """

    model_config = {"extra": "forbid"}

    #: The GCP project the instance lives in. Empty means no gcp host.
    project: str = ""

    #: The instance's zone, e.g. ``us-central1-a``.
    zone: str = ""

    #: The Compute Engine instance name. Defaults to the first entry of
    #: :attr:`instances`, so a pool need not repeat its own first host.
    instance: str = ""

    #: The instances a sharded verification spreads across, in order
    #: (``kew verify --shards N`` takes the first N). The first of them runs
    #: the single-run phases beside its own shard. Empty means the pool is
    #: :attr:`instance` alone, which is what an unsharded verify uses.
    instances: list[str] = []

    #: How many agent runs one instance may host at a time (#2398). The
    #: per-host half of the fleet's slot accounting: ``[fleet] max_concurrent``
    #: bounds the whole fleet, and this bounds each machine in it, so a
    #: ``prefer-gcp`` placement knows when an instance is full and has to look
    #: elsewhere. Exactly 1 today, and refused above it — see
    #: :meth:`runs_the_host_transport_honours`: the number placement spends has
    #: to be the number a host will actually run.
    max_runs: int = 1

    #: Minutes of idle time ``release()`` arms the host's own stop after, as a
    #: ``systemd`` timer rather than a ``shutdown`` (which would close logins
    #: before it fired). The host stops itself, so a crashed driver cannot
    #: leave a VM billing forever.
    idle_stop_minutes: int = 30

    #: The deadline, in minutes, for one job's ssh session when the job names
    #: no ceiling of its own (``[verify] timeout_seconds = 0``). A backstop
    #: against a connection that died silently, which the local ``ssh`` would
    #: otherwise wait on forever while holding this host's lock (#2465): the
    #: session is killed and the job reported as interrupted. It never cuts a
    #: ceiling the caller did name, and the default sits above the longest one
    #: kew itself ships (:attr:`VerifyConfig.timeout_seconds`, 7200s) — a
    #: deadline reached by a job that was merely slow would be attested as a
    #: lost connection (review of #2475).
    job_timeout_minutes: int = 240

    @field_validator("project", "zone", "instance")
    @classmethod
    def strip_identifier(cls, value: str) -> str:
        """Trim surrounding whitespace; a padded name resolves to nothing."""
        return value.strip()

    @field_validator("max_runs")
    @classmethod
    def runs_the_host_transport_honours(cls, value: int) -> int:
        """Exactly 1: capacity kew spends has to be capacity a host will honour.

        Below 1 is a host that is not a host — nothing would ever be placed on
        it, so it would only bill for being up.

        Above 1 is worse, which is why it is refused rather than clamped (PR
        #2466 review). The transport serializes runs on an instance twice over:
        a job holds the host's non-blocking ``flock`` for its whole session
        (:mod:`kew.hosts.lock`), and every job on that machine shares the one
        checkout directory ``ship()`` writes. So a second run placed on a busy
        instance does not run beside the first — it is refused with
        :class:`~kew.hosts.errors.HostBusy`, and under ``prefer-gcp`` that
        refusal lands where the fallback to the operator machine should have.
        Clamping it quietly would be the silently-lowered cap ``[sandbox]
        max_memory`` refuses to be. Raising this ceiling is a transport change —
        a checkout per run on the instance — not a config edit.
        """
        if value < 1:
            raise ValueError(
                "hosts.gcp.max_runs must be at least 1: a host with room for "
                "zero runs would never be placed on, so it would only ever "
                "bill for being up"
            )
        if value > 1:
            raise ValueError(
                "hosts.gcp.max_runs must be 1: a host runs one job at a time "
                "(the per-host lock, and one checkout per instance for jobs to "
                "share), so a second run placed on an instance is refused "
                "rather than run beside the first. Raising this needs a "
                "checkout per run on the host, not a config change"
            )
        return value

    @field_validator("idle_stop_minutes")
    @classmethod
    def require_positive_idle(cls, value: int) -> int:
        """A zero delay is an immediate stop, not an idle grace."""
        if value < 1:
            raise ValueError(
                "hosts.gcp.idle_stop_minutes must be at least 1: +0 would stop "
                "the host the moment a job released it"
            )
        return value

    @field_validator("job_timeout_minutes")
    @classmethod
    def require_positive_deadline(cls, value: int) -> int:
        """A deadline of zero is not "unbounded" here; it is already expired.

        The hang this bounds is a job that never ends, so there is no spelling
        for "no deadline": a host that cannot give up is the defect.
        """
        if value < 1:
            raise ValueError(
                "hosts.gcp.job_timeout_minutes must be at least 1: a zero "
                "deadline is already expired when the job starts"
            )
        return value

    @field_validator("instances")
    @classmethod
    def usable_instance_pool(cls, value: list[str]) -> list[str]:
        """Every pool entry names one instance, and no instance twice.

        A blank entry addresses nothing, and a repeated one is two shards
        aimed at one machine: kew runs one job at a time per host, so they
        would queue behind each other's lock and the shard that was supposed
        to halve the wall clock would double a host's.
        """
        names = [entry.strip() for entry in value]
        for name, entry in zip(names, value, strict=True):
            if not name or len(name.split()) != 1:
                raise ValueError(
                    f"hosts.gcp.instances entries must each name one instance, "
                    f"got {entry!r}"
                )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                "hosts.gcp.instances names the same instance twice "
                f"({', '.join(duplicates)}); one host runs one job at a time, "
                "so two shards on it would run one after the other"
            )
        return names

    @model_validator(mode="after")
    def instance_defaults_to_the_pools_first_host(self) -> "GcpHostConfig":
        """A pool names its own first host; ``instance`` need not repeat it."""
        if not self.instance and self.instances:
            self.instance = self.instances[0]
        return self

    def configured(self) -> bool:
        """True iff all three identifiers needed to address an instance are set."""
        return bool(self.project and self.zone and self.instance)

    def pool(self) -> tuple[str, ...]:
        """The instances a verification may address, in the pool's order.

        ``instance`` alone when no pool is configured, so an unsharded verify
        reads the same host whether or not an operator ever listed a second.
        """
        return tuple(self.instances) if self.instances else (self.instance,)


class HostsConfig(BaseModel):
    """``[hosts]`` — where a verification job runs.

    ``local`` is the default and needs no cloud account at all. Selecting
    ``gcp`` requires the three ``[hosts.gcp]`` identifiers: a provider that
    cannot name its instance would only fail later, at the first job.
    """

    model_config = {"extra": "forbid"}

    #: ``"local"`` (default) runs the job on this machine; ``"gcp"`` ships it
    #: to the configured Compute Engine instance.
    provider: str = "local"

    gcp: GcpHostConfig = GcpHostConfig()

    @field_validator("provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        """A name with no provider behind it must fail at load, not at use."""
        name = value.strip().lower()
        if name not in ("local", "gcp"):
            raise ValueError(f'hosts.provider must be "local" or "gcp", got {value!r}')
        return name

    @model_validator(mode="after")
    def gcp_provider_needs_an_instance(self) -> "HostsConfig":
        """``provider = "gcp"`` with no instance addresses nothing."""
        if self.provider == "gcp" and not self.gcp.configured():
            raise ValueError(
                'hosts.provider = "gcp" requires [hosts.gcp] project, zone and '
                "instance: without all three there is no instance to start"
            )
        return self


class VerifyConfig(BaseModel):
    """``[verify]`` — how ``kew verify`` runs one merge verification.

    The host name lives here rather than in ``[hosts] provider``: the provider
    is which machine kew *has*, and this is which machine a verification *runs
    on*, so an operator can keep a configured gcp host and still default the
    gate to the laptop in front of them.
    """

    model_config = {"extra": "forbid"}

    #: ``"local"`` (the default, and needs no cloud account) or ``"gcp"``.
    #: ``kew verify --host`` overrides it for one run.
    host: str = "local"

    #: Pytest node ids the verification deselects, for tests that cannot pass
    #: on a verify host for environmental reasons alone. Passed to the job as
    #: ``PYTEST_ADDOPTS``; empty means the host runs the suite kew's CI runs.
    deselect: list[str] = []

    #: Wall-clock ceiling for the whole job. ``0`` leaves it unbounded; a job
    #: that outruns a ceiling has no exit code and is recorded as incomplete.
    timeout_seconds: int = 7200

    #: Run the held-out phase on the verify host before a reviewer launches
    #: (#3212): the base's tests against the head merged with the current base,
    #: judged against the PR body's **Behavior changes** section. An undeclared
    #: failure is sent back as a remediation round and no reviewer launches.
    held_out_before_review: bool = False

    #: The ceiling of that pre-review held-out run, in seconds. A run that
    #: outruns it is skipped for the head, never a finding against it.
    held_out_before_review_timeout_seconds: int = 1200

    #: Which tests a PR's merge verify runs (#3202). ``"full"`` (the default)
    #: runs the whole suite. ``"tiered"`` runs the test files
    #: :mod:`kew.verify_selection` selects plus every ``policy`` test, and
    #: falls back to the full suite whenever safety is in doubt
    #: (:mod:`kew.verify_suite`). The nightly always runs the full suite.
    suite: Literal["full", "tiered"] = "full"

    #: What vitest and ``ui-check`` run on a PR that changes ``frontend/``
    #: (#3213). ``"full"`` (the default) runs them whole. ``"scoped"`` runs
    #: ``vitest related`` on the changed files and the ui-check modules that
    #: own the change, and runs them whole whenever that is in doubt
    #: (:mod:`kew.verify_frontend`). svelte-check, the builds and the www
    #: gates always run whole, and the nightly runs everything.
    frontend: Literal["full", "scoped"] = "full"

    @field_validator("host")
    @classmethod
    def known_host(cls, value: str) -> str:
        """A host with no provider behind it must fail at load, not at use."""
        name = value.strip().lower()
        if name not in ("local", "gcp"):
            raise ValueError(f'verify.host must be "local" or "gcp", got {value!r}')
        return name

    @field_validator("deselect")
    @classmethod
    def usable_node_ids(cls, value: list[str]) -> list[str]:
        """A node id reaches a shell word; whitespace in one is not one."""
        for node_id in value:
            if not node_id.strip() or len(node_id.split()) != 1:
                raise ValueError(
                    f"verify.deselect entries must be single-word pytest node "
                    f"ids, got {node_id!r}"
                )
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def non_negative_ceiling(cls, value: int) -> int:
        """A negative ceiling is already expired; 0 is the unbounded spelling."""
        if value < 0:
            raise ValueError(
                "verify.timeout_seconds must be 0 (unbounded) or a positive "
                f"number of seconds, got {value}"
            )
        return value

    @field_validator("held_out_before_review_timeout_seconds")
    @classmethod
    def bounded_pre_review_ceiling(cls, value: int) -> int:
        """The pre-review run is the cheap check: it always has a ceiling."""
        if value <= 0:
            raise ValueError(
                "verify.held_out_before_review_timeout_seconds must be a "
                f"positive number of seconds, got {value}"
            )
        return value


class CiConfig(BaseModel):
    """How kew reads a head's GitHub check runs (#2033)."""

    model_config = {"extra": "forbid"}

    # The CI jobs that skip an agent PR until a human adds `labels.ci_approved`.
    # A check run of one of these names (or a matrix leg, "test (3.12)") whose
    # every run was skipped did not run: the head waits for approval. Any other
    # all-skipped check (push-only, paths-filtered, matrix-excluded) is ignored,
    # as GitHub ignores it. Defaults to the two gated jobs in ci.yml, pinned by
    # tests/test_workflow_agent_pr_gate.py.
    approval_gated_checks: list[str] = ["test", "agent-image-scan"]


class RetryConfig(BaseModel):
    """Retry and backoff settings for failed agent runs."""

    model_config = {"extra": "forbid"}

    max_attempts: int = 3
    max_resume_attempts: int = 2
    backoff_base_seconds: int = 30
    backoff_multiplier: float = 2.0
    retriable_exit_codes: list[int] = [1]

    @model_validator(mode="after")
    def _apply_backward_compat(self) -> "RetryConfig":
        # Ensure at least one attempt
        if self.max_attempts < 1:
            self.max_attempts = 1
        # Clamp max_resume_attempts
        max_retries = max(self.max_attempts - 1, 0)
        if self.max_resume_attempts > max_retries:
            self.max_resume_attempts = max_retries
        return self


class ParallelConfig(BaseModel):
    """Concurrency limits for parallel dispatch."""

    model_config = {"extra": "forbid"}

    max_workers: int = 1
    shutdown_grace_seconds: int = 30

    @field_validator("max_workers")
    @classmethod
    def validate_max_workers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_workers must be at least 1")
        return v


class FleetConfig(BaseModel):
    """``[fleet]`` — how many agent runs the WHOLE fleet may have in flight.

    ``[parallel] max_workers`` was this bound for as long as there was one
    machine to spend it on: a worker was a process on the operator's Mac, so
    counting workers and counting machines-worth-of-agent were the same count.
    Once a run can land on a Compute Engine instance they come apart —
    ``max_workers`` reads as "how many agents this machine runs", and the thing
    the owner actually caps is how many agents exist anywhere (owner decision
    2026-10-06: VMs REPLACE Mac slots, they do not add to them).

    So this is the fleet-wide number, read off the ledger rather than off any
    host's process table, and it DEFAULTS to ``[parallel] max_workers`` — an
    operator who never opens this section keeps exactly today's cap.
    """

    model_config = {"extra": "forbid"}

    #: The fleet-wide in-flight cap. ``None`` (the default) means "whatever
    #: ``[parallel] max_workers`` says" — see
    #: :meth:`DispatchConfig.fleet_concurrency`.
    max_concurrent: int | None = None

    @field_validator("max_concurrent")
    @classmethod
    def require_at_least_one_agent(cls, value: int | None) -> int | None:
        """At least one, exactly as ``[parallel] max_workers`` is.

        This key REPLACES ``max_workers`` as the number every concurrency
        reader takes, so it inherits that key's floor rather than inventing a
        value below it. 0 does not mean "unbounded" here: it is the cap the
        scheduler sizes its process pool from (``ProcessPoolExecutor`` refuses
        ``max_workers=0``), the capacity the drain loop reports, and the epic
        driver's dispatch budget — so a 0 that the admission gate alone reads
        as unbounded would stop the fleet dead everywhere else. An operator who
        wants a bigger cap writes a bigger number.
        """
        if value is not None and value < 1:
            raise ValueError(
                "fleet.max_concurrent must be at least 1: it is the number of "
                "agents allowed in flight anywhere, and 0 would halt the fleet "
                "rather than unbound it"
            )
        return value


class ReviewConfig(BaseModel):
    """PR review handling options."""

    model_config = {"extra": "forbid"}

    auto_reply: bool = True
    include_resolved: bool = False
    max_review_usd: float = 10.0
    # Separate cap for the governed remediation leg (``kew review <PR>`` driven
    # for an epic node — fixing what the reviewer flagged).  Remediation runs
    # have a fundamentally different cost profile from a single adversarial
    # reviewer pass, so they get their own pool rather than sharing
    # ``max_review_usd``.  $40 covers substantial multi-file fixes; raise it
    # per project for heavier remediations (#993).
    max_remediation_usd: float = 40.0
    # Governed adversarial-review stage (#977 epic). ``adversarial`` gates the
    # whole stage; ``blocking`` decides whether a non-READY verdict HELDs the
    # node (True) or is merely attested and proceeds (False, the advisory
    # on-ramp). ``max_rounds`` caps remediation cycles before HELD. ``model``
    # picks the reviewer runner model (empty = runner default).
    adversarial: bool = False
    blocking: bool = True
    max_rounds: int = 2
    model: str = ""
    # Pin the reviewer's runner independently of node routing (#1069). Empty =
    # claude-code, the only accepted value today: the verdict must come from
    # the runner's own typed terminal stream event (#2021), and only
    # claude-code emits one kew can read. Any other runner is a ConfigError at
    # load (see ``validate_runner``), not a failure on every review round.
    runner: str = ""
    # Mirror the verdict onto the PR head SHA as a ``kew-review`` GitHub commit
    # status branch protection on ``main`` can REQUIRE — the GitHub-native gate
    # that blocks the merge button even against a human race (#1056). Best-effort:
    # a publish failure never crashes the tick and never changes the verdict.
    publish_status: bool = True
    # Post/update one edit-in-place machine comment on the PR for every verdict
    # attestation (#1046): verdict, bound head SHA, round, findings summary,
    # acceptance-evidence digests, and a stale marker when the head has moved.
    # Best-effort: a failure never crashes the tick and never changes the verdict.
    publish_comment: bool = True
    # Review stop rule (#2112): after this many rounds, a NEEDS_CHANGES verdict
    # whose findings are all within ``stop_rule_max_severity`` is accepted as
    # READY-with-residuals — the findings are filed as one GitHub issue and the
    # node proceeds to the merge gate. A severity above the ceiling, or an
    # unknown one, keeps remediating. 0 disables the rule.
    stop_rule_after_rounds: int = 6
    # The severity ceiling the stop rule accepts as residual (#2349): "low"
    # accepts a round of lows only, "medium" also accepts mediums. Anything
    # else is a ConfigError at load. A failed acceptance criterion blocks
    # whatever the ceiling.
    stop_rule_max_severity: Literal["low", "medium"] = "medium"

    @field_validator("max_review_usd")
    @classmethod
    def validate_max_review_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_review_usd must be a finite number")
        if v < 0:
            raise ValueError("max_review_usd must be >= 0")
        return v

    @field_validator("max_remediation_usd")
    @classmethod
    def validate_max_remediation_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_remediation_usd must be a finite number")
        if v < 0:
            raise ValueError("max_remediation_usd must be >= 0")
        return v

    @field_validator("max_rounds")
    @classmethod
    def validate_max_rounds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_rounds must be >= 1")
        return v

    @field_validator("stop_rule_after_rounds")
    @classmethod
    def validate_stop_rule_after_rounds(cls, v: int) -> int:
        if v < 0:
            raise ValueError("stop_rule_after_rounds must be >= 0 (0 disables)")
        return v

    @field_validator("stop_rule_max_severity", mode="before")
    @classmethod
    def validate_stop_rule_max_severity(cls, v: Any) -> Any:
        allowed = ("low", "medium")
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in allowed:
                return normalized
        raise ValueError(
            f"stop_rule_max_severity must be one of {allowed!r}, got {v!r}: "
            "the stop rule accepts lows only, or lows and mediums"
        )

    @field_validator("runner")
    @classmethod
    def validate_runner(cls, v: str) -> str:
        if not v:
            return v
        # Lazy: review_stream imports the sandbox, which imports this module.
        from kew.review_stream import STREAM_VERDICT_RUNNERS

        if v not in STREAM_VERDICT_RUNNERS:
            supported = ", ".join(sorted(STREAM_VERDICT_RUNNERS))
            raise ValueError(
                f"{supported!r} or empty, got {v!r}: "
                "the review verdict must come from the runner's own terminal "
                f"stream event, and only {supported} emits one kew can read"
            )
        return v


class PublicationConfig(BaseModel):
    """The host-owned publication broker (W1.1).

    ``shadow`` runs the broker BESIDE a real sandboxed dispatch — extract the
    agent's work in a container, filter it, rebuild the tree in a clone the
    agent never touched — and compares that tree against the one the legacy
    path is about to publish. It never publishes anything and never blocks a
    dispatch: it exists to answer, from real work rather than fixtures, whether
    the broker would drop a file class.

    Default off, and the kill switch is this line: an operator who sees shadow
    comparisons costing time or disk turns it off in config, without a release.

    ``shadow_evidence`` bounds what a divergence leaves behind. Each non-matching
    comparison keeps its workspace — a bare clone plus the artifact — so there is
    something to look at; matching ones are removed. Keeping them forever would
    make an observability feature a disk-space incident, so only the most recent
    N are retained.
    """

    model_config = {"extra": "forbid"}

    publish: Literal["canonical", "checkout"] = "canonical"
    shadow: bool = False
    shadow_evidence: int = 10

    @field_validator("shadow_evidence")
    @classmethod
    def validate_shadow_evidence(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"shadow_evidence must not be negative, got {v!r}")
        return v


class ResolveConfig(BaseModel):
    """Merge conflict resolution options."""

    model_config = {"extra": "forbid"}

    strategy: str = "merge"
    auto: bool = False
    # The checks a resolution must pass on the resolved tree before kew pushes
    # it (#3200). A merge that applies cleanly can still break the build: on
    # 2026-10-25 a resolution reintroduced a TypeScript symbol an operator fix
    # had removed, and nothing ran the tree before it reached the branch. Each
    # runs from the root of the resolved worktree when the integration changed
    # files of its kind, under the same containment-first admission as the
    # merge queue's verify. A red check pushes nothing. A cheap guard, not the
    # merge gate (the merge verify runs the full suite), so whole-tree static
    # gates are the recommended form: a semantic conflict breaks files the
    # integration never touched. `{paths}` is optional and, when present, is
    # replaced by the changed files of the kind (root-relative, so never with a
    # `cd <subdir> &&` prefix). E.g.
    # python_checks = "cd kew && uv run ruff check . && uv run mypy src/",
    # frontend_checks = "cd frontend && npm ci && npm run check". Those install
    # dependencies, and a contained check has no network and a read-only
    # checkout: they need `dangerously_skip_containment` or a check image with
    # the dependencies built in (`[sandbox] check_image`, #3281), else they
    # hold every merge-main step.
    # Empty = that kind is not checked (logged at WARNING when it changes).
    python_checks: str = ""
    frontend_checks: str = ""

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        allowed = ("merge", "rebase")
        if v not in allowed:
            raise ValueError(f"strategy must be one of {allowed!r}, got {v!r}")
        return v


class OverlapAdvancedConfig(BaseModel):
    """Advanced overlap detection tuning."""

    model_config = {"extra": "forbid"}

    merge_context_ttl_minutes: int = 60

    @field_validator("merge_context_ttl_minutes")
    @classmethod
    def validate_ttl(cls, v: int) -> int:
        if v < 1:
            raise ValueError("merge_context_ttl_minutes must be at least 1")
        return v


class OverlapConfig(BaseModel):
    """Overlap detection and serialization for concurrent dispatch."""

    model_config = {"extra": "forbid"}

    strategy: str = "serialize"
    advanced: OverlapAdvancedConfig = OverlapAdvancedConfig()

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        allowed = ("serialize", "warn", "off")
        if v not in allowed:
            raise ValueError(f"overlap.strategy must be one of {allowed!r}, got {v!r}")
        return v


class TriggersConfig(BaseModel):
    """How many times a CI failure may be re-dispatched (``ci_fix.py``).

    The three event-trigger stubs that shared this section (label, review,
    and CI-failure dispatch) never reached a consumer and are gone. A stale
    ``kew.toml`` that still declares them now fails to load rather than
    reading as an enabled trigger nothing acts on.
    """

    model_config = {"extra": "forbid"}

    ci_failure_max_retries: int = 2


class BudgetConfig(BaseModel):
    """Spending limits for agent runs."""

    model_config = {"extra": "forbid"}

    per_issue_usd: float = 15.0
    daily_usd: float = 50.0
    weekly_usd: float = 200.0

    @field_validator("per_issue_usd")
    @classmethod
    def validate_per_issue_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("per_issue_usd must be a finite number")
        if v < 0:
            raise ValueError("per_issue_usd must be >= 0")
        return v

    @field_validator("daily_usd")
    @classmethod
    def validate_daily_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("daily_usd must be a finite number")
        if v < 0:
            raise ValueError("daily_usd must be >= 0")
        return v

    @field_validator("weekly_usd")
    @classmethod
    def validate_weekly_usd(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("weekly_usd must be a finite number")
        if v < 0:
            raise ValueError("weekly_usd must be >= 0")
        return v


class BreakerConfig(BaseModel):
    """Automatic circuit breaker policy (#245)."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    cost_anomaly_factor: float = 3.0
    min_samples: int = 5
    # Absolute backstop under the cost-anomaly threshold. Its only job is to
    # absorb noise in near-zero buckets — the bucket-relative floor below does
    # the real guarding, because one dollar value cannot serve buckets whose
    # medians span three orders of magnitude (#1116).
    min_anomaly_threshold_usd: float = 0.0
    # Bucket-relative floor: the anomaly threshold is never below
    # ``bucket_median * anomaly_floor_multiple``. At the 3.0 default it ties
    # with the 3.0 cost_anomaly_factor, so on an established bucket this term
    # never wins; its effect is confined to the under-sampled path, where it
    # arms proportionately instead of at the absolute floor (which
    # SIGTERM-killed the #992 remediation). Setting it to 0 disables it, and
    # with it the whole under-sampled path: falling back to the flat absolute
    # floor there is the #992 mode, so that bucket stays dormant instead.
    anomaly_floor_multiple: float = 3.0
    # Per-autocompaction cost allowance, as a fraction of the run-kind baseline
    # median, discounted before the cost-anomaly rule fires (#859). Keeps a long
    # run that legitimately autocompacts several times from tripping on the
    # compaction overhead alone. A contained run discounts at most
    # ``breaker.MAX_DISCOUNTED_COMPACTIONS``: its count is the agent's own
    # claim (#1462). 0 disables the discount.
    compaction_allowance_factor: float = 0.5

    @field_validator("cost_anomaly_factor")
    @classmethod
    def validate_cost_anomaly_factor(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 1.0:
            raise ValueError("cost_anomaly_factor must be > 1.0")
        return v

    @field_validator("min_samples")
    @classmethod
    def validate_min_samples(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_samples must be >= 1")
        return v

    @field_validator("min_anomaly_threshold_usd")
    @classmethod
    def validate_min_anomaly_threshold_usd(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("min_anomaly_threshold_usd must be >= 0")
        return v

    @field_validator("compaction_allowance_factor")
    @classmethod
    def validate_compaction_allowance_factor(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("compaction_allowance_factor must be >= 0")
        return v

    @field_validator("anomaly_floor_multiple")
    @classmethod
    def validate_anomaly_floor_multiple(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("anomaly_floor_multiple must be >= 0")
        return v


class ApprovalConfig(BaseModel):
    """Pre-dispatch approval gate for high-estimated-cost runs.

    threshold_usd unset/None means the feature is off (default).
    """

    model_config = {"extra": "forbid"}

    threshold_usd: float | None = None
    label: str = "needs-approval"
    approvers: list[str] = []
    """Optional allowlist of GitHub logins permitted to approve/deny remotely.
    Empty (default) = `kew approve`/`kew deny` admit any actor on the host;
    only the `/approve` comment workflow checks repo write access. Prompt
    profile decisions do not share that rule and are all refused while the
    list is empty."""

    @field_validator("threshold_usd")
    @classmethod
    def validate_threshold_usd(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("threshold_usd must be a finite number")
        if v < 0:
            raise ValueError("threshold_usd must be >= 0")
        return v


class EpicConfig(BaseModel):
    """Epic DAG driver settings ([epic] in kew.toml)."""

    model_config = {"extra": "forbid"}

    max_parallel: int | None = None  # None => config.parallel.max_workers
    stall_days: int = 3
    # Expected seconds between advances when something OUTSIDE kew drives the
    # loop — a cron/systemd timer or a wrapper script calling `kew epic advance`
    # (#2297). `kew epic run --interval N` records its own real cadence; a
    # one-shot advance has none, which is why every drive used to record a NULL
    # interval and `driver_liveness` could never say "gone". Recorded in its own
    # `epic_driver_state.expected_interval_s` column — refreshed by every drive
    # that supplies it, so editing this value takes effect at the next drive,
    # and never folded into the measured cadence, so a reading can report a
    # guess AS a guess (`cadence_source="expected"`).
    driver_expected_interval_seconds: int = 300
    approvers: list[str] = []  # falls back to approval.approvers if empty
    # Base path; keyed per epic. None => <logging.dir>/epic-driver.lock
    lock_path: str | None = None
    # When True, a tick whose node has a red (definitively failed) PR CI may
    # dispatch a bounded ci-fix run (reusing the ci_green loop machinery)
    # instead of parking for a human. Off by default: recovery only engages
    # when an operator opts in. Infra-dead CI (zero executed steps across jobs
    # — billing failure / runner-offline) is never fixable and parks the node
    # with an infra diagnostic instead of burning an agent run (F5).
    auto_ci_fix: bool = False
    # Serial merge queue (#2148). When True, every candidate the merge pass
    # would merge is first verified as the tree it will PRODUCE: the base branch
    # is merged into the READY head in a scratch clone and ``commands.verify_loop``
    # runs on that combined tree, one verification at a time repo-wide. Two
    # siblings each green alone can be red together, which no per-PR check can
    # see. ``commands.verify_loop`` is the only key read: the config REFUSES to
    # load without it rather than falling back to ``commands.ci``, which is
    # usually a narrower gate than the one a combined tree has to pass (#2184).
    # The command executes the PR's own files, so it runs under the same
    # containment-first admission the loop gate resolves (#1444) — the image
    # mapped for ``agent.runner``, else the stock ``sandbox.image``, with no
    # network and none of the host's environment. ``[sandbox]
    # dangerously_skip_containment`` is NOT needed; where it is unset and no
    # image resolves, every candidate is held ``merge-verify-uncontained``
    # rather than reaching the host. Off by default because engaging it is an
    # operator decision: it additionally needs ``[review] adversarial``, whose
    # stage is the only thing that remediates a red combined tree.
    merge_queue: bool = False
    # Paths whose content DEFINES what the verify command checks (#2184). The
    # queue runs an operator-configured command, but the files that command
    # executes — the gate scripts themselves, the build/test configuration they
    # read, the container they build — live in the tree the PR may edit, so a PR
    # that edits one is a PR that can decide what proves it. Before any command
    # runs, the queue intersects `git diff --name-only <base>...<head>` with this
    # list and HOLDs a node that hits it (`merge_gate_touched`, released by
    # `kew epic unhold` once a human has read the diff). Patterns match with
    # `PurePosixPath.full_match`; a trailing `/` means "this directory and
    # everything under it". Empty disables the check — an explicit operator
    # decision to let a PR redefine its own gates.
    #
    # Every entry naming a file or a tool-owned directory is `**/`-anchored,
    # because a gate definition lives wherever the project it gates lives: this
    # repo keeps the pinned semgrep ruleset at `kew/.semgrep/pinned.yml`, the
    # xenon exclude list at `kew/.xenon-excludes`, gate scripts under both
    # `scripts/` and `kew/scripts/`, and lockfiles at both `uv.lock` and
    # `kew/uv.lock`. The root-anchored spellings matched none of those, so a PR
    # that rewrote the rules it is scanned by — or added itself to the
    # complexity exclude list — was verified by a gate it authored and merged
    # unheld. The lockfiles are here for the same reason as the gate scripts:
    # they pin the ruff / bandit / semgrep / pytest versions the gates ARE.
    # ``.github/workflows/`` and ``kew.toml`` stay root-anchored: those are the
    # only copies anything reads.
    #
    # The list covers EVERY gate's own configuration, not just the ones a Python
    # project notices first (#2192): a scanner's allowlist is as much a gate
    # definition as the script that invokes it. `.gitleaks.toml` /
    # `.gitleaksignore` are the secrets gate's — a PR that allowlists away its
    # own leaked credential is otherwise scanned by rules it wrote; `mkdocs.yml`
    # is the docs gate's (its `exclude_docs` decides what may reach a published
    # site); `tsconfig*.json` is what `tsc` IS for the frontend gate;
    # `.dockerignore` decides the build context of the images `docker/` holds.
    #
    # The generic `**/*.config.*` entry stands in for every framework config a
    # gate DISCOVERS instead of being pointed at: `vite.config.*` is what
    # `vite build` and `vitest run` ARE, and `svelte.config.*` decides what
    # `svelte-check` checks (`svelte-kit sync` generates the
    # `.svelte-kit/tsconfig.json` both tracked `tsconfig.json`s extend) and,
    # through `prerender.handleHttpError`, which broken links the site build is
    # allowed to survive. Naming those two by hand left the rest of the class
    # open — `vitest.config.ts` alone would have overridden the test config the
    # frontend gate runs under.
    #
    # The same blindness had a bigger Python half (#2192). A gate config is
    # dangerous when it need not EXIST yet: a PR that ADDS a root `.ruff.toml`,
    # `ruff.toml`, `pytest.ini`, `tox.ini` or `setup.cfg` outranks the
    # `pyproject.toml` those tools would otherwise read, so `select = []` in a
    # file the PR itself introduced turns the lint gate off and the queue
    # verifies the PR green. `.python-version` and `uv.toml` are the same move
    # one level down — they choose the interpreter and the resolution the gates
    # run under — and `.npmrc` / `.nvmrc` are its JS spelling. Every entry below
    # is a file the tool finds by convention, for a tool this repo's runners
    # actually invoke; none of them has to be present for the hole to open.
    #
    # A scanner has a second conventional file, and pinning its RULES does not
    # close it (#2192): `.semgrepignore` and `.trivyignore` do not choose what
    # the scanner looks for, they choose what it is allowed to look AT, and
    # both are read from the working directory whatever `--config` was passed.
    # `**/.semgrep/` covered the pinned ruleset and read as if the SAST gate
    # were handled, while a PR adding a one-line `.semgrepignore` scanned
    # nothing, was not held, and was attested `result: pass`. `trivy.y*ml` is
    # here for the same reason as its ignore list: the container scan passes
    # no `--config` either, so a trivy config the branch adds is the one the
    # scanner obeys.
    # ``tests/test_merge_gate_paths.py`` keeps the list honest against this
    # repo's own tree, naming a gate definition by the gate runners that read
    # it — or, for a config the tool DISCOVERS rather than is pointed at, by
    # the tool those runners invoke — rather than by restating this list.
    #
    # `**/ratchets/` holds the ratchet ledgers the policy tests compare the
    # tree against (#2493) — a ledger is as much what the gate IS as the test
    # reading it, so a PR that raises its own allowance is held. They live
    # outside `tests/` for the same reason.
    merge_gate_paths: list[str] = [
        "**/scripts/",
        "**/ratchets/",
        "**/pyproject.toml",
        "**/setup.cfg",
        "**/setup.py",
        "**/tox.ini",
        "**/pytest.ini",
        "**/conftest.py",
        "**/ruff.toml",
        "**/.ruff.toml",
        "**/mypy.ini",
        "**/.mypy.ini",
        "**/.coveragerc",
        "**/.importlinter",
        "**/.bandit",
        "**/uv.lock",
        "**/uv.toml",
        "**/.python-version",
        "**/.semgrep/",
        "**/.semgrepignore",
        "**/trivy.y*ml",
        "**/.trivyignore",
        "**/.xenon-excludes",
        "**/.gitleaks.toml",
        "**/.gitleaksignore",
        # `export-ignore` lines decide what `git archive` leaves out of the
        # public snapshot that scripts/make-public-snapshot.sh then checks, so
        # a PR that added one would be checked by a snapshot it shaped.
        "**/.gitattributes",
        "**/mkdocs.y*ml",
        "**/docker/",
        "**/.dockerignore",
        "**/package*.json",
        "**/.npmrc",
        "**/.nvmrc",
        "**/tsconfig*.json",
        "**/*.config.*",
        # A Caddyfile sets a site's security headers, its Content-Security-
        # Policy among them, and a check that serves a build with those headers
        # is defined by it: a PR that loosened it would be checked against the
        # policy it wrote.
        "**/Caddyfile",
        # The demo world (#3141): the demo browser checks take every ref and
        # expected figure from its files — `manifest.json` names what the
        # header must count and which tests the refused PR failed on — so a
        # PR that rewrote the world would be checked against expectations it
        # wrote. The leak scan's allowlist clears findings the way
        # `.gitleaksignore` does, and the scanner beside it is the gate itself.
        "**/demo/world/",
        "**/demo/leak-scan*",
        # The demo checks also decide whether a served build carries the real
        # build history by matching its PR links against the repository
        # `public-repo.json` names (#3207), so a PR that renamed it would pick
        # which assertions run against its own build.
        "**/demo/public-repo.json",
        ".github/workflows/",
        "kew.toml",
    ]
    # Gates the contained combined-tree verification CANNOT run — a docker
    # build, anything needing the network — named so the queue's green is honest
    # about what it did not prove (#2184). The names are recorded on every
    # merge-verify attestation with the reason, and exported to the verify
    # command as `KEW_MERGE_QUEUE_UNCONTAINED_GATES` (comma-separated) so a
    # command that can skip them does. They are expected to be covered by CI,
    # which the merge pass gates on separately.
    merge_queue_uncontained_gates: list[str] = []

    @field_validator("merge_gate_paths", "merge_queue_uncontained_gates")
    @classmethod
    def _validate_named_entries(cls, v: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in v]
        if any(not entry for entry in cleaned):
            raise ValueError("epic list entries must not be blank")
        return cleaned

    @field_validator("stall_days")
    @classmethod
    def _validate_stall_days(cls, v: int) -> int:
        if v < 1:
            raise ValueError("epic.stall_days must be >= 1")
        return v

    @field_validator("driver_expected_interval_seconds")
    @classmethod
    def _validate_driver_expected_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("epic.driver_expected_interval_seconds must be >= 1")
        return v

    @field_validator("max_parallel")
    @classmethod
    def _validate_max_parallel(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("epic.max_parallel must be >= 1")
        return v


class DriveLoadStep(BaseModel):
    """One ``[drive.load]`` threshold: while the load average is above
    ``above``, the drive admits under at most ``max_parallel`` (#2616)."""

    model_config = {"extra": "forbid"}

    above: float
    max_parallel: int

    @field_validator("max_parallel")
    @classmethod
    def _validate_max_parallel(cls, v: int) -> int:
        if v < 0:
            raise ValueError("drive.load thresholds max_parallel must be >= 0")
        return v


class DriveLoadConfig(BaseModel):
    """The load gate (``[drive.load]``): thresholds that lower the drive's cap
    while this machine's one-minute load average is above them (#2616)."""

    model_config = {"extra": "forbid"}

    thresholds: list[DriveLoadStep] = []


class ReportConfig(BaseModel):
    """Reserved evidence-pack export settings (``[report]`` in kew.toml)."""

    model_config = {"extra": "forbid"}


class RunReportsConfig(BaseModel):
    """Per-run decision-intelligence report export ([run_reports] in kew.toml).

    When enabled, every run that reaches a terminal state writes one small
    markdown report (kew-authored structured frontmatter only) into
    ``sink_path`` — a machine-level location shared by every repo kew
    orchestrates, so cross-project retrieval has a single corpus. Default off:
    no behavior change until the operator opts in. Export is best-effort and
    never fails a run. Named ``run_reports`` (not ``reports``) because
    ``[report]`` already carries evidence-pack branding. Spec:
    docs/design/2026-08-21-run-report-sink-design.md (#1578).
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    sink_path: str = "~/.local/share/kew/runs"

    @field_validator("sink_path")
    @classmethod
    def _validate_sink_path(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_reports.sink_path must not be empty")
        return v


class PricingConfig(BaseModel):
    """Custom model pricing overrides for budget tracking."""

    model_config = {"extra": "forbid"}

    overrides: dict[str, list[float]] = {}


_LIFECYCLE_ENTRIES = TypeAdapter(dict[str, LifecycleEntry])


class ModelEolConfig(BaseModel):
    """Offline model lifecycle policy over the curated registry (#1490).

    There is no feed and no fetch: the wheel-packaged curated registry is the
    runtime source. ``snapshot_path`` is the *explicit project override* — set
    it to a strict registry document to replace the packaged one; leave it
    unset (the default) and the packaged registry is used.
    """

    model_config = {"extra": "forbid"}

    snapshot_path: str = ""

    # Lifecycle rows hydrated by the loader from the curated registry.
    # Loader-only (#1679): carried on the config object, rejected when an
    # operator writes it in kew.toml.
    _snapshot_models: dict[str, LifecycleEntry] = PrivateAttr(default_factory=dict)

    @property
    def snapshot_models(self) -> dict[str, LifecycleEntry]:
        """Curated lifecycle rows, keyed by model id."""
        return self._snapshot_models

    @snapshot_models.setter
    def snapshot_models(self, value: dict[str, LifecycleEntry]) -> None:
        self._snapshot_models = _LIFECYCLE_ENTRIES.validate_python(value)


class BillingConfig(BaseModel):
    """How the API-equivalent token cost relates to actual spend.

    kew computes cost from published per-token API pricing — an *API-equivalent
    list price*. What you actually pay depends on the channel: a metered API key
    (list price = real spend), a flat subscription (≈ included), or local
    inference (≈ free, detected per-run from the model). This records the
    account-level channel so the UI can frame the number honestly.
    """

    model_config = {"extra": "forbid"}

    mode: Literal["metered", "subscription"] = "metered"
    plan_label: str = ""  # e.g. "Claude Max" — shown in the value banner
    plan_monthly_usd: float = Field(default=0.0, ge=0)


class FlightRecorderConfig(BaseModel):
    """Flight Recorder capture + local serve settings."""

    model_config = {"extra": "forbid"}

    # Inert since I16: the per-step git-tree capture this toggled is gone, and
    # what a run produced is now read from the sealed artifact instead (see
    # kew.flight.evidence). The key stays accepted because `extra: forbid`
    # would otherwise reject an existing config that switched it on; dropping
    # it is a config-contract change for a release that can announce one.
    snapshots: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    expose: bool = False  # opt-in to bind a non-loopback host (see model validator)
    # Public origins to add to the write Origin-allowlist when fronting kew with a
    # TLS reverse proxy. The browser sends its public Origin (e.g.
    # "https://kew.example.com", no port) which never matches the internal
    # bind_host:port — without this, writes silently 403. Reads are unaffected.
    extra_origins: list[str] = Field(default_factory=list)
    # Addresses of the reverse proxies kew trusts: the only peers whose
    # X-Forwarded-For/-Proto are believed, and — because kew never terminates
    # TLS — the operator's declaration that a TLS terminator is in front.
    # `kew serve --expose` is refused while this is empty (see
    # kew.serve.security.expose_transport_refusal).
    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, v: list[str]) -> list[str]:
        for entry in v:
            if entry == "*":
                raise ValueError(
                    "[flight_recorder].trusted_proxies entry '*' would trust "
                    "forwarded headers from every peer, defeating the allowlist — "
                    "name the proxy addresses instead"
                )
            try:
                # strict=True: uvicorn parses these strictly too, and keeps a
                # host-bits entry ("10.0.0.5/24") as an opaque literal that
                # matches no peer — an allowlist that is silently inert. Fail
                # here instead, naming the fix.
                network = ipaddress.ip_network(entry, strict=True)
            except ValueError as exc:
                raise ValueError(
                    f"[flight_recorder].trusted_proxies entry {entry!r} must be an IP "
                    "address or a CIDR network with no host bits (e.g. '127.0.0.1' "
                    "or '10.0.0.0/24', not '10.0.0.5/24'); hostnames are never resolved"
                ) from exc
            if network.prefixlen == 0:
                raise ValueError(
                    f"[flight_recorder].trusted_proxies entry {entry!r} trusts every "
                    "peer, the same as '*' — name the proxy addresses instead"
                )
        return v

    @field_validator("extra_origins")
    @classmethod
    def _validate_extra_origins(cls, v: list[str]) -> list[str]:
        from urllib.parse import urlsplit

        for origin in v:
            parts = urlsplit(origin)
            if (
                parts.scheme not in ("http", "https")
                or not parts.netloc
                or parts.path
                or parts.query
                or parts.fragment
            ):
                raise ValueError(
                    f"[flight_recorder].extra_origins entry {origin!r} must be a bare "
                    "origin like 'https://kew.example.com' (scheme + host[:port], "
                    "no path/query/fragment)"
                )
        return v

    @model_validator(mode="after")
    def _validate_exposure(self) -> "FlightRecorderConfig":
        if _is_loopback(self.host) or self.expose:
            return self
        raise ValueError(
            f"[flight_recorder].host={self.host!r} is non-loopback; "
            "set [flight_recorder].expose = true to bind it intentionally."
        )


class IngestConfig(BaseModel):
    """Cross-runner usage ingestion settings (#256).

    `kew ingest serve` exposes a loopback-only OTLP/HTTP receiver; the cap
    is an alert threshold, never an enforced limit.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=4318, ge=1, le=65535)
    cap_usd: float = Field(default=0.0, ge=0.0)  # 0 = no cap alert
    combine_with_dispatch: bool = False

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v == "localhost":
            return v
        try:
            if ipaddress.ip_address(v).is_loopback:
                return v
        except ValueError:
            pass
        raise ValueError(
            "[ingest].host must be loopback (127.0.0.0/8, localhost, or ::1)"
        )


# Test-only override for the default log dir. Left None in production; the test
# suite sets it (see tests/conftest.py) so a *bare* ``DispatchConfig()`` never
# falls back to the CWD-relative ``./logs/dispatch`` — i.e. the repo-local
# dispatch.db — and poisons the checkout for the next cross-schema run
# (SFD-134 / kew #784). It is a *default*, not an override: a kew.toml value or
# an explicit ``logging.dir`` still wins, since the factory only fires when the
# field is unset.
_DEFAULT_LOG_DIR_OVERRIDE: str | None = None


def _default_log_dir() -> str:
    """Default log dir, honoring KEW_LOG_DIR.

    load_config already layers this env var over any kew.toml value via
    _ENV_OVERRIDES, but a *bare* ``DispatchConfig()`` (built directly, e.g. in
    tests or ad-hoc call sites) would otherwise ignore them. Reading the env
    here keeps every construction path pointed at the same log dir.
    """
    if _DEFAULT_LOG_DIR_OVERRIDE is not None:
        return _DEFAULT_LOG_DIR_OVERRIDE
    return os.environ.get("KEW_LOG_DIR") or "./logs/dispatch"


class LoggingConfig(BaseModel):
    """Where run logs live and how long they are kept.

    ``level`` used to sit here; nothing ever read it, so verbosity is not a
    kew.toml setting.
    """

    model_config = {"extra": "forbid"}

    dir: str = Field(default_factory=_default_log_dir)
    retention_days: int = 0  # 0 = keep forever

    @field_validator("retention_days")
    @classmethod
    def validate_retention_days(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retention_days must be >= 0 (0 = keep forever)")
        return v


class HumanReviewConfig(BaseModel):
    """The one human-review policy ([human_review], I13).

    Three numbers and an allowlist decide when a publication stops for a
    person, and there is exactly one place to change them.

    * ``max_pending_reviews`` is *capacity*: how many publications may sit
      awaiting a human at once. It is counted per canonical GitHub delivery
      repository, never across unrelated repositories — a busy repo must not
      be able to starve a quiet one.
    * ``max_diff_lines`` counts additions **plus** deletions between the
      trusted base and the artifact. A deletion is not the harmless direction.
    * ``max_paths`` counts distinct paths the artifact touches.
    * ``approvers`` is the terminal-judgment allowlist. Empty falls back to
      ``[approval].approvers``; empty there too means any authenticated actor
      may confirm, which is the stance the spend gate already takes.

    Both thresholds are *inclusive*: exactly ``max_diff_lines`` changed lines
    or exactly ``max_paths`` paths pass unattended; one more stops for a human.
    Zero is legal and means "hold everything" — the loud end of the range,
    never a disabled check.
    """

    model_config = {"extra": "forbid"}

    max_pending_reviews: int = 3
    max_diff_lines: int = 500
    max_paths: int = 20
    approvers: list[str] = []

    @field_validator("max_pending_reviews", "max_diff_lines", "max_paths")
    @classmethod
    def _validate_bound(cls, v: int, info: ValidationInfo) -> int:
        if v < 0:
            raise ValueError(
                f"{info.field_name} must be >= 0 (0 holds every publication)"
            )
        return v


class AuditConfig(BaseModel):
    """Audit-chain verification policy (trust hardening, T1/#912)."""

    model_config = {"extra": "forbid"}

    #: How many events the live chain may trail the latest anchor's count before
    #: ``kew audit verify --anchors`` fails closed. In honest operation the live
    #: DB is at or ahead of the newest anchor (anchors trail the head), so any
    #: positive lag beyond this window signals truncation or a stale/lagging
    #: anchor. 0 = the live chain must be at least as long as the latest anchor.
    staleness_window: int = 0

    #: How old (in days) the newest anchor may be before ``kew doctor`` warns
    #: that the chain is unwitnessed (#1408). ``staleness_window`` measures the
    #: chain against the anchors that exist; this measures whether anchors are
    #: still being *taken* — a silently dead anchor workflow leaves an intact,
    #: verifiable, and entirely unwitnessed chain. ``0`` disables the alarm.
    anchor_max_age_days: int = 7

    @field_validator("staleness_window")
    @classmethod
    def _validate_staleness_window(cls, v: int) -> int:
        if v < 0:
            raise ValueError("staleness_window must be non-negative")
        return v

    @field_validator("anchor_max_age_days")
    @classmethod
    def _validate_anchor_max_age_days(cls, v: int) -> int:
        if v < 0:
            raise ValueError("anchor_max_age_days must be non-negative (0 = off)")
        return v


class AlertingConfig(BaseModel):
    """Where run outcomes are announced ([alerting]).

    The spend caps kew actually enforces live in :class:`BudgetConfig`. Two
    dead threshold knobs here used to shadow them and were read by nothing;
    a second set of numbers that looked like limits but bound nothing is
    worse than no numbers at all.
    """

    model_config = {"extra": "forbid"}

    webhook_url: str = ""
    notify_on_failure: bool = True
    notify_on_success: bool = False
    notify_on_hold: bool = True


class DelegationConfig(BaseModel):
    """assign-to-kew delegation trigger settings ([delegation], D1/#920).

    A human assigns a Linear issue to the ``kew`` member (or adds the configured
    label) and ``kew delegate watch`` claims → dispatches it as a one-node
    ephemeral epic, reusing the epic claim → dispatch → close path. ``assignee``
    is the kew member's Linear name, resolved to a member id at use (D2); it has
    no safe default, so it is required only when the watcher actually runs, not
    at config load. ``poll_interval_seconds`` is the throttled Linear poll floor;
    ``fast_interval_seconds`` is the local ``tracker_events`` read cadence.

    ``refusal_cooldown_seconds`` damps the budget-refusal re-claim loop: a label
    triggered item that a budget preflight refuses is unclaimed but keeps its
    trigger label, so the poll floor re-surfaces it every ``poll_interval_seconds``.
    Without a cooldown that re-claims → refuses → comments every poll forever
    while over budget; within the cooldown the watcher skips the re-claim
    entirely (no In-Progress flip, no refusal row, no comment), so a raised
    budget is still retried after at most ``refusal_cooldown_seconds``. ``0`` disables
    the damping.
    """

    model_config = {"extra": "forbid"}

    assignee: str = ""
    label: str = "kew:ready"
    poll_interval_seconds: int = 20
    fast_interval_seconds: int = 2
    refusal_cooldown_seconds: int = 300

    @field_validator("poll_interval_seconds")
    @classmethod
    def _validate_poll_interval_seconds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("delegation.poll_interval_seconds must be at least 1")
        return v

    @field_validator("refusal_cooldown_seconds")
    @classmethod
    def _validate_refusal_cooldown_seconds(cls, v: int) -> int:
        if v < 0:
            raise ValueError("delegation.refusal_cooldown_seconds must be >= 0")
        return v

    @field_validator("fast_interval_seconds")
    @classmethod
    def _validate_fast_interval_seconds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("delegation.fast_interval_seconds must be at least 1")
        return v


class SwitchyardSidecarConfig(BaseModel):
    """Explicit per-attempt Docker sidecar inputs."""

    model_config = {"extra": "forbid"}

    enabled: bool = False
    image: str
    egress_network: str

    @field_validator("enabled", mode="before")
    @classmethod
    def _require_boolean(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("sidecar enabled must be a Boolean")
        return value

    @field_validator("image", "egress_network")
    @classmethod
    def _validate_resource(cls, value: str, info: Any) -> str:
        if (
            not value
            or value != value.strip()
            or value.startswith("-")
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError(f"sidecar {info.field_name} must be one Docker resource")
        return value


class SwitchyardConfig(BaseModel):
    """Fail-closed configuration for a Switchyard transport endpoint."""

    model_config = {"extra": "forbid"}

    endpoint: str = ""
    route: str = ""
    config_path: Path | None = None
    expected_version: str = ""
    compatibility_record_path: Path | None = None
    managed: bool = False
    binary: Path | None = None
    binary_sha256: str = ""
    credential_env: tuple[str, ...] = ()
    sidecar: SwitchyardSidecarConfig | None = None
    startup_timeout_seconds: int = Field(default=20, gt=0)
    fallback: Literal["refuse"] = "refuse"

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return _validate_switchyard_endpoint(value)

    @field_validator("credential_env")
    @classmethod
    def _validate_credential_env(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"}
        if len(value) != len(set(value)):
            raise ValueError("switchyard credential_env must not contain duplicates")
        if any(name not in allowed for name in value):
            raise ValueError("switchyard credential_env contains an unsupported name")
        return value

    @model_validator(mode="after")
    def _separate_sidecar_from_host_supervisor(self) -> "SwitchyardConfig":
        if self.managed and self.sidecar is not None:
            raise ValueError("managed switchyard and sidecar are separate modes")
        return self


class CapabilityPinConfig(BaseModel):
    """Minimum kew build capabilities required by a gated config section."""

    model_config = {"extra": "forbid"}

    min_kew_version: str | None = None
    min_schema_version: int | None = Field(default=None, ge=0)

    @field_validator("min_kew_version")
    @classmethod
    def _validate_min_kew_version(cls, value: str | None) -> str | None:
        if value is not None and not _version_tuple(value):
            raise ValueError("min_kew_version must start with a numeric version")
        return value


class InferenceTransportConfig(BaseModel):
    """Inference transport selection; direct mode preserves existing behavior."""

    model_config = {"extra": "forbid"}

    mode: Literal["direct", "switchyard"] = "direct"
    # Adaptive routing is a rollout-gated experiment.  It is intentionally
    # separate from selecting Switchyard itself so a route edit cannot enable
    # classifier traffic by accident.
    allow_adaptive: bool = False
    capability: CapabilityPinConfig = Field(default_factory=CapabilityPinConfig)
    switchyard: SwitchyardConfig = Field(default_factory=SwitchyardConfig)

    @model_validator(mode="after")
    def _validate_switchyard(self) -> "InferenceTransportConfig":
        _validate_switchyard_mode(self.mode, self.switchyard)
        validate_capability_pin(self.capability)
        return self


def _validate_switchyard_endpoint(value: str) -> str:
    if not value:
        return value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise ValueError("switchyard endpoint is malformed") from exc
    if not _is_endpoint_origin(parts, host):
        raise ValueError(
            "switchyard endpoint must be an HTTP(S) origin without "
            "userinfo, path, query, or fragment"
        )
    assert host is not None
    if parts.scheme == "http" and not _is_literal_loopback(host):
        raise ValueError(
            "switchyard endpoint permits HTTP only for a literal loopback IP"
        )
    return value


def _is_endpoint_origin(parts: Any, host: str | None) -> bool:
    return bool(
        parts.scheme in ("http", "https")
        and host
        and parts.username is None
        and parts.password is None
        and not parts.path
        and not parts.query
        and not parts.fragment
    )


def _validate_switchyard_mode(
    mode: Literal["direct", "switchyard"], switchyard: SwitchyardConfig
) -> None:
    sidecar_enabled = bool(switchyard.sidecar and switchyard.sidecar.enabled)
    if mode == "direct":
        if sidecar_enabled:
            raise ValueError("sidecar switchyard requires switchyard transport mode")
        return
    _validate_selected_switchyard(switchyard, sidecar_enabled=sidecar_enabled)


def _validate_selected_switchyard(
    switchyard: SwitchyardConfig, *, sidecar_enabled: bool
) -> None:
    _validate_selected_switchyard_endpoint(switchyard, sidecar_enabled)
    _validate_selected_switchyard_identity(switchyard)
    _validate_selected_switchyard_lifecycle(switchyard, sidecar_enabled)


def _validate_selected_switchyard_endpoint(
    switchyard: SwitchyardConfig, sidecar_enabled: bool
) -> None:
    if not sidecar_enabled and not switchyard.endpoint:
        raise ValueError("switchyard endpoint is required")
    if sidecar_enabled and switchyard.endpoint:
        raise ValueError("sidecar switchyard endpoint must be derived per attempt")


def _validate_selected_switchyard_identity(switchyard: SwitchyardConfig) -> None:
    if not switchyard.route.strip():
        raise ValueError("switchyard route must be non-empty")
    if not switchyard.expected_version.strip() and (
        switchyard.compatibility_record_path is None
    ):
        raise ValueError(
            "switchyard expected_version or compatibility_record_path is required"
        )
    if switchyard.compatibility_record_path is not None:
        _validate_regular_file(
            switchyard.compatibility_record_path, "compatibility_record_path"
        )


def _validate_selected_switchyard_lifecycle(
    switchyard: SwitchyardConfig, sidecar_enabled: bool
) -> None:
    if switchyard.managed:
        _validate_managed_switchyard(switchyard)
    if sidecar_enabled:
        _validate_sidecar_switchyard(switchyard)


def _validate_sidecar_switchyard(switchyard: SwitchyardConfig) -> None:
    if switchyard.config_path is None:
        raise ValueError("sidecar switchyard config_path is required")
    if not switchyard.expected_version.strip():
        raise ValueError("sidecar switchyard expected_version is required")
    _validate_regular_file(switchyard.config_path, "config_path")


def _validate_managed_switchyard(switchyard: SwitchyardConfig) -> None:
    if switchyard.config_path is None:
        raise ValueError("managed switchyard config_path is required")
    _validate_regular_file(switchyard.config_path, "config_path")
    if switchyard.binary is None:
        raise ValueError("managed switchyard binary is required")
    _validate_regular_file(switchyard.binary, "binary")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", switchyard.binary_sha256):
        raise ValueError("managed switchyard binary_sha256 must be a SHA-256 digest")


def _is_literal_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_regular_file(path: Path, field_name: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"switchyard {field_name} must be absolute")
    current = Path(path.anchor)
    try:
        metadata = current.lstat()
        for component in path.parts[1:]:
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(
                    f"switchyard {field_name} path components must not be symlinks"
                )
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"switchyard {field_name} must be a regular file")
    except OSError:
        raise ValueError(f"switchyard {field_name} must be a regular file") from None


def _runtime_kew_version() -> str:
    from kew import __version__

    return __version__


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in value.split("."):
        match = re.match(r"\d+", segment)
        if match is None:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def _version_is_less(current: str, required: str) -> bool:
    current_parts = _version_tuple(current)
    required_parts = _version_tuple(required)
    width = max(len(current_parts), len(required_parts))
    return current_parts + (0,) * (width - len(current_parts)) < required_parts + (
        0,
    ) * (width - len(required_parts))


def validate_capability_pin(pin: CapabilityPinConfig) -> None:
    """Fail closed when a gated section requires a newer kew capability."""
    current = _runtime_kew_version()
    if pin.min_kew_version and _version_is_less(current, pin.min_kew_version):
        raise ValueError(
            f"inference_transport capability requires kew >= "
            f"{pin.min_kew_version}; running kew is {current}"
        )
    if pin.min_schema_version is not None:
        from kew.run_store import CURRENT_SCHEMA_VERSION

        if pin.min_schema_version > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"inference_transport capability requires schema >= "
                f"{pin.min_schema_version}; this kew supports schema "
                f"{CURRENT_SCHEMA_VERSION}"
            )


class TriageConfig(BaseModel):
    """Triage-station policy ([triage], #2293) — shadow mode.

    In this node every triage outcome is a **proposal**. Nothing here can make
    the station act on a tracker: it applies no label, writes no comment, never
    marks an item ``ready`` and never closes one. The only thing that acts is
    ``kew triage decide``, run by a person.

    * ``enabled`` gates the station entirely. Off by default — a classifier that
      runs before an operator asked for it is a surprise, not a feature.
    * ``intake_label`` is the label an operator puts on an item to say "triage
      this". kew only READS it.
    * ``backend`` is where the classifier runs: ``local`` is the
      OpenAI-compatible endpoint in ``[agent.lemonade]``; ``runner`` means a
      registered runner. ``fallback_backend`` is tried when the first is
      unreachable, and when it too is unreachable the outcome is
      ``unavailable`` — never a dispatch.
    * ``min_confidence`` is the floor under which a decision is a person's to
      make. ``human_types`` are the classifications that are a person's to make
      whatever the confidence.
    * ``dedupe_candidates`` caps how many duplicate candidates the host looks
      up; ``max_body_chars`` caps the body handed to the model;
      ``max_usd_per_item`` caps one item's classification spend.
    * ``skip_epic_nodes`` leaves items an epic already planned alone: their
      plan came from the epic, and re-triaging them proposes nothing new.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    intake_label: str = "kew:triage"
    backend: str = "local"
    model: str = ""
    fallback_backend: str = "runner"
    min_confidence: float = 0.8
    human_types: list[str] = ["security", "question"]
    dedupe_candidates: int = 10
    max_body_chars: int = 20000
    max_usd_per_item: float = 0.50
    skip_epic_nodes: bool = True

    @field_validator("backend", "fallback_backend")
    @classmethod
    def _validate_backend(cls, v: str) -> str:
        if v not in TRIAGE_BACKENDS:
            raise ValueError(f"must be one of {sorted(TRIAGE_BACKENDS)}, not {v!r}")
        return v

    @field_validator("min_confidence")
    @classmethod
    def _validate_min_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")
        return v

    @field_validator("human_types")
    @classmethod
    def _validate_human_types(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - TRIAGE_TYPES)
        if unknown:
            raise ValueError(
                f"human_types must name triage types {sorted(TRIAGE_TYPES)};"
                f" unknown: {unknown}"
            )
        return v

    @field_validator("dedupe_candidates", "max_body_chars")
    @classmethod
    def _validate_cap(cls, v: int, info: ValidationInfo) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return v

    @field_validator("max_usd_per_item")
    @classmethod
    def _validate_spend_cap(cls, v: float) -> float:
        if v < 0:
            raise ValueError("max_usd_per_item must be >= 0")
        return v


class DriveMergeConfig(BaseModel):
    """``[drive.merge]`` — the operator's standing merge rule for ``kew drive``.

    With ``enabled``, the drive merges a READY PR on its own when its net paths
    avoid every gate class and protected publication class, and it is not a UI
    change without a recorded browser check (:mod:`kew.drive_merge`). The rule
    is SIGNED: ``signer`` is the operator's GitHub login, and the drive records
    the rule on the audit chain the first time it reads it, and again whenever
    it changes — and only while the login ``gh`` runs as is the signer. A rule
    changed under any other login is refused, and nothing merges.
    """

    model_config = {"extra": "forbid"}

    #: Off by default: every READY PR waits for a person.
    enabled: bool = False

    #: The GitHub login that signs the rule; required once ``enabled``.
    signer: str = ""

    #: Globs (``PurePosixPath.full_match``; a trailing ``/`` means everything
    #: under it) naming a UI change. A UI change merges only over a passing
    #: ``kew verify`` of its own head that ran the browser check.
    ui_paths: list[str] = ["frontend/"]

    #: When ``[verify] host`` cannot be reached, run the merge verification on
    #: the operator machine instead. Off by default: an unreachable host
    #: pauses merges until it answers again (#2619).
    local_fallback: bool = False

    #: Epics the standing rule never merges: their READY PRs stay on Needs-you
    #: for a person, with the reason recorded once per head. For an epic
    #: another session merges by hand (a browser probe the rule cannot see).
    #: Matched trimmed, case-insensitively, with or without ``epic:``.
    exclude_epics: list[str] = []

    #: What stands in for the tests at merge time. ``"github"`` (the default)
    #: needs green GitHub checks on the head as well as the passing ``kew
    #: verify`` of the candidate the drive runs before every merge;
    #: ``"verify"`` accepts that passing verify alone, for when GitHub Actions
    #: cannot run (#2814). Signed with the rule.
    gate: Literal["github", "verify"] = "github"

    @model_validator(mode="after")
    def signed_when_enabled(self) -> "DriveMergeConfig":
        self.signer = self.signer.strip()
        if any(
            not name.strip().casefold().removeprefix("epic:").strip()
            for name in self.exclude_epics
        ):
            raise ValueError("drive.merge.exclude_epics has an empty epic name")
        if self.enabled and not self.signer:
            raise ValueError(
                "drive.merge.enabled needs drive.merge.signer, the GitHub login "
                "that signs the standing merge rule"
            )
        return self


class MergeQueueConfig(BaseModel):
    """``[merge_queue]`` — the project's merge queue (``kew merge queue``, #2814).

    The queue verifies ``main`` plus up to ``train`` queued PRs, in queue order,
    with one ``kew verify``, and merges them all when it passes. A failing train
    is bisected to find the PR that breaks it; that PR is failed with the
    reason and the rest are verified again. ``train = 1`` is a plain
    one-at-a-time queue.
    """

    model_config = {"extra": "forbid"}

    train: int = 1

    @field_validator("train")
    @classmethod
    def _validate_train(cls, v: int) -> int:
        if v < 1:
            raise ValueError("merge_queue.train must be >= 1")
        return v


class DriveConfig(BaseModel):
    """``[drive]`` — the project drive (``kew drive``): admission (#2616) and
    the standing merge rule (#2618)."""

    model_config = {"extra": "forbid"}

    #: The most runs — dispatches, reviews and remediations alike, on every
    #: host — the drive's ticks admit up to.
    max_parallel: int = 3
    #: Hold admission while the one-minute load average per CPU is above this
    #: (#2813); None is off.
    max_load_per_cpu: float | None = None
    #: Hold admission while more than this percentage of swap is in use
    #: (#2813); None is off.
    max_swap_used_pct: float | None = None
    #: Nodes the drive launches new agent work for while a local merge verify
    #: holds its lease, per verify (#3190); 0 launches nothing until it ends.
    launches_during_verify: int = 0
    load: DriveLoadConfig = DriveLoadConfig()
    merge: DriveMergeConfig = Field(default_factory=DriveMergeConfig)

    @field_validator("max_parallel")
    @classmethod
    def _validate_max_parallel(cls, v: int) -> int:
        if v < 1:
            raise ValueError("drive.max_parallel must be >= 1")
        return v

    @field_validator("launches_during_verify")
    @classmethod
    def _validate_launches_during_verify(cls, v: int) -> int:
        if v < 0:
            raise ValueError("drive.launches_during_verify must be >= 0")
        return v

    @field_validator("max_load_per_cpu")
    @classmethod
    def _validate_max_load_per_cpu(cls, v: float | None) -> float | None:
        if v is not None and not (math.isfinite(v) and v > 0):
            raise ValueError("drive.max_load_per_cpu must be a finite number > 0")
        return v

    @field_validator("max_swap_used_pct")
    @classmethod
    def _validate_max_swap_used_pct(cls, v: float | None) -> float | None:
        if v is not None and not (math.isfinite(v) and 0 < v <= 100):
            raise ValueError("drive.max_swap_used_pct must be > 0 and <= 100")
        return v

    @property
    def host_gated(self) -> bool:
        """Whether either host-load threshold is set (#2813)."""
        return self.max_load_per_cpu is not None or self.max_swap_used_pct is not None


class ServeConfig(BaseModel):
    """``[serve]`` — opt-in console features that spend (#2518).

    ``palette_ask`` turns on the palette's ask mode: ``POST /api/palette/ask``
    hands one query and the titles the palette already offers to a model, which
    may only rank those entries. Off by default, because every ask is a model
    call the operator pays for. ``palette_ask_model`` names the model; empty
    falls back to ``[agent] model``.
    """

    model_config = {"extra": "forbid"}

    palette_ask: bool = False
    palette_ask_model: str = ""


class PathsConfig(BaseModel):
    """Where kew keeps the trees it makes (:mod:`kew.work_dirs`).

    ``work_root`` holds every long-lived tree kew creates: local verify
    checkouts, sandbox clones, reviewer checkouts. Never the OS temp
    directory, which macOS cleans daily under a running suite.
    ``KEW_WORK_ROOT`` overrides it.
    """

    model_config = {"extra": "forbid"}

    work_root: str = work_dirs.DEFAULT_WORK_ROOT


class DispatchConfig(BaseModel):
    """Root configuration model for kew."""

    model_config = {"extra": "forbid"}

    project: ProjectConfig = ProjectConfig()
    tracker: TrackerConfig = TrackerConfig()
    agent: AgentConfig = AgentConfig()
    branches: BranchConfig = Field(default_factory=BranchConfig)
    labels: LabelConfig = LabelConfig()
    commands: CommandsConfig = CommandsConfig()
    ci: CiConfig = CiConfig()
    prompt: PromptConfig = PromptConfig()
    security: SecurityConfig = SecurityConfig()
    sandbox: SandboxConfig = SandboxConfig()
    hosts: HostsConfig = HostsConfig()
    verify: VerifyConfig = VerifyConfig()
    drive: DriveConfig = Field(default_factory=DriveConfig)
    merge_queue: MergeQueueConfig = Field(default_factory=MergeQueueConfig)
    retry: RetryConfig = RetryConfig()
    parallel: ParallelConfig = ParallelConfig()
    fleet: FleetConfig = FleetConfig()
    review: ReviewConfig = ReviewConfig()
    resolve: ResolveConfig = ResolveConfig()
    publication: PublicationConfig = PublicationConfig()
    triggers: TriggersConfig = TriggersConfig()
    budget: BudgetConfig = BudgetConfig()
    breaker: BreakerConfig = BreakerConfig()
    approval: ApprovalConfig = ApprovalConfig()
    epic: EpicConfig = EpicConfig()
    delegation: DelegationConfig = DelegationConfig()
    report: ReportConfig = ReportConfig()
    run_reports: RunReportsConfig = RunReportsConfig()
    pricing: PricingConfig = PricingConfig()
    model_eol: ModelEolConfig = ModelEolConfig()
    billing: BillingConfig = BillingConfig()
    # default_factory (not a shared instance) so each construction re-reads
    # KEW_LOG_DIR via LoggingConfig's own default_factory (SFD-134 / kew #784).
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ingest: IngestConfig = IngestConfig()
    human_review: HumanReviewConfig = HumanReviewConfig()
    audit: AuditConfig = AuditConfig()
    alerting: AlertingConfig = AlertingConfig()
    routing: RoutingConfig | None = None
    overlap: OverlapConfig = OverlapConfig()
    triage: TriageConfig = TriageConfig()
    flight_recorder: FlightRecorderConfig = FlightRecorderConfig()
    serve: ServeConfig = ServeConfig()
    paths: PathsConfig = Field(default_factory=PathsConfig)
    inference_transport: InferenceTransportConfig = Field(
        default_factory=InferenceTransportConfig
    )

    def fleet_concurrency(self) -> int:
        """The fleet-wide in-flight cap every admission check reads (#2398).

        ``[fleet] max_concurrent`` when the operator set it, else ``[parallel]
        max_workers`` — so an operator who never opens ``[fleet]`` keeps exactly
        today's cap, and one who does gets a number that means "agents anywhere"
        rather than "processes on this machine". Both keys have a floor of 1, so
        this never returns a number a process pool or a dispatch budget cannot
        be sized from.

        A method, not a resolved field: resolving into ``self.fleet`` at load
        would make the stored value depend on load order, and both keys are
        still edited at runtime by ``--workers`` (see ``kew/cli``, which sets
        BOTH so the flag cannot be shadowed by a configured fleet cap). Reading
        it here means a late edit is honoured.

        Because ``[fleet] max_concurrent`` wins here, every layer ABOVE the file
        has to write both keys or it would be shadowed by a file value it
        outranks — which is what ``--workers`` and the ``KEW_MAX_WORKERS`` env
        layer (:func:`_carry_env_workers_to_the_fleet_cap`) do.
        """
        cap = self.fleet.max_concurrent
        return self.parallel.max_workers if cap is None else cap

    @model_validator(mode="after")
    def _merge_queue_needs_a_remediation_path(self) -> "DispatchConfig":
        """``[epic] merge_queue`` requires the governed review stage (#2148).

        A red combined tree is recorded as a ``NEEDS_CHANGES`` review round, and
        the review stage is the ONLY thing that remediates one. With
        ``[review] adversarial = false`` that stage never runs, so the round
        stands against the head forever: the queue repeats the same refusal every
        tick, nothing fixes the tree, and the node reads as awaiting a merge that
        can never happen. Refuse the combination at load rather than wedge a node
        silently at merge time.
        """
        if self.epic.merge_queue and not self.review.adversarial:
            raise ValueError(
                "[epic] merge_queue = true requires [review] adversarial = true: "
                "a red combined tree is recorded as a NEEDS_CHANGES review round "
                "and only the governed review stage remediates one"
            )
        return self

    @model_validator(mode="after")
    def _excluded_epics_need_a_resolve_path(self) -> "DispatchConfig":
        """``[drive.merge] exclude_epics`` requires the governed review stage (#2869).

        An excluded epic's READY PR that stops merging into the base is held
        ``ci_pending:dirty``, and the review stage's merge-main step is the ONLY
        thing that resolves it. With ``[review] adversarial = false`` that stage
        never runs, so the hold stands until a person fixes the PR by hand while
        the node reads as waiting on CI. Refuse the combination at load.
        """
        if self.drive.merge.exclude_epics and not self.review.adversarial:
            raise ValueError(
                "[drive.merge] exclude_epics requires [review] adversarial = true: "
                "an excluded epic's base conflict is resolved only by the "
                "governed review stage's merge-main step"
            )
        return self

    @model_validator(mode="after")
    def _merge_queue_needs_an_explicit_verify_command(self) -> "DispatchConfig":
        """``[epic] merge_queue`` requires ``[commands] verify_loop`` (#2184).

        The queue used to fall back to ``commands.ci``, which is a different
        promise: on this repo ``ci`` is pytest alone, so the fallback silently
        merged combined trees nothing had linted, typed, or scanned while the
        node's evidence said the queue had verified it. A gate whose coverage
        depends on which key happens to be set is the silent-clean-default
        archetype; the command the queue runs must be chosen for the queue.
        """
        if self.epic.merge_queue and not self.commands.verify_loop.strip():
            raise ValueError(
                "[epic] merge_queue = true requires [commands] verify_loop: the "
                "queue will not fall back to commands.ci, which is usually a "
                "narrower gate than the one a combined tree has to pass"
            )
        return self


# Maps KEW_* env var name -> (config section, field name, type converter)
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "KEW_RUNNER": ("agent", "runner", str),
    "KEW_TIMEOUT": ("agent", "timeout_minutes", int),
    "KEW_REPO": ("project", "repo", str),
    "KEW_LOG_DIR": ("logging", "dir", str),
    "KEW_MAX_BUDGET": ("agent", "max_budget_usd", float),
    "KEW_MAX_WORKERS": ("parallel", "max_workers", int),
    "KEW_FLEET_MAX_CONCURRENT": ("fleet", "max_concurrent", int),
    "KEW_AUDIT_STALENESS_WINDOW": ("audit", "staleness_window", int),
    "KEW_AUDIT_ANCHOR_MAX_AGE_DAYS": ("audit", "anchor_max_age_days", int),
    "KEW_WORKTREE_BASE": ("branches", "worktree_base", str),
}


def config_variables() -> tuple[str, ...]:
    """Every environment name that steers which project, ledger and caps a kew
    process resolves: ``KEW_PROJECT`` and the ``KEW_*`` overrides."""
    return ("KEW_PROJECT", *_ENV_OVERRIDES)


def config_environment(environ: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """The ``KEW_*`` config layer in ``environ``, to carry into another process.

    A process started elsewhere (a launchd agent, #2810) must resolve the same
    config: same ledger, same caps. A relative ``KEW_LOG_DIR`` is read against
    the cwd (the env layer overrides the project anchoring), so it is carried
    resolved against ``cwd``. ``KEW_PROJECT`` is not carried here: the caller
    pins the project it resolved.
    """
    carried = {var: environ[var] for var in _ENV_OVERRIDES if var in environ}
    log_dir = carried.get("KEW_LOG_DIR")
    if log_dir:
        carried["KEW_LOG_DIR"] = str((cwd / log_dir).resolve())
    return carried


def _env_workers_carry_the_fleet_cap(environ: Mapping[str, str]) -> bool:
    """Whether the env layer derives ``[fleet] max_concurrent`` from workers.

    The single condition, shared by the writer
    (:func:`_carry_env_workers_to_the_fleet_cap`) and by the provenance reader
    (:func:`env_source`) so the two cannot drift: ``KEW_MAX_WORKERS`` is set and
    no explicit ``KEW_FLEET_MAX_CONCURRENT`` outranks it.
    """
    return "KEW_MAX_WORKERS" in environ and "KEW_FLEET_MAX_CONCURRENT" not in environ


# Fields the env layer writes from a variable that does NOT name them: dotted
# field -> (the variable that writes it, the predicate deciding whether it did).
_ENV_CARRIED: dict[str, tuple[str, Callable[[Mapping[str, str]], bool]]] = {
    "fleet.max_concurrent": ("KEW_MAX_WORKERS", _env_workers_carry_the_fleet_cap),
}


def env_source(field: str, env: Mapping[str, str] | None = None) -> str | None:
    """The environment variable in force for a dotted field, or ``None``.

    :func:`load_config` applies the env layer LAST — after the file and after
    the DB overrides — so a name returned here is the layer that decided the
    value, whatever the other two said. Every surface that reports provenance
    has to apply that rule ("an environment variable can never be reported as
    something the file said", :func:`kew.service.config.config_snapshot`), and
    they do not all sit above ``kew.service``: the runner registry's
    :func:`~kew.runners.registry.explain_runner_route` is below it and reported
    ``KEW_RUNNER`` as a ``kew.toml [agent].runner`` line, sending an operator to
    a file line that does not exist. So the rule lives here, beside the table it
    reads.

    :data:`_ENV_CARRIED` is read for the same reason one layer down: the env
    layer also writes fields no variable NAMES, and a field it carried but no
    reader knows about is reported as ``kew.toml`` — the file credited with a
    number it never stated (#2398). A carried field is only reported when its
    own variable is unset, because the loop below answers that case first.
    """
    environ = os.environ if env is None else env
    for env_var, (section, attr, _converter) in _ENV_OVERRIDES.items():
        if field == f"{section}.{attr}" and env_var in environ:
            return env_var
    carried = _ENV_CARRIED.get(field)
    if carried is not None and carried[1](environ):
        return carried[0]
    return None


def _project_root() -> Path | None:
    """The explicitly-selected project directory, or None when unset.

    Resolves ``KEW_PROJECT`` (set by the global ``--project`` CLI option). When
    present, config + DB are resolved from this directory regardless of cwd,
    short-circuiting the cwd-upward walk — the fix for the wrong-cwd footgun
    (empty-graph tick / wrong tracker selection). Read directly from the
    environment, like ``_default_log_dir``: a path-selecting env var can't be a
    section override in ``_ENV_OVERRIDES`` (it steers discovery, not a field).
    """
    value = os.environ.get("KEW_PROJECT")
    if not value:
        return None
    return Path(value).expanduser()


def _discovery_dirs() -> Iterator[Path]:
    """Directories config discovery searches, nearest first.

    When ``KEW_PROJECT`` / ``--project`` selects a project directory, only that
    directory; otherwise cwd and its parents, stopping at the git root.
    """
    root = _project_root()
    if root is not None:
        yield root
        return
    current = Path.cwd().resolve()
    while True:
        yield current
        # Stop at git root — don't escape the repository
        if (current / ".git").exists():
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _discover_config_path() -> Path | None:
    """Find kew.toml, stopping at git root.

    Side-effect free — no warnings — so it is safe for read-only callers like
    ``resolved_config_path``.
    """
    for directory in _discovery_dirs():
        candidate = directory / "kew.toml"
        if candidate.exists():
            return candidate
    return None


def _refuse_unread_dispatch_toml() -> None:
    """Refuse to run where discovery meets a dispatch.toml before any kew.toml.

    kew does not read ``dispatch.toml``. Loading defaults in its place would
    silently drop the operator's budget caps and move the ledger, so the first
    directory holding either file decides: a kew.toml is authoritative (a
    dispatch.toml beside it is ignored); a lone dispatch.toml is refused
    without being parsed.
    """
    for directory in _discovery_dirs():
        if (directory / "kew.toml").exists():
            return
        unread = directory / "dispatch.toml"
        if unread.exists():
            raise ConfigError(
                path=unread,
                key="dispatch.toml",
                expectation="a kew.toml; kew does not read dispatch.toml",
                fix="put this project's configuration in kew.toml",
                kind="unread_config_file",
            )


def resolved_config_path(config_path: Path | None = None) -> Path | None:
    """The config file actually in force, or None when none exists.

    Mirrors ``load_config``'s discovery: an explicit *config_path* wins (when it
    exists); otherwise walk up from cwd to the git root. Unlike ``load_config``
    it does NOT fall back to a non-existent ``cwd/kew.toml`` — callers that need
    the real file on disk (e.g. ``kew backup``) get None when there is none.
    """
    if config_path is not None:
        return config_path if config_path.exists() else None
    return _discover_config_path()


def project_root(config_path: Path | None = None) -> Path:
    """The directory every *relative* kew path resolves against.

    Precedence: ``KEW_PROJECT`` / ``--project`` > the directory holding the
    config file in force > cwd. This is the same root the ``project=`` banner
    reports, so ``db=`` can no longer disagree with it (#1156): a relative
    ``logging.dir`` used to resolve against cwd, so every subdirectory kew was
    invoked from silently minted its own ledger — two live chains, a false
    ``COUNT-SHRINK`` from the wrong one, and a daily budget cap you could reset
    by changing directory.
    """
    selected = _project_root()
    if selected is not None:
        return selected.resolve()
    if config_path is not None:
        return config_path.parent.resolve()
    found = _discover_config_path()
    if found is not None:
        return found.parent.resolve()
    return Path.cwd().resolve()


_log = logging.getLogger(__name__)

OVERRIDABLE_FIELDS: frozenset[str] = frozenset(
    {
        "budget.per_issue_usd",
        "budget.daily_usd",
        "budget.weekly_usd",
        "agent.max_budget_usd",
        # Routing/model runtime overrides (#659) — steer the effective route
        # from the DB so a committed kew.toml can't win a race mid-epic.
        "agent.model",
        # The reviewer's model (#2741). Which model reviews agent PRs is a
        # trust boundary, so the write path (``kew.service.config``) accepts
        # only a routable, unretired catalog id on the reviewer's runner.
        "review.model",
        "routing.default",
        "routing.simple",
        "routing.medium",
        "routing.complex",
        # Circuit-breaker sensitivity (#245) — spend governance, hand-committed
        # repeatedly; override it from the DB like the budget knobs (#659 config page).
        "breaker.cost_anomaly_factor",
    }
)


def validate_override(field: str, value: Any) -> None:
    """Raise ``ValueError`` if *field* is off-allowlist or *value* is invalid.

    Validates by round-tripping the value through the owning Pydantic model.
    """
    if field not in OVERRIDABLE_FIELDS:
        raise ValueError(f"field {field!r} is not overridable")
    section, attr = field.split(".", 1)

    # routing.* entries round-trip through RouteEntry. A bare string is the
    # runner-name shorthand in a TOML file, but as an override it's the footgun
    # found 2026-07-15 (silently means "runner", drops the model) — reject it.
    if section == "routing":
        if isinstance(value, str):
            raise ValueError(
                f"routing.{attr}: a bare string is the runner-name shorthand, "
                "which is rejected for overrides — pass a RouteEntry dict like "
                '{"runner": "claude-code", "model": "..."} instead'
            )
        try:
            RouteEntry.model_validate(value)
        except ValidationError as exc:
            raise ValueError(str(exc.errors()[0]["msg"])) from None
        return

    # A model override must be a non-empty string (an empty override is a
    # no-op that would silently fall through to the file value/default).
    if field in ("agent.model", "review.model"):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return

    _SECTION_MODELS: dict[str, type[BaseModel]] = {
        "budget": BudgetConfig,
        "agent": AgentConfig,
        "breaker": BreakerConfig,
        "report": ReportConfig,
    }
    model_cls = _SECTION_MODELS[section]
    try:
        model_cls.model_validate({attr: value})
    except ValidationError as exc:
        raise ValueError(str(exc.errors()[0]["msg"])) from None


def _convert_env_value(env_var: str, value: str, converter: type) -> Any:
    """Convert an env var string value, raising a clear ValueError on failure."""
    try:
        return converter(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"env var {env_var!r} = {value!r}: "
            f"cannot convert to {converter.__name__}: {exc}"
        ) from exc


def _find_env_override(
    env_var: str, environ: Mapping[str, str]
) -> tuple[str, str] | None:
    """Return the configured source name and raw environment value."""
    value = environ.get(env_var)
    if value is None:
        return None
    return env_var, value


def _coerce_env_override(*, source_var: str, value: str, converter: type) -> Any:
    """Convert one environment override into a structured failure."""
    try:
        return _convert_env_value(source_var, value, converter)
    except ValueError:
        expectation = "an integer" if converter is int else f"a {converter.__name__}"
        raise ConfigError(
            path=source_var,
            key=source_var,
            expectation=expectation,
            fix=f"set {source_var} to {expectation}; got {value!r}",
            kind="env_override",
        ) from None


# Environment names kew read before W4.3 (#2000). They are generic enough to
# belong to other tools, so kew does not refuse on them; it says they are
# ignored, because a silent drop would lose a cap such as DISPATCH_MAX_BUDGET.
_REMOVED_ENV: dict[str, str] = {
    "DISPATCH_TIMEOUT": "KEW_TIMEOUT",
    "DISPATCH_REPO": "KEW_REPO",
    "DISPATCH_LOG_DIR": "KEW_LOG_DIR",
    "DISPATCH_MAX_BUDGET": "KEW_MAX_BUDGET",
    "DISPATCH_MAX_WORKERS": "KEW_MAX_WORKERS",
    "WORKTREE_BASE": "KEW_WORKTREE_BASE",
}


def _warn_removed_env(environ: Mapping[str, str]) -> None:
    """Name every removed environment variable that is still set."""
    from kew.progress import _warn

    for old, new in _REMOVED_ENV.items():
        if old in environ:
            _warn(f"{old} is no longer read and is ignored; set {new} instead.")


def _carry_env_workers_to_the_fleet_cap(
    raw: dict[str, Any], environ: Mapping[str, str]
) -> None:
    """``KEW_MAX_WORKERS`` must move the cap that is actually enforced (#2398).

    :meth:`DispatchConfig.fleet_concurrency` reads ``[fleet] max_concurrent``
    ahead of ``[parallel] max_workers``, so an operator throttling the fleet by
    exporting ``KEW_MAX_WORKERS`` over a kew.toml that happens to set ``[fleet]
    max_concurrent`` was silently ignored for the ONE number every admission
    check, the drain's capacity gate and the epic driver's dispatch budget read
    — :func:`load_config`'s documented **env > DB > toml** precedence, inverted
    for exactly the operator who configured a fleet cap.

    So the env layer writes BOTH keys, for the same reason ``--workers`` does
    (:func:`kew.cli._apply_workers_override`): nothing reading ``max_workers``
    directly may report a number the environment overrode, and nothing reading
    the fleet cap may keep a file value the environment overrode. An explicit
    ``KEW_FLEET_MAX_CONCURRENT`` still wins — it names the fleet cap and sits in
    the same layer — and a ``[fleet] max_concurrent`` with no ``KEW_MAX_WORKERS``
    set is untouched, so a file-only fleet cap still overrides the file's own
    worker count.

    The write is also a provenance fact, so the condition lives in
    :func:`_env_workers_carry_the_fleet_cap` and :func:`env_source` reads it
    through :data:`_ENV_CARRIED`: a key written here and reported as the file's
    is the #2298 rule inverted for the operator who set a fleet cap.
    """
    if not _env_workers_carry_the_fleet_cap(environ):
        return
    fleet_raw = raw.get("fleet")
    if fleet_raw is not None and not isinstance(fleet_raw, dict):
        return  # malformed [fleet]; leave the refusal to validation
    workers = raw.get("parallel", {}).get("max_workers")
    if workers is None:
        return
    raw.setdefault("fleet", {})["max_concurrent"] = workers


def _apply_env_overrides(
    raw: dict[str, Any], environ: Mapping[str, str]
) -> dict[str, Any]:
    """Apply the KEW_* environment overrides to the raw mapping."""
    _warn_removed_env(environ)
    for env_var, (section, field, converter) in _ENV_OVERRIDES.items():
        override = _find_env_override(env_var, environ)
        if override is None:
            continue
        source_var, value = override
        raw.setdefault(section, {})[field] = _coerce_env_override(
            source_var=source_var,
            value=value,
            converter=converter,
        )
    _carry_env_workers_to_the_fleet_cap(raw, environ)
    return raw


def _require_routed_lifecycle_rows(config: "DispatchConfig", config_path: Path) -> None:
    """A routed model id needs a lifecycle row (#1490, #2709).

    The rows come from the model catalog, or the explicit
    ``[model_eol].snapshot_path`` override. Free local models (``ollama/*``)
    are exempt here as they are from pricing. A row's *status* stays advisory;
    only a missing row refuses.
    """
    if config.routing is None:
        return
    rows = config.model_eol.snapshot_models
    for lane in ("default", "simple", "medium", "complex"):
        entry = getattr(config.routing, lane)
        if not isinstance(entry, RouteEntry) or not entry.model:
            continue
        if entry.model in rows or is_free_model(entry.model):
            continue
        raise ConfigError(
            path=config_path.resolve(),
            key=f"routing.{lane}.model",
            expectation="a model id with a lifecycle row in the model catalog",
            fix=f"{entry.model}: registry gap (no curated lifecycle row); add "
            "its row to kew/src/kew/models.json or route to a catalog model",
            kind="invalid_value",
        )


def _merge_model_eol_snapshot(raw: dict[str, Any], config_path: Path) -> None:
    """Translate an unusable lifecycle registry into the config envelope."""
    try:
        merge_model_eol_snapshot_into_raw(raw, config_path.parent)
    except RegistryError as exc:
        raise ConfigError(
            path=config_path.resolve(),
            key="model_eol.snapshot_path",
            expectation="a JSON snapshot written by kew model-eol",
            # The registry's own reason (unreadable, unsorted ids, bad
            # reviewedAt, ...) is the actionable part; keep it on the line the
            # operator reads, not only on __cause__.
            fix=f"{exc}; replace the configured file with a valid model-eol "
            "JSON snapshot",
            kind="snapshot",
        ) from exc


def _apply_db_overrides(raw: dict[str, Any], store: "RunStore") -> dict[str, Any]:
    """Merge allowlisted config_overrides rows into *raw* (mutates in-place)."""
    for row in store.list_config_overrides():
        field: str = row["field"]
        if field not in OVERRIDABLE_FIELDS:
            continue
        section, attr = field.split(".", 1)
        value = json.loads(row["value_json"])
        try:
            validate_override(field, value)
        except ValueError:
            _log.warning("skipping invalid config override %s=%r", field, value)
            continue
        if section not in raw:
            raw[section] = {}
        raw[section][attr] = value
    return raw


def _resolve_config_path(root: Path | None) -> Path:
    """Config file to load when the caller passed none.

    Discovery wins; otherwise a selected project resolves from its own dir (never
    the cwd — the wrong-cwd footgun), and only a bare invocation falls back to
    ``cwd/kew.toml``.
    """
    _refuse_unread_dispatch_toml()
    found = _discover_config_path()
    if found is not None:
        return found
    base = root if root is not None else Path.cwd()
    return base / "kew.toml"


def _anchor_log_dir(raw: dict[str, Any], root: Path) -> None:
    """Anchor a relative ``logging.dir`` (and thus the DB) to the project root.

    So the DB is resolved from the project dir regardless of cwd — whether that
    dir came from ``--project`` / ``KEW_PROJECT`` or from the discovered
    kew.toml (#1156). An explicit KEW_LOG_DIR still wins (the env loop overrides
    this); absolute log dirs are left untouched.
    """
    logging_raw = raw.get("logging")
    toml_dir = logging_raw.get("dir") if isinstance(logging_raw, dict) else None
    base_dir = toml_dir if toml_dir is not None else _default_log_dir()
    if not Path(base_dir).is_absolute():
        raw.setdefault("logging", {})["dir"] = str((root / base_dir).resolve())


def _anchor_worktree_base(raw: dict[str, Any], root: Path) -> None:
    """Anchor the effective relative checkout root to the selected project."""
    branches_raw = raw.get("branches")
    if branches_raw is not None and not isinstance(branches_raw, dict):
        return
    configured = (
        branches_raw.get("worktree_base") if isinstance(branches_raw, dict) else None
    )
    base = configured if configured is not None else BranchConfig().worktree_base
    if not isinstance(base, str):
        return
    expanded = Path(base).expanduser()
    if not expanded.is_absolute():
        expanded = (root / expanded).resolve()
    if not Path(base).is_absolute():
        raw.setdefault("branches", {})["worktree_base"] = str(expanded)


# Snapshot provenance the loader injects into the raw mapping after reading
# kew.toml (#1679). These are not operator keys: the values come from the
# curated lifecycle registry, and they are carried on the config object as
# private attributes rather than fields.
_SNAPSHOT_PROVENANCE: dict[str, tuple[str, ...]] = {
    "model_eol": ("snapshot_models",),
}


def _reject_operator_snapshot_provenance(raw: dict[str, Any], path: Path) -> None:
    """Raise if the operator's TOML writes a loader-set provenance key.

    Called on the TOML source *before* the snapshot merges overwrite it, so the
    hand-written key is named in the error instead of being silently replaced.
    """
    for section, keys in _SNAPSHOT_PROVENANCE.items():
        table = raw.get(section)
        if not isinstance(table, dict):
            continue
        for key in keys:
            if key in table:
                dotted_key = f"[{section}].{key}"
                raise ConfigError(
                    path=path.resolve(),
                    key=dotted_key,
                    expectation="loader-managed snapshot provenance",
                    fix=f"remove {dotted_key}; the loader supplies it",
                    kind="provenance",
                )


def _take_snapshot_provenance(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pop the loader-injected provenance out of *raw* and return it by section.

    What remains in *raw* is the operator-writable schema, which the models
    validate with ``extra="forbid"``.
    """
    taken: dict[str, dict[str, Any]] = {}
    for section, keys in _SNAPSHOT_PROVENANCE.items():
        table = raw.get(section)
        if not isinstance(table, dict):
            continue
        for key in keys:
            if key in table:
                taken.setdefault(section, {})[key] = table.pop(key)
    return taken


def _apply_snapshot_provenance(
    config: "DispatchConfig", taken: dict[str, dict[str, Any]]
) -> None:
    """Carry the popped provenance onto the validated config object."""
    for section, values in taken.items():
        target = getattr(config, section)
        for key, value in values.items():
            setattr(target, key, value)


def load_config(
    config_path: Path | None = None,
    *,
    store: "RunStore | None" = None,
    env: Mapping[str, str] | None = None,
) -> DispatchConfig:
    """Load config from kew.toml, optional DB overrides, and env var overrides.

    Precedence (highest wins): **env > DB override > kew.toml > defaults**.
    When *store* is None the DB layer is skipped — file-only behaviour is
    unchanged for read-only / test callers.

    Args:
        config_path: Explicit path to a TOML config file. If None, walks up
            from cwd to find kew.toml, stopping at the git root.
        store: Optional run store. When provided, allowlisted rows from the
            ``config_overrides`` table are merged after TOML and before env
            vars, giving precedence **env > DB > toml > defaults**.
        env: The environment the ``KEW_*`` layer is read from; ``os.environ``
            when None. Pass ``{}`` to compose the layers BELOW the environment —
            which is the only honest way to answer "what does the file say?".
            ``/api/config/budget`` reported an env-sourced number as the config
            file's own value, so an operator who set ``KEW_MAX_BUDGET`` saw the
            web override they had just made described as having no effect on a
            value it really did not control (#2298). Discovery is deliberately
            NOT affected: ``KEW_PROJECT`` and the log-dir anchoring still read
            the real environment, so the file in force is the same file either
            way.

    Returns:
        Validated DispatchConfig.
    """
    root = _project_root()
    if config_path is None:
        config_path = _resolve_config_path(root)

    # Load TOML if it exists
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = _read_toml_cached(config_path)
    _reject_operator_snapshot_provenance(raw, config_path)
    selected_root = project_root(config_path)
    _anchor_log_dir(raw, selected_root)

    # Curated model lifecycle registry (#1490): the wheel-packaged registry is
    # the source unless [model_eol].snapshot_path names an explicit override.
    # Lifecycle status stays advisory; a routed model with no row at all, or a
    # malformed registry, raises rather than hydrating an empty view.
    _merge_model_eol_snapshot(raw, config_path)

    # DB overrides layer (after toml, before env)
    if store is not None:
        _apply_db_overrides(raw, store)

    raw = _apply_env_overrides(raw, os.environ if env is None else env)
    _anchor_worktree_base(raw, selected_root)

    provenance = _take_snapshot_provenance(raw)
    config = _validate_dispatch_config(raw, config_path)
    _apply_snapshot_provenance(config, provenance)
    _require_routed_lifecycle_rows(config, config_path)
    work_dirs.configure(
        config.paths.work_root,
        ledger=(selected_root, Path(config.logging.dir).resolve() / "dispatch.db"),
    )
    return config
