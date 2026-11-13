"""The dispatch launchpad's reading, per source layer (#2302, AC1).

One test per layer the plan is assembled from, because "each field with a
``source``" is the whole feature and every layer gets its provenance from a
different place: the routing tier, the ``[agent]`` pin, the ``[review]`` pin, a
DB override, the containment resolution, the budget view, the governor, the
stored probe evidence and the admission gates.

Nothing here touches docker, the network or a tracker. The probe route is
resolved for real — it costs nothing by contract — against an uncontained
sandbox, which is the one posture a machine with no container runtime can
resolve; the contained case is covered through the refusal it produces, which
is itself a reading this surface has to carry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kew import sandbox
from kew.audit import RUNTIME_EGRESS_CERTIFICATION_EVENT
from kew.config import (
    AgentConfig,
    BudgetConfig,
    DispatchConfig,
    InferenceTransportConfig,
    ProjectConfig,
    ReviewConfig,
    RouteEntry,
    RoutingConfig,
    SandboxConfig,
)
from kew.models import GhLabel
from kew.runners.registry import runner_required_credentials
from kew.sandbox import (
    REFUSAL_OPENSHELL_UNCERTIFIED,
    REFUSAL_RUNTIME_UNAVAILABLE,
    SandboxRuntimeUnavailable,
    credential_missing_message,
)
from kew.selector import get_complexity
from kew.service import dashboard
from kew.service.dispatch_plan import (
    REFUSAL_MAX_CHARS,
    RouteResolver,
    resolve_dispatch_plan,
)
from kew.service.health import HealthProbes, ProbeCache
from kew.service.models import ReadyIssueDTO


class FakeStore:
    """Every ledger read a plan makes, and nothing else."""

    def __init__(
        self,
        *,
        overrides: dict[str, dict[str, Any]] | None = None,
        fleet_stopped: dict[str, str] | None = None,
        holds: tuple[int, ...] = (),
        issue_spend: float = 0.0,
        daily_spend: float = 0.0,
        certifications: int = 0,
    ) -> None:
        self._overrides = overrides or {}
        self._fleet_stopped = fleet_stopped
        self._holds = holds
        self._issue_spend = issue_spend
        self._daily_spend = daily_spend
        #: How many egress certifications this ledger holds. `certify()` is
        #: the test's stand-in for `kew sandbox certify` appending one.
        self.certifications = certifications
        self.audit_reads: list[Any] = []

    def certify(self) -> None:
        self.certifications += 1

    # route + provenance
    def get_config_override(self, field: str) -> dict[str, Any] | None:
        return self._overrides.get(field)

    # money
    def list_by_issue(self, issue_num: int) -> list[Any]:
        return [_Spend(self._issue_spend)] if self._issue_spend else []

    def list_by_item(self, item_id: str) -> list[Any]:
        return [_Spend(self._issue_spend)] if self._issue_spend else []

    def list_since(self, since: datetime) -> list[Any]:
        return [_Spend(self._daily_spend)] if self._daily_spend else []

    # governor
    def warm_prefixes(self, provider: str, now: datetime) -> set[str]:
        return set()

    # live evidence
    def lookup_readiness_probe(self, fingerprint: str, *, now: datetime | None = None):
        from kew.run_store_readiness import ProbeLookupResult, ProbeVerdict

        return ProbeLookupResult(verdict=ProbeVerdict.MISS, probe=None)

    # admission
    def get_fleet_stopped(self) -> dict[str, str] | None:
        return self._fleet_stopped

    def list_audit_events(self, **kwargs: Any) -> list[Any]:
        self.audit_reads.append(kwargs)
        if kwargs.get("event") == RUNTIME_EGRESS_CERTIFICATION_EVENT:
            if not self.certifications:
                return []
            return [
                _AuditEvent(RUNTIME_EGRESS_CERTIFICATION_EVENT, 0, self.certifications)
            ]
        return [_AuditEvent("approval_hold", n) for n in self._holds]

    def list_probes_awaiting_cleanup(self) -> list[Any]:
        return []


class _Spend:
    def __init__(self, usd: float) -> None:
        self.estimated_cost_usd = usd


class _AuditEvent:
    def __init__(self, event: str, issue_num: int, row_id: int = 0) -> None:
        self.event = event
        self.issue_num = issue_num
        self.id = row_id
        self.payload = {"estimate_usd": 1.0, "basis": "estimate"}


#: A runner name no build has. Declared once: every layer that can name a
#: runner is checked against the same absent one.
_ABSENT = "not-a-runner"


def _resolver() -> RouteResolver:
    """A resolver whose host facts are committed values, not probes."""
    return RouteResolver(
        cache=ProbeCache(
            HealthProbes(
                runtime_present=lambda _name: True,
                image_present=lambda _runtime, ref: ref == "kew-agent:test",
                env_set=lambda name: name == "ANTHROPIC_API_KEY",
            )
        ),
    )


def _config(**kwargs: Any) -> DispatchConfig:
    """An uncontained config: the posture a host with no docker can resolve."""
    sandbox = kwargs.pop(
        "sandbox",
        SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    return DispatchConfig(sandbox=sandbox, **kwargs)


def _plan(config: DispatchConfig | None = None, **kwargs: Any) -> dict[str, Any]:
    store = kwargs.pop("store", None) or FakeStore()
    return resolve_dispatch_plan(
        store, config or _config(), resolver=_resolver(), **kwargs
    )


# ── layer 1: the route, and which rule decided it ───────────────────────────


def test_a_routed_tier_names_the_tier_that_decided_the_runner() -> None:
    config = _config(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
            simple=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        )
    )
    plan = _plan(config, complexity="simple")
    assert plan["runner"] == {
        "value": "codex",
        "source": "kew.toml [routing].simple",
    }
    assert plan["model"]["value"] == "gpt-5.6-sol"
    assert plan["model"]["source"] == "kew.toml [routing].simple"


def test_a_tier_with_no_entry_falls_to_the_default_and_says_so() -> None:
    config = _config(
        routing=RoutingConfig(default=RouteEntry(runner="codex", model="gpt-5.6-sol"))
    )
    plan = _plan(config, complexity="complex")
    assert plan["runner"]["source"] == "kew.toml [routing].default"


def test_a_tier_entry_without_a_model_attributes_the_model_to_agent() -> None:
    """The two halves of a route can come from different layers."""
    config = _config(
        agent=AgentConfig(model="claude-opus-4-8"),
        routing=RoutingConfig(
            default="claude-code",
            medium=RouteEntry(runner="claude-code"),
        ),
    )
    plan = _plan(config, complexity="medium")
    assert plan["runner"]["source"] == "kew.toml [routing].medium"
    assert plan["model"] == {
        "value": "claude-opus-4-8",
        "source": "kew.toml [agent].model",
    }


def test_no_routing_config_reports_the_agent_pin() -> None:
    config = _config(agent=AgentConfig(runner="codex", model="gpt-5.6-sol"))
    plan = _plan(config)
    assert plan["runner"] == {"value": "codex", "source": "kew.toml [agent].runner"}


def test_a_runner_inferred_from_the_model_says_it_was_inferred() -> None:
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    plan = _plan(config)
    assert plan["runner"] == {
        "value": "claude-code",
        "source": "inferred from [agent].model",
    }


def test_a_db_override_outranks_the_file_in_the_source_it_reports() -> None:
    """The value on screen is the override's, so the file is the wrong answer."""
    store = FakeStore(
        overrides={"agent.model": {"set_by": "tom", "reason": "race", "value": "x"}}
    )
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")), store=store)
    assert plan["runner"]["source"] == "DB override (agent.model, set by tom)"


