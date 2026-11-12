"""Runner registry and model-to-runner resolution."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kew.config import DispatchConfig, RouteEntry, RoutingConfig, env_source
from kew.inference_transport import RunnerTransportCapability
from kew.runner_quota import (
    QuotaWarning,
    RunCredential,
    RunnerQuotaWarning,
    active_runner_quota_warnings,
    credential_fingerprint,
    record_runner_launch_credential,
    record_runner_quota_warning,
)
from kew.runners.capabilities import RunnerCapabilities, capability_manifest
from kew.runners.protocol import AgentRunner, ContainmentPosture

if TYPE_CHECKING:
    from datetime import datetime

    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

# Model prefix -> runner name mapping. Order matters: earlier patterns win.
_MODEL_PATTERNS: list[tuple[str, str]] = [
    ("claude-", "claude-code"),
    ("gemini-", "opencode"),
    ("gpt-", "opencode"),
    ("o1-", "opencode"),
    ("o3-", "opencode"),
    ("o4-", "opencode"),
    ("ollama/", "opencode"),
    ("openrouter/", "opencode"),
    ("lemonade/", "lemonade"),
]

# Valid complexity levels for routing lookup
_VALID_COMPLEXITY = {"simple", "medium", "complex"}

# Every runner name the registry can resolve. Declared as a plain frozenset so
# config validation can check a runner-keyed key (``[sandbox] images.<runner>``)
# without importing the runner CLASSES — kew.config cannot import this module at
# module scope (registry imports kew.config), and the runner modules are the
# heavy half of that cycle. ``test_known_runner_names_match_the_factories``
# pins it to ``_get_runner_factories()`` so the two cannot drift.
KNOWN_RUNNER_NAMES = frozenset(
    {"claude-code", "codex", "opencode", "pi", "hermes", "lemonade"}
)

_TRANSPORT_CAPABILITIES = {
    name: RunnerTransportCapability() for name in KNOWN_RUNNER_NAMES
}


@dataclass
class ResolvedRoute:
    """Result of runner resolution — runner instance + effective model."""

    runner: AgentRunner
    model: str


def _get_runner_factories() -> dict[str, Callable[[], AgentRunner]]:
    """Lazy import to avoid circular dependencies."""
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.codex import CodexRunner
    from kew.runners.hermes import HermesRunner
    from kew.runners.lemonade import LemonadeRunner
    from kew.runners.opencode import OpenCodeRunner
    from kew.runners.pi import PiRunner

    classes = {
        "claude-code": ClaudeCodeRunner,
        "codex": CodexRunner,
        "opencode": OpenCodeRunner,
        "pi": PiRunner,
        "hermes": HermesRunner,
        "lemonade": LemonadeRunner,
    }
    return {
        name: _factory_with_capability(name, factory)
        for name, factory in classes.items()
    }


def _factory_with_capability(
    name: str, factory: Callable[[], object]
) -> Callable[[], AgentRunner]:
    def create() -> AgentRunner:
        return _runner_with_capability(name, factory())

    return create


def _runner_with_capability(name: str, runner: object) -> AgentRunner:
    """Attach the registry's immutable capability declaration to an instance."""
    mutable_runner = cast(Any, runner)
    if not hasattr(mutable_runner, "transport_capability"):
        mutable_runner.transport_capability = _TRANSPORT_CAPABILITIES[name]
    mutable_runner.capabilities = capability_manifest(name)
    return cast(AgentRunner, mutable_runner)


def runner_capabilities(runner_name: str) -> RunnerCapabilities:
    """Return the central immutable runner-native capability declaration."""
    return capability_manifest(runner_name)


