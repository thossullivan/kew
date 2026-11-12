"""The prompt catalog: which prompt each agent role runs (#3018).

Eleven run kinds build their prompts in eleven places. :data:`ROLES` names one
entry per agent role — the run kinds it covers, where its base prompt comes
from, whether ``[prompt]`` applies to it, and whether an operator profile may
add to it — so a read-only surface can say which prompt a role runs without
anyone reading the builders.

:func:`render_preview` renders a role's BASE prompt over the fixed sample item
defined here, without the operator's ``[prompt]`` template and append. Neither
it nor :func:`role_preview` reads a tracker or a checkout: a preview carries no
real item content. :func:`role_preview` renders what the role's launch renders
instead of the base: the served ``[prompt]`` template and append for a role
they apply to (#3071), and the role's active approved profile, read off the
host store, on top (#3023). A configuration the launch would refuse leaves the
base text with the refusal beside it, and ``prompt_config_rendered`` says
which of the two configurations the text was rendered with.

The builders themselves are not imported here. Every one of them reaches
``kew.graph`` through the dispatch stack (``kew.prompt`` imports
``kew.worktree``), which ``kew.service`` may not import (import-linter). They
are bound in :mod:`kew.prompt_previews` and handed in as *renderers* — by
``kew serve`` to ``create_app``, the way ``readiness_checks`` is.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jinja2 import TemplateError
from pydantic import BaseModel, Field

from kew.config import DispatchConfig, PromptConfig
from kew.prompt_addendum import (
    ProfileAddendum,
    PromptConfigurationError,
    refuse_custom_template_with_addendum,
)
from kew.review_contract import (
    ReviewerPromptReadError,
    ReviewerPromptText,
    resolve_reviewer_prompt,
    reviewer_base_ref,
    reviewer_prompt_artifact,
)

if TYPE_CHECKING:
    from kew.run_store import RunStore

#: ``deployed-artifact`` is the pinned artifact's role when the served project
#: deploys its own copy, which then wins over the pin exactly as it does at
#: review launch (``review_contract.resolve_reviewer_prompt``).
#: ``config-template`` is the ``[prompt] template`` kew.toml configures, in
#: effect instead of the packaged one for a role ``[prompt]`` applies to.
SourceKind = Literal[
    "packaged-template",
    "config-template",
    "pinned-artifact",
    "deployed-artifact",
    "code",
]

#: Run kinds that start no agent of their own, so no prompt. ``review_stage``
#: is the detached review child: the reviewer and remediation it starts run
#: under their own kinds (``run_store.REVIEW_STAGE_RUN_KIND``).
NON_AGENT_RUN_KINDS = frozenset({"review_stage"})


@dataclass(frozen=True)
class PromptRole:
    """One agent role and where its base prompt comes from.

    ``source_name`` is the packaged template's file name, the pinned
    artifact's manifest name, or the builder function for a ``code`` source.
    """

    role: str
    run_kinds: tuple[str, ...]
    source_kind: SourceKind
    source_name: str
    prompt_config_applies: bool
    profile_applies: bool


ROLES: tuple[PromptRole, ...] = (
    PromptRole(
        "dispatch",
        ("dispatch", "loop", "delegation"),
        "packaged-template",
        "dispatch.md.j2",
        prompt_config_applies=True,
        profile_applies=True,
    ),
    PromptRole(
        "adversarial_review",
        ("adversarial_review",),
        "pinned-artifact",
        "adversarial-reviewer",
        prompt_config_applies=False,
        profile_applies=True,
    ),
    PromptRole(
        "review_remediation",
        ("review_remediation", "review"),
        "packaged-template",
        "review.md.j2",
        prompt_config_applies=False,
        profile_applies=True,
    ),
    PromptRole(
        "resolve",
        ("resolve",),
        "packaged-template",
        "resolve.md.j2",
        prompt_config_applies=False,
        profile_applies=True,
    ),
    PromptRole(
        "ci_fix",
        ("ci_fix",),
        "packaged-template",
        "ci-fix.md.j2",
        prompt_config_applies=False,
        profile_applies=True,
    ),
    PromptRole(
        "triage",
        ("triage",),
        "packaged-template",
        "triage.md.j2",
        prompt_config_applies=False,
        profile_applies=False,
    ),
    PromptRole(
        "planning",
        ("planning",),
        "packaged-template",
        "epic-plan.md.j2",
        prompt_config_applies=False,
        profile_applies=False,
    ),
    PromptRole(
        "acceptance_author",
        ("acceptance_author",),
        "code",
        "acceptance_author.author_prompt",
        prompt_config_applies=False,
        profile_applies=False,
    ),
    PromptRole(
        "loop_continuation",
        ("loop",),
        "code",
        "loops.delegate._build_continuation_prompt",
        prompt_config_applies=False,
        profile_applies=False,
    ),
    PromptRole(
        "readiness_probe",
        ("readiness_probe",),
        "code",
        "probe_route.PROBE_PROMPT",
        prompt_config_applies=False,
        profile_applies=False,
    ),
)

_BY_ROLE = {entry.role: entry for entry in ROLES}


class PreviewUnavailable(LookupError):
    """No renderer was handed in for a role that exists."""

    def __init__(self, role: str) -> None:
        super().__init__(role)
        self.role = role


class UnknownPromptRole(LookupError):
    """No role in :data:`ROLES` carries this id."""

    def __init__(self, role: str) -> None:
        super().__init__(role)
        self.role = role


class PromptSourceUnreadable(RuntimeError):
    """The base ref deploys a copy of *role*'s prompt that Git cannot read.

    The prompt a launch would run is then unknown, so no preview stands in
    for it — neither another copy nor the packaged default."""

    def __init__(self, role: str, reason: str) -> None:
        super().__init__(f"{role}: {reason}")
        self.role = role
        self.reason = reason


class PromptSourceDTO(BaseModel):
    kind: SourceKind
    name: str
    version: int | None = None
    sha256: str | None = None
    """The digest of the bytes the preview starts with, never a pin alone."""
    pin_mismatch: bool = False
    """True when the packaged artifact's bytes are not the manifest's pin."""
    base_ref: str | None = None
    """The Git ref a deployed reviewer copy is read from, as at launch."""
    base_commit: str | None = None
    """The commit *base_ref* resolved to for this read; None when it did not."""


class PromptRoleDTO(BaseModel):
    role: str
    run_kinds: list[str]
    source: PromptSourceDTO
    prompt_config_applies: bool
    profile_applies: bool


class AddendumSpanDTO(BaseModel):
    """Where the operator addendum block sits in a preview.

    ``start`` and ``end`` are character offsets (Unicode code points) into
    ``preview``: from the addendum's ``## Operator instructions`` heading
    through the last character of its body. They are where the builder put the
    approved body, never re-inferred from the text, so a body with a ``## ``
    line of its own stays inside the block, and a body that imitates a fixed
    heading stays marked as the addendum.
    """

    start: int
    end: int


class PromptPreviewDTO(PromptRoleDTO):
    preview: str
    preview_sha256: str
    variant: dict[str, str | bool] = Field(default_factory=dict)
    """Which shape of the role's prompt *preview* is, where the role has more
    than one — e.g. ``{"contained": true, "publication": "host"}`` for
    dispatch. Empty for a role with one shape."""
    addendum: AddendumSpanDTO | None = None
    """The operator addendum block in *preview*; None when none is rendered."""
    profile_generation: int | None = None
    """The role's profile generation the preview's profile was read at — the
    one ``GET /api/prompts/{role}/versions`` serves beside its history — so a
    reader can tell a preview and a history from two decisions apart. None for
    a role no profile applies to, without a host store, or when a decision
    landed inside every read."""
    prompt_config_rendered: bool
    """True when *preview* was rendered with the served ``[prompt]``
    configuration, its template and append; False when it is the built-in
    base without them: a role ``[prompt]`` never applies to, a refused profile
    (its launch refuses, so the preview stays the base), or a ``refusal`` in
    *variant*. ``prompt_config_applies`` says only that the role reads
    ``[prompt]``; this says whether THIS text did (#3071)."""


# --- The fixed sample item -------------------------------------------------

SAMPLE_ISSUE_NUM = 1
SAMPLE_TITLE = "Sample item"
SAMPLE_BODY = (
    "A short sample body. It stands in for a real item so the preview shows "
    "the prompt's shape without carrying anyone's content.\n\n"
    "### Acceptance Criteria\n\n"
    "- [ ] The sample behaves as described.\n"
)
#: The per-render untrusted-fence sentinel is random; the preview pins the
#: builder's so the same base renders the same bytes, and so the same sha256,
#: every read. An artifact's own bytes are never rewritten (#3029).
_SENTINEL_RE = re.compile(r"<<<KEW-UNTRUSTED-[0-9a-f]+>>>")
_SAMPLE_SENTINEL = "<<<KEW-UNTRUSTED-sample>>>"


@dataclass(frozen=True)
class PreviewInputs:
    """What a renderer may read: the served config and the project's root.

    ``repo_root`` is where a launch would look for deployed copies (the
    reviewer artifact, the rubric the self-check quotes); None looks nowhere.
    ``reviewer_prompt`` is the reviewer artifact this request resolved, so the
    preview and its ``source`` describe one copy; None resolves it afresh.
    ``profile_addendum`` is the approved addendum :func:`role_preview` read off
    the host store; each profile role's renderer hands it to the builder its
    launch calls, so the preview places it where the launch does (#3023).
    None is the base.
    """

    config: DispatchConfig
    repo_root: Path | None = None
    reviewer_prompt: ReviewerPromptText | None = None
    profile_addendum: ProfileAddendum | None = None


@dataclass(frozen=True)
class RenderedPrompt:
    """One role's base prompt, and which of its shapes it is.

    ``verbatim_prefix`` is the head of *text* through the loaded artifact
    text (an operator addendum may render before it), kept byte-for-byte:
    only what the builder generated after it is normalized.
    """

    text: str
    variant: Mapping[str, str | bool] = field(default_factory=dict)
    verbatim_prefix: str = ""


#: One role's base-prompt builder, bound over the sample item.
PromptRenderer = Callable[[PreviewInputs], RenderedPrompt]
PromptRenderers = Mapping[str, PromptRenderer]


def _base_config(config: DispatchConfig | None) -> DispatchConfig:
    """*config* without its ``[prompt]`` overrides: the preview is the base."""
    source = config if config is not None else DispatchConfig()
    return source.model_copy(update={"prompt": PromptConfig()})


def _profile_refused(variant: Mapping[str, str | bool]) -> bool:
    label = variant.get("profile")
    return isinstance(label, str) and label.startswith("refused: ")


def _served_config(config: DispatchConfig | None, served: bool) -> DispatchConfig:
    """The configuration a preview renders with: what its launch reads.

    *served* is whether the served ``[prompt]`` applies. A role ``[prompt]``
    applies to renders with it, so the preview carries an append its launch
    carries and meets every refusal its composer makes (#3071). A refused
    profile already names the launch's refusal, so that preview stays the
    base, without ``[prompt]`` or profile, and *served* is False.
    """
    if not served:
        return _base_config(config)
    return config if config is not None else DispatchConfig()


def _refused_render(
    role: str,
    renderers: PromptRenderers,
    inputs: PreviewInputs,
    error: Exception,
) -> RenderedPrompt:
    """The base preview of a launch whose composer refuses *inputs*' config.

    The launch builds no prompt, so the base stands in for it, without the
    ``[prompt]`` overrides or the profile, and ``refusal`` says why (#3071):
    the composer's refusal, or the custom template its launch cannot render.
    """
    base = replace(inputs, config=_base_config(inputs.config), profile_addendum=None)
    rendered = _render(role, renderers, base)
    if isinstance(error, PromptConfigurationError):
        refusal = str(error)
    elif isinstance(error, TemplateError):
        refusal = f"the [prompt] template cannot be rendered: {error}"
    else:
        refusal = (
            f"the [prompt] template cannot be rendered: {type(error).__name__}: {error}"
        )
    return RenderedPrompt(rendered.text, {**rendered.variant, "refusal": refusal})


def _served_refusal(inputs: PreviewInputs, error: Exception) -> bool:
    """Whether *error*, raised rendering *inputs*, is its launch's refusal.

    The composer refuses with :class:`PromptConfigurationError`, Jinja with a
    :class:`TemplateError`. A custom ``[prompt] template`` is the operator's
    code: its expressions and the file itself can raise anything (a zero
    division, an undecodable file), and its launch fails on each the same way,
    so with one served, any error but an unknown or unrenderable role is its.
    """
    if isinstance(error, (PromptConfigurationError, TemplateError)):
        return True
    if isinstance(error, (UnknownPromptRole, PreviewUnavailable)):
        return False
    return bool(inputs.config.prompt.template)


def get_role(role: str) -> PromptRole:
    """The :data:`ROLES` entry for *role*; :class:`UnknownPromptRole` if none."""
    entry = _BY_ROLE.get(role)
    if entry is None:
        raise UnknownPromptRole(role)
    return entry


def _render(
    role: str, renderers: PromptRenderers, inputs: PreviewInputs
) -> RenderedPrompt:
    get_role(role)
    renderer = renderers.get(role)
    if renderer is None:
        raise PreviewUnavailable(role)
    rendered = renderer(inputs)
    prefix = rendered.verbatim_prefix
    if not rendered.text.startswith(prefix):
        prefix = ""
    rest = _SENTINEL_RE.sub(_SAMPLE_SENTINEL, rendered.text[len(prefix) :])
    return RenderedPrompt(prefix + rest, rendered.variant)


def render_preview(
    role: str,
    renderers: PromptRenderers,
    config: DispatchConfig | None = None,
    repo_root: Path | None = None,
) -> str:
    """*role*'s base prompt rendered over the fixed sample item.

    *config* supplies the project's commands and labels; its ``[prompt]``
    section is dropped, so this is never the text a launch with a template or
    append renders: :func:`role_preview` renders that. *repo_root* is the
    served project's root, where a deployed copy is looked for. Raises
    :class:`UnknownPromptRole` for an unknown id and :class:`PreviewUnavailable`
    when *renderers* has none for it.
    """
    return _render(role, renderers, PreviewInputs(_base_config(config), repo_root)).text


def _reviewer_prompt(
    entry: PromptRole, repo_root: Path | None, config: DispatchConfig | None
) -> ReviewerPromptText | None:
    """The reviewer artifact *entry* runs, resolved once; None for other roles.

    Read from *config*'s ``branches.base`` (``main`` when unset), the base ref
    the review launch reads, never from the working tree (#3087), so the
    preview shows the bytes a review would run under. A deployed copy the ref
    lists but Git cannot read raises :class:`PromptSourceUnreadable`."""
    if entry.source_kind != "pinned-artifact":
        return None
    base = reviewer_base_ref(config.branches.base if config is not None else None)
    try:
        return resolve_reviewer_prompt(repo_root, base)
    except ReviewerPromptReadError as exc:
        raise PromptSourceUnreadable(entry.role, str(exc)) from exc


def _source(
    entry: PromptRole,
    loaded: ReviewerPromptText | None,
    config: DispatchConfig | None = None,
) -> PromptSourceDTO:
    template = config.prompt.template if config is not None else ""
    if entry.prompt_config_applies and template:
        return PromptSourceDTO(kind="config-template", name=template)
    if loaded is None:
        return PromptSourceDTO(kind=entry.source_kind, name=entry.source_name)
    if loaded.deployed is not None:
        return PromptSourceDTO(
            kind="deployed-artifact",
            name=loaded.deployed,
            sha256=loaded.sha256,
            base_ref=loaded.base_ref,
            base_commit=loaded.base_commit,
        )
    artifact = reviewer_prompt_artifact()
    return PromptSourceDTO(
        kind=entry.source_kind,
        name=artifact.name,
        version=artifact.version,
        sha256=loaded.sha256,
        pin_mismatch=loaded.sha256 != artifact.sha256,
        base_ref=loaded.base_ref,
        base_commit=loaded.base_commit,
    )


def describe_role(
    entry: PromptRole,
    repo_root: Path | None = None,
    config: DispatchConfig | None = None,
) -> PromptRoleDTO:
    """*entry* as the API serves it, manifest or deployed-copy facts filled in.

    *config* names the base branch a deployed reviewer copy is read from, and
    the ``[prompt] template`` that replaces the packaged one for a role
    ``[prompt]`` applies to: the source named is the one in effect."""
    return _describe(entry, _reviewer_prompt(entry, repo_root, config), config)


def _describe(
    entry: PromptRole,
    loaded: ReviewerPromptText | None,
    config: DispatchConfig | None = None,
) -> PromptRoleDTO:
    return PromptRoleDTO(
        role=entry.role,
        run_kinds=list(entry.run_kinds),
        source=_source(entry, loaded, config),
        prompt_config_applies=entry.prompt_config_applies,
        profile_applies=entry.profile_applies,
    )


def list_roles(
    repo_root: Path | None = None, config: DispatchConfig | None = None
) -> list[PromptRoleDTO]:
    """Every role in :data:`ROLES`, in catalog order."""
    return [describe_role(entry, repo_root, config) for entry in ROLES]


def _profile(
    entry: PromptRole, store: RunStore | None, config: DispatchConfig | None
) -> tuple[ProfileAddendum | None, dict[str, str | bool], int | None]:
    """The addendum *entry*'s launch would render off *store*, its variant,
    and the role generation it was read at.

    *config* is the served configuration, ``[prompt]`` included: a role it
    applies to is refused here when its launch would refuse the pair, so the
    preview never shows a runnable prompt the launch would stop. A role no
    profile applies to reads nothing, adds no variant key and has no
    generation.
    """
    if not entry.profile_applies:
        return None, {}, None
    # Local: kew.service.prompt_profiles reads ROLES from this module.
    from kew.service.prompt_profiles import preview_profile

    addendum, label, generation = preview_profile(store, entry.role)
    if entry.prompt_config_applies:
        try:
            refuse_custom_template_with_addendum(config or DispatchConfig(), addendum)
        except PromptConfigurationError as exc:
            return None, {"profile": f"refused: {exc}"}, generation
    return addendum, {"profile": label}, generation


def role_preview(
    role: str,
    renderers: PromptRenderers,
    config: DispatchConfig | None = None,
    repo_root: Path | None = None,
    store: RunStore | None = None,
) -> PromptPreviewDTO:
    """One role with its prompt preview, its sha256 and its variant.

    The reviewer artifact is resolved once, here, and that one copy is both
    the ``source`` and the text the preview starts with: resolving it twice
    could pair one copy's sha256 with another copy's text if the deployed
    file changed in between.

    *store* is the host store a launch reads its role's approved profile from
    (#3023). An active profile the launch would render is rendered here by
    the launch's own builder, and the variant names its version; one the
    launch would refuse — the row itself, or the row with the served
    ``[prompt] template`` — leaves the base preview, and the variant says why.
    A role ``[prompt]`` applies to renders with the served ``[prompt]``; a
    configuration its composer refuses (an append or template under host
    publication) leaves the base preview too, with ``refusal`` saying why and
    the profile state kept beside it (#3071); ``prompt_config_rendered`` says
    whether the text returned was rendered with the served ``[prompt]``.
    ``addendum`` is where the rendered addendum block sits in the preview, as
    the builder placed it (:class:`_AddendumMarks`), and
    ``profile_generation`` is the role generation that profile was read at.
    """
    entry = get_role(role)
    loaded = _reviewer_prompt(entry, repo_root, config)
    described = _describe(entry, loaded, config)
    addendum, profile_variant, generation = _profile(entry, store, config)
    marks = _AddendumMarks.around(addendum)
    served = entry.prompt_config_applies and not _profile_refused(profile_variant)
    inputs = PreviewInputs(
        _served_config(config, served), repo_root, loaded, marks.addendum
    )
    try:
        rendered = _render(role, renderers, inputs)
    except Exception as exc:  # noqa: BLE001 - _served_refusal re-raises the rest
        if not _served_refusal(inputs, exc):
            raise
        marks, served = _AddendumMarks(None), False
        rendered = _refused_render(role, renderers, inputs, exc)
    # A renderer that refuses without raising (a containment posture that
    # cannot be met) returns no text it rendered with the served [prompt].
    served = served and not rendered.variant.get("refusal")
    text, span = marks.lift(rendered.text)
    return PromptPreviewDTO(
        **described.model_dump(),
        preview=text,
        preview_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        variant={**rendered.variant, **profile_variant},
        addendum=span,
        profile_generation=generation,
        prompt_config_rendered=served,
    )


#: The heading every builder renders an addendum block under: ``dispatch.md.j2``,
#: ``_operator_addendum.md.j2`` and ``review_dispatch.operator_addendum_section``.
_ADDENDUM_HEADING = "## Operator instructions — "


@dataclass(frozen=True)
class _AddendumMarks:
    """Two markers the preview puts around an addendum's body, then lifts out.

    The launch's own builder places the body; the markers record where, so the
    addendum's extent is never re-inferred from the rendered text. They are
    drawn fresh for each read, so neither a body nor a base can spell them,
    and they sit inside the body's surrounding whitespace, so a builder that
    strips the body renders the same bytes around them. Lifting them out
    leaves exactly the text the launch renders.
    """

    addendum: ProfileAddendum | None
    start: str = ""
    end: str = ""

    @classmethod
    def around(cls, addendum: ProfileAddendum | None) -> _AddendumMarks:
        if addendum is None:
            return cls(None)
        token = secrets.token_hex(16)
        start = f"[[kew-addendum-start-{token}]]"
        end = f"[[kew-addendum-end-{token}]]"
        body = addendum.body
        core = body.strip()
        lead = body[: len(body) - len(body.lstrip())]
        trail = body[len(lead) + len(core) :]
        marked = replace(addendum, body=f"{lead}{start}{core}{end}{trail}")
        return cls(marked, start, end)

    def lift(self, text: str) -> tuple[str, AddendumSpanDTO | None]:
        """*text* without the markers, and the addendum block they bound.

        The block runs from the addendum heading before the body through the
        body's last character. A render that dropped the body, or carried it
        twice, marks nothing rather than guess.
        """
        if not self.start:
            return text, None
        at, stop = text.find(self.start), text.find(self.end)
        once = text.count(self.start) == 1 and text.count(self.end) == 1
        if not once or stop < at:
            return text.replace(self.start, "").replace(self.end, ""), None
        body_end = stop - len(self.start)
        lifted = (
            text[:at] + text[at + len(self.start) : stop] + text[stop + len(self.end) :]
        )
        heading = text.rfind(_ADDENDUM_HEADING, 0, at)
        return lifted, AddendumSpanDTO(
            start=heading if heading >= 0 else at, end=body_end
        )