def test_an_override_steers_every_row_it_set_not_just_the_runners() -> None:
    """One screen, one answer. The model row and the runner row read the same
    field here, so a runner row naming the override beside a model row naming
    ``kew.toml [agent].model`` sent the operator to a file line that is not
    what is running — the exact thing ``_override_source`` exists to prevent
    (reviewer P2)."""
    store = FakeStore(
        overrides={"agent.model": {"set_by": "tom", "reason": "race", "value": "x"}}
    )
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")), store=store)
    assert plan["model"]["source"] == "DB override (agent.model, set by tom)"
    assert plan["runner"]["source"] == plan["model"]["source"]


def test_an_overridden_budget_cap_names_the_override_not_the_file() -> None:
    """``model_fields_set`` cannot answer this on its own: ``_apply_db_overrides``
    merges the override into the raw dict BEFORE validation, so an overridden
    cap looks hand-committed in the parsed model (reviewer P2)."""
    store = FakeStore(
        overrides={
            "budget.per_issue_usd": {"set_by": "tom", "value": 5.0},
            "budget.daily_usd": {"set_by": "ops", "value": 50.0},
        }
    )
    config = _config(budget=BudgetConfig(per_issue_usd=5.0))
    plan = _plan(config, item="2302", store=store)
    assert plan["budget"]["per_issue_cap_source"] == (
        "DB override (budget.per_issue_usd, set by tom)"
    )
    # ...and the daily cap, which the file never mentions, so "default" was the
    # other way to be wrong about a cap an override is holding.
    assert plan["budget"]["daily_cap_source"] == (
        "DB override (budget.daily_usd, set by ops)"
    )


def test_a_tier_model_override_is_not_reported_against_the_agent_pin() -> None:
    """The two halves can be steered by different fields, so each row asks
    about its own: a ``routing.simple`` override holds the model here, and
    ``agent.model`` — the field the plan used to ask about for every model —
    holds nothing."""
    store = FakeStore(
        overrides={"routing.simple": {"set_by": "tom", "value": {"runner": "codex"}}}
    )
    config = _config(
        agent=AgentConfig(model="claude-opus-4-8"),
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
            simple=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        ),
    )
    plan = _plan(config, complexity="simple", store=store)
    assert plan["model"]["source"] == "DB override (routing.simple, set by tom)"