def runner_required_credentials(runner_name: str) -> tuple[str, ...]:
    """The env var names ``runner_name`` authenticates with in-container (#1146).

    The single resolution site for the container env allowlist: ``kew.sandbox``
    asks THIS function rather than keeping its own runner→credential table, so
    a new runner's declaration reaches containment the moment the runner is
    registered, and no second copy can drift from the runners themselves.

    Returns ``()`` for an unknown runner as well as for a runner that declares
    no in-container credential — both are un-containable for the same reason
    (nothing kew can forward), and both fail closed at
    ``resolve_effective_sandbox``. Never guess another runner's credentials:
    handing one runner's key to another's container is the #1104 mismatch with
    an exfiltration surface attached.
    """
    factory = _get_runner_factories().get(runner_name)
    if factory is None:
        return ()
    return tuple(factory().required_credentials)


def runner_credential_fingerprint(
    runner_name: str, env: Mapping[str, str] | None = None
) -> str:
    """Fingerprint of *runner_name*'s credential in *env* (default: os.environ).

    A runner quota hold recorded under one fingerprint stops applying under
    another (#2643): a new token is a new account window. "" when the runner
    declares no credential or none is set. Never the value itself.
    """
    return credential_fingerprint(
        os.environ if env is None else env, runner_required_credentials(runner_name)
    )


def runner_credential_names(
    runner_name: str, env: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The credential env vars *runner_name* reads that are set in *env*.

    Every declared one when none is set. Names only, never values.
    """
    source = os.environ if env is None else env
    names = runner_required_credentials(runner_name)
    return tuple(n for n in names if source.get(n)) or names


def run_credential(
    runner_name: str, env: Mapping[str, str] | None = None
) -> RunCredential:
    """The credential a *runner_name* run launched from *env* (default:
    os.environ) runs under (#3330): names and fingerprint, never the value."""
    return RunCredential(
        runner_name,
        runner_credential_names(runner_name, env),
        runner_credential_fingerprint(runner_name, env),
    )


def record_runner_token_warning(
    store: RunStore,
    warning: QuotaWarning,
    *,
    run_id: str = "",
    env: Mapping[str, str] | None = None,
) -> RunnerQuotaWarning | None:
    """Record a usage warning against the runner's credential in *env* (#3329).

    The record names the credential env vars that are set (every declared one
    when none is) and the fingerprint of their values — never a value.
    """
    return record_runner_quota_warning(
        store,
        warning,
        run_id=run_id,
        credential_fingerprint=runner_credential_fingerprint(warning.runner, env),
        credential_names=runner_credential_names(warning.runner, env),
    )


def record_runner_launch(
    store: RunStore | None, runner_name: str, env: Mapping[str, str] | None = None
) -> None:
    """Record the credential fingerprint *runner_name* launches under (#3329).

    A usage warning seen under any other fingerprint is for a token since
    replaced. Advisory: a store that refuses the write is logged, and the
    launch goes on. No store (an unrecorded run), nothing recorded.
    """
    if store is None:
        return
    try:
        record_runner_launch_credential(
            store, runner_name, runner_credential_fingerprint(runner_name, env)
        )
    except sqlite3.Error as exc:
        _logger.warning(
            "runner %s: launch fingerprint not recorded (%s)",
            runner_name,
            type(exc).__name__,
        )


def active_runner_token_warnings(
    store: RunStore,
    runners: Iterable[str] = KNOWN_RUNNER_NAMES,
    *,
    now: datetime | None = None,
) -> tuple[RunnerQuotaWarning, ...]:
    """Each runner's usage warnings that still apply, per limit.

    Read off the store alone: a warning stops applying once the runner's
    latest run launched under another credential (:func:`record_runner_launch`),
    never because of the reader's own environment.
    """
    return active_runner_quota_warnings(store, runners, now=now)


def runner_containment_posture(
    runner_name: str, *, runtime: str | None = None
) -> ContainmentPosture | None:
    """Return the containment posture declared by ``runner_name``'s code.

    Unknown runners declare nothing and continue through the existing
    fail-closed image admission path. Keeping this lookup in the registry makes
    the capability runner-owned; there is deliberately no config fallback.

    ``runtime`` is forwarded verbatim to the runner, and the caller owns which
    question it is asking: the configured ``[sandbox] runtime`` when kew is
    deciding whether to contain this dispatch itself, and ``None`` (the
    default) when there is no kew boundary on offer at all. See
    :meth:`kew.runners.protocol.AgentRunner.containment_posture` — the answers
    legitimately differ, and passing a runtime kew is not actually launching
    into would pick the wrong one.
    """
    factory = _get_runner_factories().get(runner_name)
    if factory is None:
        return None
    return factory().containment_posture(runtime=runtime)


def preflight_runner_workspace(runner_name: str, checkout_dir: Path) -> None:
    """Admit the tree ``runner_name`` is about to be handed, or raise.

    SECURITY SURFACE. The repo-supplied-config admission check, asked by NAME
    so that every launch lane can ask the same question. It used to live only
    in :func:`kew.orchestrator._preflight_runner_workspace`, which is the HOST
    lane's single launch site; when codex stopped declaring ``"native"`` on
    ``openshell`` (node N10) its dispatches started resolving to the contained
    lane instead, and that lane uploaded and exec'd the very ``.codex/`` tree
    this check exists to refuse (PR #2461 review).

    An unknown runner has no tree rule, which is not a pass for its image: the
    name never reaches a launch without going through
    :func:`resolve_effective_sandbox`'s fail-closed image admission first.
    """
    if runner_name != "codex":
        return
    from kew.runners.codex import validate_codex_project_tree

    validate_codex_project_tree(checkout_dir)


def runner_uncontainable_reason(runner_name: str) -> str | None:
    """Return why ``runner_name`` can never be contained, or ``None``.

    Unknown runners declare nothing and continue through the fail-closed image
    admission path — an unrecognised name must not become an uncontained run.
    Runner-owned like the rest of the containment surface: there is deliberately
    no config fallback, because "this cannot be contained" is a claim about the
    runner's design and configuration must not be able to assert it.
    """
    factory = _get_runner_factories().get(runner_name)
    if factory is None:
        return None
    return factory().uncontainable_reason


def get_runner(name: str) -> AgentRunner:
    """Instantiate the runner called ``name``, or raise ``ValueError``.

    The name→instance lookup for call sites that carry a runner NAME rather than
    the resolved instance — the sandbox path, where the name rides from the
    dispatch decision down to the container argv build (#1145). Unknown names
    fail closed here rather than defaulting to claude: a wrong guess puts one
    runner's flags in another runner's image (the #1104 class).
    """
    runner_classes = _get_runner_factories()
    if name not in runner_classes:
        available = ", ".join(sorted(runner_classes))
        raise ValueError(f"Unknown runner {name!r}. Available: {available}")
    return runner_classes[name]()


def _infer_runner_name_from_model(
    model: str, runner_classes: dict[str, Callable[[], AgentRunner]]
) -> str:
    """Infer runner name from model. Raises ValueError if not mappable.

    Resolution order:
      1. Known prefix match (claude-, gemini-, gpt-, o1-, o3-, o4-, ollama/,
         openrouter/)
      2. Fallback: any model containing "/" (i.e. provider/model form) -> opencode
      3. Otherwise raise.
    """
    for prefix, mapped_runner in _MODEL_PATTERNS:
        if model.startswith(prefix):
            if mapped_runner not in runner_classes:
                raise ValueError(
                    f"Runner {mapped_runner!r} matched for model "
                    f"{model!r} but is not yet available. "
                    f"Install it or set runner explicitly."
                )
            return mapped_runner
    if "/" in model and "opencode" in runner_classes:
        return "opencode"
    raise ValueError(
        f"Can't infer runner for model {model!r}. Set runner explicitly in [agent]."
    )


# Namespaces a runner can drive that `_MODEL_PATTERNS` attributes elsewhere.
# Inference must pick exactly ONE runner per model; compatibility is the wider
# question ("can X run M?"), and the OpenRouter namespace is shared by three
# harnesses (opencode/pi/hermes) even though inference names only opencode.
_RUNNER_EXTRA_PREFIXES: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-", "o1-", "o3-", "o4-"),
    "pi": ("openrouter/",),
    "hermes": ("openrouter/",),
}

_RUNNER_EXTRA_MODELS: dict[str, frozenset[str]] = {
    "codex": frozenset({"o1", "o3", "o4"}),
}


def runner_accepts_model(runner_name: str, model: str) -> bool:
    """Can ``runner_name`` run ``model``? The registry's compatibility answer.

    Derived from ``_MODEL_PATTERNS`` (so a new prefix cannot be added to routing
    inference and silently miss this check) plus ``_RUNNER_EXTRA_PREFIXES`` for
    namespaces more than one runner drives. Used to fail closed on a route no
    operator chose — e.g. ``loop resume --runner claude-code`` inheriting a
    recorded ``openrouter/...`` model (#1111).
    """
    if not model:
        return False
    if model in _RUNNER_EXTRA_MODELS.get(runner_name, ()):
        return True
    prefixes = tuple(
        prefix for prefix, mapped in _MODEL_PATTERNS if mapped == runner_name
    ) + _RUNNER_EXTRA_PREFIXES.get(runner_name, ())
    if any(model.startswith(prefix) for prefix in prefixes):
        return True
    # Mirror the inference fallback: a bare provider/model form is opencode's.
    return "/" in model and runner_name == "opencode"


def _resolve_route_entry(
    entry: RouteEntry | str,
    runner_classes: dict[str, Callable[[], AgentRunner]],
    fallback_model: str,
) -> ResolvedRoute:
    """Resolve a RouteEntry or string shorthand to a ResolvedRoute."""
    if isinstance(entry, str):
        # String shorthand: entry is the runner name; model inherited from agent config
        runner_name = entry
        model = fallback_model
    else:
        # RouteEntry
        runner_name = entry.runner
        model = entry.model or fallback_model

        # If runner_name is not set, infer from model
        if not runner_name:
            if model:
                runner_name = _infer_runner_name_from_model(model, runner_classes)
            else:
                runner_name = "claude-code"

    if runner_name not in runner_classes:
        available = ", ".join(sorted(runner_classes))
        raise ValueError(f"Unknown runner {runner_name!r}. Available: {available}")

    return ResolvedRoute(runner=runner_classes[runner_name](), model=model)


def resolve_runner(
    config: DispatchConfig, complexity: str | None = None
) -> ResolvedRoute:
    """Resolve the AgentRunner and effective model from config.

    Priority when routing is configured:
    1. Look up the entry for the given complexity level
    2. Fall back to config.routing.default if complexity level is absent or None

    Priority when no routing config:
    1. Explicit runner in config -> use it
    2. Model prefix match -> infer runner
    3. No model, no runner -> default to claude-code
    """
    runner_classes = _get_runner_factories()
    fallback_model = config.agent.model

    # --- Routing path ---
    if config.routing is not None:
        routing: RoutingConfig = config.routing

        # Look up entry for this complexity level (None complexity uses default)
        entry: RouteEntry | str | None = None
        if complexity is not None and complexity in _VALID_COMPLEXITY:
            entry = getattr(routing, complexity, None)

        # Fall back to default if no entry for this complexity
        if entry is None:
            entry = routing.default

        return _resolve_route_entry(entry, runner_classes, fallback_model)

    # --- No-routing path (existing behaviour, wrapped in ResolvedRoute) ---
    runner_name = config.agent.runner

    # 1. Explicit runner
    if runner_name:
        if runner_name not in runner_classes:
            available = ", ".join(sorted(runner_classes))
            raise ValueError(f"Unknown runner {runner_name!r}. Available: {available}")
        return ResolvedRoute(runner=runner_classes[runner_name](), model=fallback_model)

    # 2. Infer from model
    model = config.agent.model
    if model:
        inferred = _infer_runner_name_from_model(model, runner_classes)
        return ResolvedRoute(runner=runner_classes[inferred](), model=model)

    # 3. Default
    return ResolvedRoute(runner=runner_classes["claude-code"](), model=fallback_model)


@dataclass(frozen=True)
class RouteExplanation:
    """A resolved route plus WHICH rule decided each of its two fields (#2302).

    The sibling of :func:`resolve_runner` the dispatch launchpad reads: the
    route alone cannot tell an operator why ``codex`` is about to run, and a
    surface that guessed at the provenance would be a second resolver free to
    disagree with the first. So the same walk that picks the entry names it.

    ``field`` is the config field in the dotted form the DB override table is
    keyed by (``routing.simple``, ``agent.model``), so a caller with a store can
    ask whether an override — not the file — is what is steering the route.
    ``model_field`` is the same thing for the model half, which is NOT always
    the same field: a tier entry without a ``model`` leaves the runner steered
    by ``routing.<tier>`` and the model by ``agent.model``, and an override on
    one of those must not be reported against the other.
    ``runner_source`` / ``model_source`` are the operator-facing strings
    (``kew.toml [routing].simple``).
    """

    route: ResolvedRoute
    field: str
    runner_source: str
    model_source: str
    model_field: str = "agent.model"

    @property
    def runner_name(self) -> str:
        return getattr(self.route.runner, "name", "")

    @property
    def model(self) -> str:
        return self.route.model


def _agent_model_source(config: DispatchConfig) -> str:
    return "kew.toml [agent].model" if config.agent.model else "default"


def _routing_explanation(
    config: DispatchConfig, routing: RoutingConfig, complexity: str | None
) -> RouteExplanation:
    """Explain the routing path, walked exactly as ``resolve_runner`` walks it."""
    entry: RouteEntry | str | None = None
    if complexity is not None and complexity in _VALID_COMPLEXITY:
        entry = getattr(routing, complexity, None)
    tier = complexity if entry is not None else "default"
    if entry is None:
        entry = routing.default
    source = f"kew.toml [routing].{tier}"
    from_entry = isinstance(entry, RouteEntry) and bool(entry.model)
    return RouteExplanation(
        route=resolve_runner(config, complexity),
        field=f"routing.{tier}",
        runner_source=source,
        model_source=source if from_entry else _agent_model_source(config),
        model_field=f"routing.{tier}" if from_entry else "agent.model",
    )


def _agent_explanation(
    config: DispatchConfig, route: ResolvedRoute
) -> RouteExplanation:
    """Explain the no-routing path: the env, the pin, the inference, or the default.

    The environment is asked FIRST because ``load_config`` applies it last, so
    ``KEW_RUNNER`` is what decided ``config.agent.runner`` whenever it is set —
    and reporting that as ``kew.toml [agent].runner`` sent the operator to a
    file line that does not exist, which is the one thing a provenance string
    must never do (``kew.service.config.config_snapshot``'s rule, #2302 review).
    """
    env_var = env_source("agent.runner")
    if env_var is not None:
        runner_source, field = f"env ({env_var})", "agent.runner"
    elif config.agent.runner:
        runner_source, field = "kew.toml [agent].runner", "agent.runner"
    elif config.agent.model:
        runner_source, field = "inferred from [agent].model", "agent.model"
    else:
        runner_source, field = "default (claude-code)", "agent.runner"
    return RouteExplanation(
        route=route,
        field=field,
        runner_source=runner_source,
        model_source=_agent_model_source(config),
    )


def explain_runner_route(
    config: DispatchConfig, complexity: str | None = None
) -> RouteExplanation:
    """:func:`resolve_runner`'s answer, with the rule that produced it.

    Raises whatever ``resolve_runner`` raises — an unresolvable route has no
    provenance to report, and a reader that wants the refusal catches it there.
    """
    if config.routing is not None:
        return _routing_explanation(config, config.routing, complexity)
    return _agent_explanation(config, resolve_runner(config, complexity))