def test_a_tier_without_a_model_asks_about_the_field_that_holds_it() -> None:
    """The mirror of the test above: the runner is steered by the tier and the
    model by ``[agent].model``, so an override on the tier may not be reported
    against the model row it does not set."""
    store = FakeStore(
        overrides={"routing.medium": {"set_by": "tom", "value": {"runner": "codex"}}}
    )
    config = _config(
        agent=AgentConfig(model="claude-opus-4-8"),
        routing=RoutingConfig(
            default="claude-code", medium=RouteEntry(runner="claude-code")
        ),
    )
    plan = _plan(config, complexity="medium", store=store)
    assert plan["runner"]["source"] == "DB override (routing.medium, set by tom)"
    assert plan["model"]["source"] == "kew.toml [agent].model"


def test_an_environment_runner_is_reported_as_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KEW_RUNNER`` beats both the file and a DB override, and says so.

    ``load_config`` applies the env layer last, so a row that named ``kew.toml
    [agent].runner`` pointed the operator at a file line that does not exist,
    and one that named the DB override pointed at a layer the env had already
    beaten (#2302 review).
    """
    monkeypatch.setenv("KEW_RUNNER", "codex")
    store = FakeStore(
        overrides={"agent.runner": {"set_by": "tom", "value": "claude-code"}}
    )
    config = _config(agent=AgentConfig(runner="codex", model="gpt-5.6-sol"))
    plan = _plan(config, store=store)
    assert plan["runner"] == {"value": "codex", "source": "env (KEW_RUNNER)"}
    # The model has no environment layer, so it is still read off the file.
    assert plan["model"]["source"] == "kew.toml [agent].model"


# ── layer 2: the lane ───────────────────────────────────────────────────────


def test_the_review_lane_resolves_the_reviewer_not_the_node_routing() -> None:
    """The review stage has its own pin; a routed runner is not its runner."""
    config = _config(
        routing=RoutingConfig(default=RouteEntry(runner="codex", model="gpt-5.6-sol")),
        review=ReviewConfig(model="claude-opus-4-8"),
    )
    plan = _plan(config, lane="review", complexity="simple")
    assert plan["lane"]["value"] == "review"
    assert plan["runner"]["value"] == "claude-code"
    assert plan["runner"]["source"] == "default (claude-code)"
    assert plan["model"] == {
        "value": "claude-opus-4-8",
        "source": "kew.toml [review].model",
    }


def test_the_remediation_lane_runs_as_the_reviewer_too() -> None:
    plan = _plan(_config(), lane="remediation")
    assert plan["lane"]["value"] == "remediation"
    assert plan["runner"]["value"] == "claude-code"
    assert plan["model"]["source"] == "claude-code's own default"


# The launcher's own resolution site is `review_launcher.review_model`.
# Mirrored rather than imported because `kew.service` may not import
# `kew.graph` (import-linter) — the same reason `review_lane_runner` mirrors
# `review_runner`.
@pytest.mark.parametrize(
    ("review_model", "agent_model", "source"),
    [
        ("claude-opus-4-8", "", "kew.toml [review].model"),
        ("claude-sonnet-4-5", "claude-opus-4-8", "kew.toml [review].model"),
        ("", "claude-opus-4-8", "kew.toml [agent].model"),
        ("", "", "claude-code's own default"),
    ],
)
def test_the_review_lane_model_mirrors_the_launchers_own_fallback(
    review_model: str, agent_model: str, source: str
) -> None:
    """``[review].model`` unset is not "no model": ``[agent].model`` is next.

    Reading only the ``[review]`` pin reported the model as '' — "claude-code's
    own default" — for a review that really runs on ``[agent].model``, and ''
    is also what the route fingerprint was composed from, so the stored probe
    evidence for the REAL review boundary could never be matched (reviewer P1).
    """
    config = _config(
        agent=AgentConfig(model=agent_model) if agent_model else AgentConfig(),
        review=ReviewConfig(model=review_model),
    )
    plan = _plan(config, lane="review")
    assert plan["model"] == {
        "value": review_model or agent_model,
        "source": source,
    }


@pytest.mark.parametrize(
    ("review_pin", "agent_pin"),
    [
        ("claude-opus-4-8", ""),
        ("claude-sonnet-4-5", "claude-opus-4-8"),
        ("", "claude-opus-4-8"),
        ("", ""),
    ],
)
def test_the_review_lane_model_mirrors_the_launcher(
    review_pin: str, agent_pin: str
) -> None:
    """One answer to "which model reviews?", held to the launcher that launches.

    ``_review_model`` is a MIRROR of ``review_launcher.review_model`` — the
    service layer cannot import ``kew.graph`` — and the case list above fixes
    the mirror's own answers. This is the test that keeps the mirror from
    becoming a second answer: dropping the ``[agent].model`` fallback from the
    launcher side left every one of those cases green, so the defect could be
    reintroduced from the launcher with nothing red (#2302 review).
    """
    from kew.graph.driver.review_launcher import review_model
    from kew.service.dispatch_plan import _review_model

    config = _config(
        agent=AgentConfig(model=agent_pin) if agent_pin else AgentConfig(),
        review=ReviewConfig(model=review_pin),
    )
    mirrored, _source, _field = _review_model(config, "claude-code")
    assert mirrored == review_model(config)


def test_the_review_lane_fingerprint_is_the_one_the_real_boundary_files_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model is not just a label: it composes the fingerprint evidence is
    keyed by, so a plan that reported '' pointed at a boundary no review has."""
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    asked: list[str] = []
    store = FakeStore()

    class _Probing(FakeStore):
        def lookup_readiness_probe(self, fingerprint, *, now=None):
            asked.append(fingerprint)
            return super().lookup_readiness_probe(fingerprint, now=now)

    probing = _Probing()
    review = resolve_dispatch_plan(probing, config, resolver=_resolver(), lane="review")
    implement = resolve_dispatch_plan(store, config, resolver=_resolver())
    # Same runner, same model, same uncontained posture — so the review lane
    # looks evidence up under the implement lane's boundary, not under a
    # model-less one.
    assert asked == [review["route_fingerprint"]["value"]]
    assert (
        review["route_fingerprint"]["value"]
        == (implement["route_fingerprint"]["value"])
    )


# ── layer 3: complexity, which no tracker call may supply ───────────────────


def test_an_absent_complexity_is_unknown_with_a_flag() -> None:
    plan = _plan()
    assert plan["complexity"]["value"] == "unknown"
    assert plan["complexity"]["known"] is False
    assert "no complexity given" in plan["complexity"]["source"]


def test_a_caller_supplied_complexity_is_attributed_to_the_caller() -> None:
    plan = _plan(complexity="medium")
    assert plan["complexity"] == {
        "value": "medium",
        "source": "the caller",
        "known": True,
    }


def test_a_nonsense_complexity_is_unknown_rather_than_a_crash() -> None:
    plan = _plan(complexity="gigantic")
    assert plan["complexity"]["known"] is False


def _queued(monkeypatch: pytest.MonkeyPatch, complexity: str) -> DispatchConfig:
    """A config whose repo's ready queue was last read holding #2060 at a tier."""
    issue = ReadyIssueDTO(
        issue_num=2060, item_id="2060", title="queued", complexity=complexity
    )
    monkeypatch.setitem(dashboard._ready_cache, "kew-dev/kew", (0.0, [issue]))
    return _config(project=ProjectConfig(repo="kew-dev/kew"))


def test_a_queued_item_resolves_the_ready_queue_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _queued(monkeypatch, "complex")
    config.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
        complex=RouteEntry(runner="codex", model="gpt-5.6-sol"),
    )
    plan = _plan(config, item="2060")
    assert plan["complexity"] == {
        "value": "complex",
        "source": "the ready queue's `complex` label",
        "known": True,
    }
    # The tier is resolved, not only printed: the route is the complex lane's.
    assert plan["runner"]["value"] == "codex"
    assert plan["model"]["value"] == "gpt-5.6-sol"


def test_the_caller_s_tier_wins_over_the_ready_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(_queued(monkeypatch, "complex"), item="2060", complexity="simple")
    assert plan["complexity"]["value"] == "simple"
    assert plan["complexity"]["source"] == "the caller"


def test_a_queued_item_with_no_tier_label_resolves_the_medium_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kew dispatch` classifies an unlabelled item as medium, so must the plan."""
    config = _queued(monkeypatch, "")
    config.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-opus-4-8"),
        medium=RouteEntry(runner="codex", model="gpt-5.6-sol"),
    )
    plan = _plan(config, item="2060")
    assert plan["complexity"]["value"] == "medium"
    assert plan["complexity"]["known"] is True
    assert plan["runner"]["value"] == "codex"
    assert plan["model"]["value"] == "gpt-5.6-sol"


def test_an_item_the_ready_queue_does_not_hold_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = _queued(monkeypatch, "complex")
    assert _plan(queued, item="2061")["complexity"]["known"] is False


def test_a_simple_and_medium_labelled_item_plans_as_dispatch_classifies_it() -> None:
    """The ready queue's tier precedence is the dispatcher's.

    complex, then simple, then medium — as ``selector.get_complexity`` reads it.
    """
    labels: list[GhLabel] = [{"name": "medium"}, {"name": "simple"}]
    assert dashboard._complexity_of(labels) == get_complexity(labels) == "simple"
    both = [*labels, {"name": "complex"}]
    assert dashboard._complexity_of(both) == get_complexity(both) == "complex"


# ── layer 4: containment, from the resolution dispatch itself uses ──────────


def test_containment_reports_the_posture_and_the_credential_names_only() -> None:
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))
    containment = plan["containment"]
    assert containment["posture"] == "uncontained"
    assert containment["refusal_reason"] is None
    assert containment["image"] is None
    assert containment["image_present"] is None
    names = containment["credential_names_present"]
    assert {entry["name"] for entry in names} == set(
        __import__("kew.runners.registry", fromlist=["x"]).runner_required_credentials(
            "claude-code"
        )
    )
    assert all(set(entry) == {"name", "set"} for entry in names)
    assert any(entry["set"] for entry in names)


def test_a_route_that_cannot_resolve_is_a_refusal_not_an_exception() -> None:
    """No container runtime here, so a contained lane is refused — and says why."""
    config = _config(
        sandbox=SandboxConfig(
            enabled=True, runtime="docker", images={"claude-code": "kew-agent:absent"}
        ),
        agent=AgentConfig(model="claude-opus-4-8"),
    )
    plan = _plan(config)
    assert plan["containment"]["posture"] == "refused"
    assert plan["containment"]["refusal_reason"]
    assert plan["route_fingerprint"]["value"] is None
    assert plan["live_status"]["value"] == "unverified"


@pytest.mark.parametrize(
    ("kwargs", "plan_kwargs"),
    [
        pytest.param(
            {"agent": AgentConfig(runner=_ABSENT, model="claude-opus-4-8")},
            {},
            id="agent-pin",
        ),
        pytest.param(
            {"routing": RoutingConfig(default=RouteEntry(runner=_ABSENT))},
            {},
            id="routing-entry",
        ),
    ],
)
def test_a_config_naming_an_unknown_runner_is_refused_not_an_exception(
    kwargs: dict[str, Any], plan_kwargs: dict[str, Any]
) -> None:
    """A runner the registry does not have is a READING, not a 500.

    No config validator checks a runner name against the registry — ``kew.config``
    may not import ``kew.runners`` — so ``runner = "nope"`` loads and raises at
    the resolution. That raise came out of ``_lane_route``, which sat OUTSIDE
    the refusal guard, so the read-only endpoint answered 500 instead of the
    ``posture: refused`` its own docstring promises (reviewer finding).

    Both layers that can name an unchecked runner are covered: the ``[agent]``
    pin and a routing entry. ``[review] runner`` is not one of them — its own
    validator takes ``claude-code`` or nothing, because only that runner emits
    a verdict event kew can read — so the review lane cannot reach here.
    """
    plan = _plan(_config(**kwargs), **plan_kwargs)
    assert plan["containment"]["posture"] == "refused"
    assert _ABSENT in plan["containment"]["refusal_reason"]
    # Nothing is invented in place of the route that could not be resolved.
    assert plan["runner"]["value"] == ""
    assert plan["model"]["value"] == ""
    assert plan["route_fingerprint"]["value"] is None
    assert plan["live_status"]["value"] == "unverified"
    assert plan["containment"]["credential_names_present"] == []
    # And the governor is not asked about a route that does not exist.
    assert plan["governor"] == {
        "would_swap": False,
        "reason": "no_route",
        "recommended_runner": "",
        "recommended_model": "",
        "est_savings_usd": 0.0,
        "source": "no route resolved, so no advice",
    }


def test_an_unknown_runner_still_answers_the_money_and_the_gates() -> None:
    """The rest of the reading survives one unresolvable field.

    The budget, the admission gates and the complexity flag have nothing to do
    with the runner, and a refusal in one row must not blank the panel.
    """
    plan = _plan(
        _config(agent=AgentConfig(runner=_ABSENT)),
        store=FakeStore(fleet_stopped={"reason": "deploy"}),
        complexity="simple",
    )
    assert plan["complexity"] == {
        "value": "simple",
        "source": "the caller",
        "known": True,
    }
    assert plan["budget"]["daily_cap_usd"] is not None
    assert plan["admission"]["fleet_stopped"] is True


def test_the_transport_is_the_one_the_launch_would_resolve() -> None:
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))
    assert plan["transport"]["value"] == "direct"
    assert plan["transport"]["source"] == "default (direct)"


def test_an_explicitly_pinned_direct_transport_is_not_reported_as_a_default() -> None:
    """A deliberate pin and an unset field resolve the same value, so the value
    cannot tell them apart — ``model_fields_set`` can, which is the rule
    ``_cap_source`` already follows in this module (reviewer P5)."""
    config = _config(
        agent=AgentConfig(model="claude-opus-4-8"),
        inference_transport=InferenceTransportConfig(mode="direct"),
    )
    plan = _plan(config)
    assert plan["transport"] == {
        "value": "direct",
        "source": "kew.toml [inference_transport].mode",
    }


def test_a_resolved_route_carries_the_fingerprint_evidence_is_filed_under() -> None:
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))
    assert plan["route_fingerprint"]["value"]
    assert "resolve_probe_route" in plan["route_fingerprint"]["source"]


def test_a_credential_refusal_reaches_the_reader_with_its_remedy_whole() -> None:
    """#2445: the cap cut the one instruction a refused lane exists to hand over.

    The longest refusal kew writes is the credential preflight's — the
    diagnosis, then ``Export ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (…)
    to dispatch sandboxed, or set [sandbox] dangerously_skip_containment =
    true`` — at 461 characters against a 300-character cap. The launchpad
    printed it, in the band above the table AND under Launch, ending "do not
    use subscription au".

    Resolved for real against a runtime patched present and an environment
    with neither credential name in it: the message asserted on is the one
    ``kew.sandbox`` composes, never a copy spelt out here.
    """
    config = _config(
        sandbox=SandboxConfig(enabled=True, runtime="docker"),
        agent=AgentConfig(runner="claude-code", model="claude-opus-4-8"),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            sandbox, "detect_runtime_cached", lambda *a, **k: "/usr/bin/docker"
        )
        for name in runner_required_credentials("claude-code"):
            patch.delenv(name, raising=False)
        plan = _plan(config)

    reason = plan["containment"]["refusal_reason"]
    assert plan["containment"]["posture"] == "refused"
    guidance = credential_missing_message(
        "claude-code", runner_required_credentials("claude-code")
    )
    assert guidance in reason, "the remedy survives the cap whole"
    # The remedy's last clause, and the whole word it ends on: the cut used to
    # land inside "au|th" with everything after it gone.
    assert reason.endswith("no container boundary."), "the line ends on a whole word"
    assert len(reason) <= REFUSAL_MAX_CHARS, "a reading stays bounded"
    assert "..." not in reason, "nothing was cut out of a refusal this length"


def test_an_over_long_refusal_loses_its_middle_and_never_half_a_word() -> None:
    """A refusal carrying a child's stderr is still bounded — from the MIDDLE.

    The remedy is written after the diagnosis, so a cut from the end drops the
    half an operator acts on (#2436's reading, applied to the refusal the
    launchpad prints). Both edges land on a space: half a word beside the
    ellipsis reads as a typo in the command the operator is told to type.
    """
    remedy = "Export ANTHROPIC_API_KEY to dispatch sandboxed."
    long = (
        "Sandbox: docker refused to resolve kew-agent:test\n"
        + "unreadable \x1b[31mstderr\x1b[0m noise " * 60
        + remedy
    )
    plan_module = __import__("kew.service.dispatch_plan", fromlist=["x"])

    def refuse(config: Any, **kwargs: Any) -> Any:
        raise RuntimeError(long)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plan_module, "resolve_probe_route", refuse)
        plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))

    reason = plan["containment"]["refusal_reason"]
    flat = "".join(char for char in " ".join(long.split()) if char.isprintable())
    assert len(reason) <= REFUSAL_MAX_CHARS
    assert "\n" not in reason, "the row that shows it is one line"
    # A child's stderr is untrusted text bound for a terminal and a TUI, so an
    # escape sequence in it never reaches either (`safe_hold_reason`'s rule).
    assert "\x1b" not in reason
    assert reason.startswith("Sandbox: docker refused to resolve kew-agent:test")
    assert reason.endswith(remedy), "the remedy is the part a reader acts on"
    head, _, tail = reason.partition("...")
    assert f"{head} " in flat, "the head stops at a word boundary"
    assert f" {tail}" in flat, "and the tail starts at one"


# ── layer 5: money ──────────────────────────────────────────────────────────


def test_each_cap_carries_the_layer_that_set_it() -> None:
    config = _config(
        agent=AgentConfig(model="claude-opus-4-8", max_budget_usd=12.0),
        budget=BudgetConfig(per_issue_usd=5.0),
    )
    plan = _plan(config, item="2302", store=FakeStore(issue_spend=1.5, daily_spend=9.0))
    budget = plan["budget"]
    assert budget["runtime_cap_usd"] == 12.0
    assert budget["runtime_cap_source"] == "kew.toml"
    assert budget["per_issue_cap_source"] == "kew.toml [budget].per_issue_usd"
    assert budget["daily_cap_source"] == "default"
    assert budget["issue_spent_usd"] == 1.5
    assert budget["daily_spent_usd"] == 9.0


def test_spend_that_cannot_be_keyed_to_an_item_is_none_not_zero() -> None:
    """No item selected yet: a dash, not a reassuring $0.00."""
    plan = _plan()
    assert plan["budget"]["issue_spent_usd"] is None


def test_a_tracker_key_keys_per_item_spend_without_the_zero_bucket() -> None:
    plan = _plan(item="SFD-12", store=FakeStore(issue_spend=2.0))
    assert plan["item"] == "SFD-12"
    assert plan["issue_num"] is None
    assert plan["budget"]["issue_spent_usd"] == 2.0


@pytest.mark.parametrize(
    "item", ["\u00b2", "\u0660", "\u0660\u0661", "\u2081", "#\u00b2"]
)
def test_a_non_ascii_digit_is_a_tracker_key_not_an_issue_number(item: str) -> None:
    """``str.isdigit`` is wider than ``int`` accepts and wider than a GitHub
    issue number is (reviewer P4).

    ``\u00b2`` passes ``isdigit`` and raises ``ValueError`` from ``int`` — a 500
    out of a reader whose contract is that nothing here raises. ``\u0660`` is
    worse than a crash: ``int`` reads it as 0, the sentinel bucket this
    function exists to keep per-item keys out of.
    """
    plan = _plan(item=item, store=FakeStore(issue_spend=2.0))
    assert plan["issue_num"] is None
    assert plan["item"] == item.lstrip("#")
    # And the per-item spend is still keyed, because a key kew cannot read as
    # an issue number is exactly the tracker case.
    assert plan["budget"]["issue_spent_usd"] == 2.0


def test_an_item_reference_that_is_only_a_hash_keys_nothing() -> None:
    plan = _plan(item="#", store=FakeStore(issue_spend=2.0))
    assert (plan["issue_num"], plan["item"]) == (None, None)


def test_the_command_is_composed_for_an_implement_issue_and_nothing_else() -> None:
    """The console copies this string verbatim (#2517), so the server says it."""
    assert _plan(item="#2302")["command"] == "kew dispatch 2302"
    assert _plan(item="SFD-12")["command"] is None
    assert _plan()["command"] is None
    assert _plan(item="2302", lane="review")["command"] is None


# ── layer 6: the governor, which advises and writes nothing ─────────────────


def test_the_governor_reports_its_reason_without_a_routing_config() -> None:
    plan = _plan()
    assert plan["governor"]["would_swap"] is False
    assert plan["governor"]["reason"] == "no_routing_config"
    assert "advice only" in plan["governor"]["source"]


def test_a_governor_that_raises_leaves_the_rest_of_the_plan_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("governor down")

    monkeypatch.setattr("kew.runners.governor.cache_governor", boom)
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))
    assert plan["governor"]["reason"] == "governor_error"
    assert plan["runner"]["value"] == "claude-code"


# ── layer 7: live status, from stored evidence only ─────────────────────────


def test_an_unprobed_boundary_is_unverified_and_says_there_is_no_evidence() -> None:
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")))
    assert plan["live_status"]["value"] == "unverified"
    assert plan["live_status"]["source"] == (
        "no stored readiness probe for this boundary"
    )
    assert plan["live_status"]["probed_at"] is None


# ── layer 8: admission ──────────────────────────────────────────────────────


def test_admission_reports_the_latch_the_hold_and_the_tightest_headroom() -> None:
    store = FakeStore(
        fleet_stopped={"stopped_at": "2026-10-07T00:00:00+00:00", "reason": "deploy"},
        holds=(2302,),
        issue_spend=0.1,
        daily_spend=90.0,
    )
    config = _config(budget=BudgetConfig(per_issue_usd=5.0, daily_usd=100.0))
    plan = _plan(config, item="2302", store=store)
    admission = plan["admission"]
    assert admission["fleet_stopped"] is True
    assert admission["fleet_stopped_reason"] == "deploy"
    assert admission["open_hold"] is True
    # per-issue headroom 4.90, daily headroom 10.00 — the smaller one binds.
    assert admission["headroom"]["usd"] == pytest.approx(4.9)
    assert admission["headroom"]["binds"] == "per_issue"


def test_the_daily_cap_binds_when_it_is_the_tighter_of_the_two() -> None:
    store = FakeStore(issue_spend=0.0, daily_spend=99.5)
    config = _config(budget=BudgetConfig(per_issue_usd=5.0, daily_usd=100.0))
    plan = _plan(config, item="2302", store=store)
    assert plan["admission"]["headroom"]["binds"] == "daily"
    assert plan["admission"]["headroom"]["usd"] == pytest.approx(0.5)


def test_the_hold_read_is_scoped_to_this_item_in_sql() -> None:
    """Bounded: the approval read asks for one issue, not the whole log."""
    store = FakeStore(holds=(2302,))
    _plan(item="2302", store=store)
    assert store.audit_reads == [
        {"limit": None, "event_prefix": "approval_", "issue_nums": [2302]}
    ]


def test_no_item_means_no_hold_question_was_answerable() -> None:
    store = FakeStore()
    plan = _plan(store=store)
    assert plan["admission"]["open_hold"] is None
    assert store.audit_reads == []


# ── the shape as a whole ────────────────────────────────────────────────────


def test_every_resolved_field_carries_a_source() -> None:
    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")), item="2302")
    for key in (
        "lane",
        "complexity",
        "runner",
        "model",
        "transport",
        "containment",
        "budget",
        "governor",
        "route_fingerprint",
        "live_status",
        "admission",
    ):
        assert plan[key].get("source"), f"{key} carries no source"
    assert plan["admission"]["headroom"]["source"]
    assert datetime.fromisoformat(plan["checked_at"]).tzinfo is not None


def test_a_second_read_reuses_the_resolution_rather_than_re_probing() -> None:
    """The TTL is the rule that keeps a revalidating page off the host."""
    calls: list[Any] = []
    plan_module = __import__("kew.service.dispatch_plan", fromlist=["x"])
    unpatched = plan_module.resolve_probe_route

    def counting(config: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return unpatched(config, **kwargs)

    resolver = _resolver()
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    store = FakeStore()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plan_module, "resolve_probe_route", counting)
        for _ in range(3):
            resolve_dispatch_plan(store, config, resolver=resolver)
    assert len(calls) == 1


def _refusing(reason: str, calls: list[int]):
    """A ``resolve_probe_route`` that refuses once with *reason*, then resolves."""
    plan_module = __import__("kew.service.dispatch_plan", fromlist=["x"])
    unpatched = plan_module.resolve_probe_route

    def refuse_once(config: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise SandboxRuntimeUnavailable("refused", reason=reason)
        return unpatched(config, **kwargs)

    return plan_module, refuse_once


@pytest.mark.parametrize(
    ("reason", "second_read_posture"),
    [
        # Read off the LEDGER: `kew sandbox certify` — the remedy the refusal
        # itself prints — makes it false immediately, so once a certification
        # lands the next read must re-derive rather than serve the held
        # refusal (reviewer finding on #2319's merge).
        (REFUSAL_OPENSHELL_UNCERTIFIED, "uncontained"),
        # Read off the HOST: "no docker here" does not become true again within
        # the minute, which is the TTL's whole justification — still cached,
        # and certifying does not touch it.
        (REFUSAL_RUNTIME_UNAVAILABLE, "refused"),
    ],
)
def test_certifying_releases_only_the_ledger_refusal(
    reason: str, second_read_posture: str
) -> None:
    calls: list[int] = []
    plan_module, refuse_once = _refusing(reason, calls)
    resolver = _resolver()
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    store = FakeStore()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plan_module, "resolve_probe_route", refuse_once)
        first = resolve_dispatch_plan(store, config, resolver=resolver)
        store.certify()  # the remedy the refusal printed
        second = resolve_dispatch_plan(store, config, resolver=resolver)
    assert first["containment"]["posture"] == "refused"
    assert second["containment"]["posture"] == second_read_posture
    assert len(calls) == (2 if second_read_posture == "uncontained" else 1)


def test_an_uncertified_host_is_not_re_probed_until_something_is_certified() -> None:
    """A held ledger refusal costs a ledger row per read, never the host.

    The refusal is re-checked every read — that is what makes `kew sandbox
    certify` visible immediately — but through the certification MARK, not by
    re-running the resolution. Re-deriving it instead put `docker image
    inspect` and `openshell --version` behind every read of an unrate-limited
    endpoint the launchpad re-reads on each complexity change, which is the
    exact behaviour ROUTE_TTL_S exists to prevent (#2302 review).
    """
    calls: list[int] = []
    plan_module = __import__("kew.service.dispatch_plan", fromlist=["x"])

    def always_refuse(config: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise SandboxRuntimeUnavailable("refused", reason=REFUSAL_OPENSHELL_UNCERTIFIED)

    resolver = _resolver()
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    store = FakeStore()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plan_module, "resolve_probe_route", always_refuse)
        for _ in range(3):
            plan = resolve_dispatch_plan(store, config, resolver=resolver)
    assert plan["containment"]["posture"] == "refused"
    assert len(calls) == 1
    marks = [
        read
        for read in store.audit_reads
        if read.get("event") == RUNTIME_EGRESS_CERTIFICATION_EVENT
    ]
    assert [read["limit"] for read in marks] == [1, 1, 1]


def test_a_ledger_refusal_whose_mark_cannot_be_read_is_never_held() -> None:
    """ "I could not check" is not "nothing changed" — so it re-derives."""
    calls: list[int] = []
    plan_module, refuse_once = _refusing(REFUSAL_OPENSHELL_UNCERTIFIED, calls)

    class UnreadableLedger(FakeStore):
        def list_audit_events(self, **kwargs: Any) -> list[Any]:
            if kwargs.get("event") == RUNTIME_EGRESS_CERTIFICATION_EVENT:
                raise RuntimeError("ledger unreadable")
            return super().list_audit_events(**kwargs)

    resolver = _resolver()
    config = _config(agent=AgentConfig(model="claude-opus-4-8"))
    store = UnreadableLedger()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plan_module, "resolve_probe_route", refuse_once)
        first = resolve_dispatch_plan(store, config, resolver=resolver)
        second = resolve_dispatch_plan(store, config, resolver=resolver)
    assert first["containment"]["posture"] == "refused"
    assert second["containment"]["posture"] == "uncontained"
    assert len(calls) == 2


def test_the_plan_is_pure_json_values() -> None:
    """It is serialised straight to the browser, so no object may ride along."""
    import json

    plan = _plan(_config(agent=AgentConfig(model="claude-opus-4-8")), item="2302")
    assert json.loads(json.dumps(plan)) == plan


def test_a_known_complexity_now_is_the_moment_the_caller_passed() -> None:
    moment = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
    plan = _plan(complexity="simple", now=moment)
    assert plan["checked_at"] == moment.isoformat()
