"""Plan→issues on-ramp: manifest schema, draft validation, governed creation.

`kew epic plan` splits the motion kew's way: a dispatched agent turns an
implementation plan into a JSON manifest of draft issues (judgment); this
module validates the draft DAG before anything touches GitHub, then creates
the issues itself (guardrails). The schema has no free-form label field, so
`ready` can never be applied; `epic:<name>` and `<!-- kew:depends -->`
markers are written by kew, not the agent.

Creation also writes GitHub's own keys for the same two facts — the nodes
become sub-issues of an epic container issue and each `depends_on` becomes a
`blocked_by` relation (see `link_native_relations`). The label and the marker
stay for one release so a repository without the relations reads the same
epic.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kew import _TEMPLATES_DIR
from kew.criteria import CRITERIA_MAX, CRITERIA_MIN, criteria_problems
from kew.dependencies import (
    PLAN_NODE_MARKER,
    PlanNodeIdentity,
    format_epic_container_marker,
    format_plan_node_marker,
    plan_node_identity,
)
from kew.graph.adapters.github_native import (
    GitHubNativeSource,
    probe_native_linkage,
)
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.validate import validate
from kew.progress import _warn
from kew.publication import sanitize_publication_error
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.config import DispatchConfig

MANIFEST_RELPATH = ".kew/epic-plan-manifest.json"

# Dispatchability Gate: a plan node over this many target files must be
# re-split into multiple issues before creation (see plan-to-issues SKILL.md).
TARGET_FILES_LIMIT = 10

Priority = Literal["p0-critical", "p1-high", "p2-medium", "p3-low"]
Complexity = Literal["simple", "medium", "complex"]
IssueType = Literal["feature", "bug", "refactor", "test", "docs"]


class DraftIssue(BaseModel):
    """One issue the planning agent proposes.

    No free-form labels — the three enum axes are the only labels the agent
    can influence, so `ready` is unappliable by construction.
    """

    model_config = ConfigDict(extra="forbid")

    # An empty id could never be matched back to the issue made for it.
    local_id: str = Field(min_length=1)
    title: str
    body: str
    priority: Priority
    complexity: Complexity
    type: IssueType
    depends_on: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(default_factory=list)


class EpicPlanManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epic: str
    plan_path: str
    issues: list[DraftIssue] = Field(min_length=1)


class ManifestError(Exception):
    """Manifest unreadable or schema-invalid."""


def load_manifest(path: Path) -> EpicPlanManifest:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Cannot read manifest {path}: {exc}") from exc
    try:
        return EpicPlanManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(f"Manifest schema invalid: {exc}") from exc


def draft_graph(manifest: EpicPlanManifest) -> Graph:
    """Draft DAG over local ids; Edge(src=dep, dst=dependent) per domain contract."""
    nodes = [
        Node(
            id=i.local_id,
            title=i.title,
            status=NodeStatus.TODO,
            epic_ref=manifest.epic,
        )
        for i in manifest.issues
    ]
    edges = [
        Edge(src=dep, dst=i.local_id) for i in manifest.issues for dep in i.depends_on
    ]
    return Graph(nodes, edges)


def _issue_problems(
    issue: DraftIssue, manifest: EpicPlanManifest, existing: set[str], seen: set[str]
) -> list[str]:
    """Per-issue draft checks (identity, plan reference, criteria, gates)."""
    problems: list[str] = []
    if issue.local_id in seen:
        problems.append(f"duplicate local_id: {issue.local_id!r}")
    if issue.local_id in existing:
        # A new node cannot reuse an existing epic node's identity: a
        # depends_on pointing at it would be ambiguous between the new node
        # and the live issue.
        problems.append(
            f"local_id {issue.local_id!r} collides with an existing epic "
            "node -- pick a fresh id"
        )
    if manifest.plan_path not in issue.body:
        problems.append(
            f"{issue.local_id!r}: body does not reference the plan file "
            f"({manifest.plan_path})"
        )
    if len(issue.target_files) > TARGET_FILES_LIMIT:
        # Dispatchability Gate: a plan node over the limit must be re-split into
        # multiple issues before creation, never carried through as one
        # oversized node (see plan-to-issues SKILL.md). The governed run
        # (plan_run.py) feeds a re-split instruction back to the agent for one
        # retry before this backstop hard-fails.
        problems.append(
            f"{issue.local_id!r}: {len(issue.target_files)} target_files "
            f"exceeds limit {TARGET_FILES_LIMIT} -- re-split into "
            f"<={TARGET_FILES_LIMIT}-file nodes"
        )
    problems.extend(criteria_problems(issue.body, repr(issue.local_id)))
    problems.extend(_marker_problems(issue))
    return problems


def _marker_problems(issue: DraftIssue) -> list[str]:
    """Refuse a draft body that already carries one of kew's own markers."""
    problems: list[str] = []
    if "kew:depends" in issue.body:
        # parse_depends_ids unions every marker in the body, so an
        # agent-written marker would add edges beside the one kew appends and
        # silently corrupt the DAG. Markers are kew's to write.
        problems.append(
            f"{issue.local_id!r}: body contains a kew:depends marker — "
            "declare dependencies in depends_on; kew writes the markers"
        )
    if PLAN_NODE_MARKER in issue.body:
        # The plan-node marker is how a re-applied manifest finds the issue it
        # already made; a hand-written one could claim another node's identity.
        problems.append(
            f"{issue.local_id!r}: body contains a {PLAN_NODE_MARKER} marker — "
            "kew writes it to recognise its own issues on a re-run"
        )
    return problems


def validate_manifest(
    manifest: EpicPlanManifest, existing_ids: set[str] | None = None
) -> list[str]:
    """Return human-readable problems; empty list means the draft is creatable.

    ``existing_ids`` names the ids of nodes already in the epic (the amend
    path). A new ``local_id`` may not collide with one of them, and a
    ``depends_on`` that references one is a legitimate cross-existing-node edge
    (not a dangling reference).
    """
    existing = existing_ids or set()
    problems: list[str] = []
    seen: set[str] = set()
    for issue in manifest.issues:
        problems.extend(_issue_problems(issue, manifest, existing, seen))
        seen.add(issue.local_id)
    report = validate(draft_graph(manifest))
    problems.extend(f"self-dependency: {nid!r}" for nid in report.self_edges)
    problems.extend(
        f"unknown depends_on id: {nid!r}"
        for nid in report.dangling
        if nid not in existing
    )
    problems.extend(
        "dependency cycle: " + " -> ".join([*cyc, cyc[0]]) for cyc in report.cycles
    )
    return problems


def oversized_issues(manifest: EpicPlanManifest) -> list[DraftIssue]:
    """Draft issues that exceed the target-file limit (the re-split trigger).

    The governed run reacts to these specially — one agent retry with a
    re-split instruction — before the ``validate_manifest`` backstop rejects
    them outright; every other draft problem hard-fails straight away.
    """
    return [i for i in manifest.issues if len(i.target_files) > TARGET_FILES_LIMIT]


def build_resplit_instruction(oversized: list[DraftIssue]) -> str:
    """Instruction fed back to the planning agent to re-split oversized nodes.

    Names each offending node and its file count so the retry re-splits the
    exact nodes the gate rejected, then re-emits the full manifest.
    """
    listing = "\n".join(
        f"- {i.local_id!r}: {len(i.target_files)} target_files" for i in oversized
    )
    return (
        "## Re-split required\n\n"
        "Your previous draft carried oversized node(s) through the "
        f"dispatchability gate — every node must name at most {TARGET_FILES_LIMIT} "
        "target_files (see plan-to-issues SKILL.md). Re-split each oversized "
        f"node below into multiple <={TARGET_FILES_LIMIT}-file nodes, wiring "
        "their depends_on so the DAG stays acyclic, and re-emit the full "
        f"manifest:\n\n{listing}\n"
    )


@dataclass
class CreatedIssue:
    local_id: str
    number: int
    title: str
    url: str
    # The real issue numbers this node depends on, resolved at creation time.
    # Carried so the native-relation writer does not re-resolve local ids.
    depends_on: tuple[int, ...] = ()
    # An earlier run of the same manifest already made this issue: it was
    # found by its plan-node marker, not created again.
    reused: bool = False


class PartialCreationError(Exception):
    """Creation aborted mid-way; `created` lists what already exists on GitHub."""

    def __init__(
        self, created: list[CreatedIssue], failed_local_id: str, cause: str
    ) -> None:
        self.created = created
        self.failed_local_id = failed_local_id
        self.cause = cause
        super().__init__(f"issue creation failed at {failed_local_id!r}: {cause}")


# Priority/complexity colors match labels.setup_labels; type colors are new
# (kew setup does not create the type taxonomy, so epic plan must).
_LABEL_DEFS: dict[str, tuple[str, str]] = {
    "p0-critical": ("D93F0B", "Priority level 0"),
    "p1-high": ("E99695", "Priority level 1"),
    "p2-medium": ("FBCA04", "Priority level 2"),
    "p3-low": ("C2E0C6", "Priority level 3"),
    "simple": ("C2E0C6", "Complexity: simple"),
    "medium": ("FBCA04", "Complexity: medium"),
    "complex": ("E99695", "Complexity: complex"),
    "feature": ("1D76DB", "New capability"),
    "bug": ("D73A4A", "Something broken"),
    "refactor": ("FBCA04", "Restructuring without behavior change"),
    "test": ("0E8A16", "Test coverage improvement"),
    "docs": ("0075CA", "Documentation only"),
}


def _ensure_label(name: str, color: str, description: str, repo: str) -> None:
    """Create the label; tolerate an existing one (colors are not clobbered)."""
    result = run(
        [
            "gh",
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            color,
            "--description",
            description,
        ],
        check=False,
    )
    if result.returncode != 0 and "already exists" not in (result.stderr or ""):
        raise RuntimeError(
            f"gh label create failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def ensure_labels(manifest: EpicPlanManifest, *, epic_name: str, repo: str) -> None:
    """Ensure every label the creation step will apply exists on the repo."""
    _ensure_label(f"epic:{epic_name}", "5319E7", "kew epic membership", repo)
    used = sorted(
        {i.priority for i in manifest.issues}
        | {i.complexity for i in manifest.issues}
        | {i.type for i in manifest.issues}
    )
    for label in used:
        color, description = _LABEL_DEFS[label]
        _ensure_label(label, color, description, repo)


def _dep_number(dep: str, numbers: dict[str, int]) -> int:
    """Resolve one depends_on entry to a real issue number.

    Intra-manifest deps are minted during this run (found in ``numbers``);
    cross-existing-node deps (amend path) are already the live issue number.
    """
    if dep in numbers:
        return numbers[dep]
    return int(dep)


def _body_with_depends(body: str, dep_numbers: list[int]) -> str:
    if not dep_numbers:
        return body
    nums = " ".join(str(n) for n in sorted(dep_numbers))
    return body.rstrip() + f"\n\n## Dependencies\n\n<!-- kew:depends {nums} -->\n"


def draft_digest(issue: DraftIssue) -> str:
    """Fingerprint the draft's title and author-written body (before kew's markers).

    Written into the plan-node marker, so a later manifest that picks the same
    local id for different work is told apart from a re-run of the same one.
    """
    payload = json.dumps([issue.title, issue.body]).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _issue_body(issue: DraftIssue, epic_name: str, dep_numbers: list[int]) -> str:
    """The draft body plus kew's markers: dependencies, then plan-node identity."""
    body = _body_with_depends(issue.body, dep_numbers)
    marker = format_plan_node_marker(epic_name, issue.local_id, draft_digest(issue))
    return body.rstrip() + "\n\n" + marker + "\n"


def _create_one(
    issue: DraftIssue, body: str, *, epic_name: str, repo: str
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            issue.title,
            "--body",
            body,
            "--label",
            f"epic:{epic_name}",
            "--label",
            issue.priority,
            "--label",
            issue.complexity,
            "--label",
            issue.type,
        ],
        check=False,
    )


def _create_node(
    issue: DraftIssue,
    *,
    epic_name: str,
    repo: str,
    dep_numbers: list[int],
    created: list[CreatedIssue],
) -> CreatedIssue:
    """Create one issue and parse its number; raise PartialCreationError on failure."""
    body = _issue_body(issue, epic_name, dep_numbers)
    result = _create_one(issue, body, epic_name=epic_name, repo=repo)
    if result.returncode != 0:
        raise PartialCreationError(
            created, issue.local_id, result.stderr.strip() or result.stdout.strip()
        )
    lines = result.stdout.strip().splitlines()
    url = lines[-1].strip() if lines else ""
    number = _issue_number_from(result.stdout)
    if number is None:
        raise PartialCreationError(
            created,
            issue.local_id,
            f"cannot parse issue number from: {result.stdout!r}",
        )
    return CreatedIssue(
        local_id=issue.local_id,
        number=number,
        title=issue.title,
        url=url,
        depends_on=tuple(sorted(dep_numbers)),
    )


# The most `epic:<name>` issues one read returns. A listing that reaches it may
# have dropped an older plan node, so it is refused rather than trusted.
PLAN_NODE_LIST_LIMIT = 1000


@dataclass(frozen=True)
class PlanNodeIssue:
    """One issue carrying a plan-node marker, as its marker and state read."""

    number: int
    digest: str
    state: str
    state_reason: str
    author: str = ""

    @property
    def reusable(self) -> bool:
        """Open, or closed as done: a dependent may build on it."""
        return self.state == "OPEN" or (
            self.state == "CLOSED" and self.state_reason == "COMPLETED"
        )


@dataclass(frozen=True)
class PlanNodes:
    """The plan-node issues on an epic's label, split by who wrote them.

    ``own`` holds the issues ``login`` (the login ``gh`` creates with) wrote,
    the only ones kew may reuse; ``foreign`` holds every other marked issue.
    Each list is in ascending issue order.
    """

    login: str = ""
    own: dict[str, list[PlanNodeIssue]] = field(default_factory=dict)
    foreign: dict[str, list[PlanNodeIssue]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanReuse:
    """What an amend may reuse, what refuses it, and what it should warn about."""

    numbers: dict[str, int]
    problems: list[str]
    warnings: list[str]


def _epic_label_issues(epic_name: str, repo: str) -> list[dict[str, Any]]:
    """Every ``epic:<name>`` issue, open or closed; RuntimeError if unreadable."""
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            f"epic:{epic_name}",
            "--state",
            "all",
            "--limit",
            str(PLAN_NODE_LIST_LIMIT),
            "--json",
            "number,body,author,state,stateReason",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Cannot read the epic's issues to find the nodes an earlier run "
            f"made: {(result.stderr or result.stdout or '').strip()}"
        )
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse the epic's issues: {exc}") from exc
    if not isinstance(issues, list):
        raise RuntimeError("Cannot parse the epic's issues: not a JSON list")
    if len(issues) >= PLAN_NODE_LIST_LIMIT:
        raise RuntimeError(
            f"Label 'epic:{epic_name}' lists {PLAN_NODE_LIST_LIMIT} or more "
            "issues, the most kew reads in one listing, so it cannot tell "
            "which nodes an earlier plan made"
        )
    return issues


def _creator_login() -> str:
    """The login ``gh`` creates issues as; RuntimeError when it cannot be read."""
    result = run(["gh", "api", "user", "--jq", ".login"], check=False)
    login = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not login:
        detail = (result.stderr or "gh named no authenticated login").strip()
        raise RuntimeError(
            "Cannot read the gh login kew creates issues as, so the plan-node "
            f"issues it made cannot be told from copies: {detail}"
        )
    return login


def _author_login(issue: dict[str, Any]) -> str:
    author = issue.get("author")
    return str(author.get("login") or "") if isinstance(author, dict) else ""


def _plan_node_issue(
    identity: PlanNodeIdentity, issue: dict[str, Any]
) -> PlanNodeIssue:
    return PlanNodeIssue(
        number=int(issue["number"]),
        digest=identity.digest,
        state=str(issue.get("state") or ""),
        state_reason=str(issue.get("stateReason") or ""),
        author=_author_login(issue),
    )


def plan_node_issues(epic_name: str, *, repo: str) -> PlanNodes:
    """Map each local id to the issues an earlier plan run made for it.

    Read from the plan-node marker kew writes into every work issue, across
    every ``epic:<name>`` issue, open or closed. The label is read rather than
    the epic's members because a run that failed part-way never wrote the
    native membership. Only issues written by the login ``gh`` creates with
    are kew's own: anyone who can label an issue could copy a marker. Raises
    RuntimeError when the issues or the login cannot be read: reusing nothing
    would create every node a second time.
    """
    claims: list[tuple[str, PlanNodeIssue]] = []
    for issue in _epic_label_issues(epic_name, repo):
        identity = plan_node_identity(issue.get("body"))
        if identity is not None and identity.epic == epic_name:
            claims.append((identity.local_id, _plan_node_issue(identity, issue)))
    if not claims:
        return PlanNodes()
    nodes = PlanNodes(login=_creator_login())
    for local_id, claim in sorted(claims, key=lambda pair: pair[1].number):
        side = nodes.own if claim.author == nodes.login else nodes.foreign
        side.setdefault(local_id, []).append(claim)
    return nodes


def _numbers(claims: list[PlanNodeIssue]) -> str:
    return " and ".join(", ".join(f"#{c.number}" for c in claims).rsplit(", ", 1))


def _closed_as(claim: PlanNodeIssue) -> str:
    if claim.state_reason:
        return f"#{claim.number} (closed as {claim.state_reason})"
    return f"#{claim.number} (closed for an unknown reason)"


def _dead_oldest_problem(
    lid: str, claims: list[PlanNodeIssue], live: list[PlanNodeIssue]
) -> str:
    """The oldest issue kew made for *lid* is closed for a reason other than done."""
    oldest = claims[0]
    if not live:
        dead = ", ".join(_closed_as(c) for c in claims)
        return (
            f"local_id {lid!r} names {dead}; a re-run would count it as done "
            f"— reopen #{oldest.number} or pick a fresh id"
        )
    return (
        f"local_id {lid!r} is claimed by {_numbers(live)}, but the oldest issue "
        f"kew made for it is {_closed_as(oldest)} and dependents made with it "
        f"may name it — reopen #{oldest.number} and close {_numbers(live)} as "
        "not planned, or pick a fresh id"
    )


def _reuse_choice(
    issue: DraftIssue, claims: list[PlanNodeIssue], epic_name: str
) -> PlanNodeIssue | str:
    """The issue kew made that stands in for *issue*, or why none can.

    Only the oldest claim can stand in: dependents an earlier run made name
    the first issue it made for the node. A later copy closed for any reason
    other than completed is dead and ignored, so closing a duplicate as not
    planned clears it; a later copy still live must be closed first.
    """
    lid = issue.local_id
    oldest = claims[0]
    live = [c for c in claims if c.reusable]
    if not oldest.reusable:
        return _dead_oldest_problem(lid, claims, live)
    if len(live) > 1:
        return (
            f"local_id {lid!r} is claimed by {_numbers(live)}, each made by an "
            f"earlier plan of epic {epic_name}: keep #{oldest.number} (the "
            f"oldest) and close {_numbers(live[1:])} as not planned, then re-run"
        )
    if oldest.digest != draft_digest(issue):
        return (
            f"local_id {lid!r} already names #{oldest.number} from an earlier "
            f"plan of epic {epic_name}, with a different title or body — "
            "pick a fresh id"
        )
    return oldest


def _foreign_copy_warnings(issue: DraftIssue, nodes: PlanNodes) -> list[str]:
    """Name issues another login wrote from this very draft: a re-auth, or a copy.

    They are never reused (a marker can be copied), but a gh login that
    changed since the earlier run would otherwise re-create the node silently.
    """
    digest = draft_digest(issue)
    return [
        f"#{c.number} carries the plan-node marker for {issue.local_id!r} from "
        f"this manifest but was written by {c.author or 'an unknown login'!r}, "
        f"not {nodes.login!r} (the gh login now): it is not reused and the "
        "node is created again. If the gh login changed since the earlier "
        "run, close the duplicate."
        for c in nodes.foreign.get(issue.local_id, [])
        if c.digest == digest
    ]


def plan_reuse(
    manifest: EpicPlanManifest, nodes: PlanNodes, *, epic_name: str
) -> PlanReuse:
    """The manifest's nodes an earlier run already made, and why any cannot be reused.

    A local id reuses its issue only when the oldest issue kew made for it is
    the one live claim and its digest matches the draft. Any other claim is a problem:
    the caller must refuse before creating.
    """
    plan = PlanReuse(numbers={}, problems=[], warnings=[])
    for issue in manifest.issues:
        claims = nodes.own.get(issue.local_id)
        if not claims:
            plan.warnings.extend(_foreign_copy_warnings(issue, nodes))
            continue
        choice = _reuse_choice(issue, claims, epic_name)
        if isinstance(choice, str):
            plan.problems.append(choice)
        else:
            plan.numbers[issue.local_id] = choice.number
    return plan


def create_issues(
    manifest: EpicPlanManifest,
    *,
    epic_name: str,
    repo: str,
    existing_ids: set[str] | None = None,
    reuse: dict[str, int] | None = None,
) -> list[CreatedIssue]:
    """Create issues level-by-level in topological order.

    Dependencies are created before dependents, so the depends block always
    carries real issue numbers — no post-creation edits. On the amend path,
    ``existing_ids`` names live epic nodes a new issue may depend on; such a
    ``depends_on`` entry is already the real issue number, used as-is.

    ``reuse`` maps a local id to the issue an earlier run of this manifest
    already made for it (read from its plan-node marker). That node is not
    created again: it is returned with ``reused=True`` and its number serves
    its dependents, so re-running after a partial failure converges.
    """
    existing = existing_ids or set()
    if validate_manifest(manifest, existing):
        msg = "manifest failed validation; run validate_manifest first"
        raise ValueError(msg)
    made = reuse or {}
    by_id = {i.local_id: i for i in manifest.issues}
    levels = validate(draft_graph(manifest)).levels
    created: list[CreatedIssue] = []
    numbers: dict[str, int] = {}
    for level in levels:
        for lid in level:
            issue = by_id[lid]
            dep_numbers = [_dep_number(d, numbers) for d in issue.depends_on]
            if lid in made:
                node = CreatedIssue(
                    local_id=lid,
                    number=made[lid],
                    title=issue.title,
                    url="",
                    depends_on=tuple(sorted(dep_numbers)),
                    reused=True,
                )
            else:
                node = _create_node(
                    issue,
                    epic_name=epic_name,
                    repo=repo,
                    dep_numbers=dep_numbers,
                    created=created,
                )
            numbers[lid] = node.number
            created.append(node)
    return created


def build_epic_plan_prompt(
    *, epic_name: str, plan_path: str, config: DispatchConfig
) -> str:
    """Render the planning-agent prompt.

    No user template override — the manifest contract is kew's, not the
    project's.
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,  # nosec B701 — renders a CLI prompt, not HTML
        keep_trailing_newline=True,
    )
    template = env.get_template("epic-plan.md.j2")
    return template.render(
        epic_name=epic_name,
        plan_path=plan_path,
        manifest_relpath=MANIFEST_RELPATH,
        project_name=config.project.name,
        criteria_min=CRITERIA_MIN,
        criteria_max=CRITERIA_MAX,
    )


# --- GitHub-native linkage ---------------------------------------------------
#
# The label and the `<!-- kew:depends -->` marker above are kept for one
# release; these writes add GitHub's own keys for the same two facts, so an
# epic kew planned reads either way.

# GitHub's sub-issue and issue-dependency REST endpoints, both GA under this
# API version. Pinned so a default-version bump cannot silently change shape.
_API_VERSION = "2026-03-10"
_PROVIDER_ERROR_LIMIT = 200


def _api(
    path: str,
    *,
    repo: str,
    method: str | None = None,
    fields: dict[str, int] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", "api"]
    if method is not None:
        cmd += ["--method", method]
    cmd += ["-H", f"X-GitHub-Api-Version: {_API_VERSION}", f"repos/{repo}/{path}"]
    for key, value in (fields or {}).items():
        cmd += ["-F", f"{key}={value}"]
    return run(cmd, check=False)


def _issue_number_from(stdout: str) -> int | None:
    """The issue number in the URL `gh issue create` prints, or None.

    Every caller of a None turns it into its own message naming the raw
    output — there is no path where an unparsed URL reads as a success.
    """
    lines = (stdout or "").strip().splitlines()
    if not lines:
        return None
    tail = lines[-1].strip().rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdecimal() else None


def _issue_database_id(number: int, *, repo: str) -> tuple[int | None, str]:
    """One issue's database id, or None and why it could not be read."""
    result = _api(f"issues/{number}", repo=repo)
    if result.returncode != 0:
        return None, (result.stderr or "").strip() or f"gh api rc={result.returncode}"
    try:
        payload: Any = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        return None, f"unreadable response ({exc})"
    issue_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(issue_id, int):
        return None, "no id in response"
    return issue_id, ""


def _database_ids(numbers: set[int], *, repo: str) -> tuple[dict[int, int], list[str]]:
    """Issue number -> database id, the key both REST endpoints take."""
    ids: dict[int, int] = {}
    warnings: list[str] = []
    for number in sorted(numbers):
        issue_id, reason = _issue_database_id(number, repo=repo)
        if issue_id is None:
            warnings.append(
                f"#{number}: could not read its issue id ({reason}); "
                "left out of the native linkage"
            )
        else:
            ids[number] = issue_id
    return ids, warnings


def _safe_provider_error(stderr: str, *, returncode: int) -> str:
    """Return one bounded, secret-free provider error line."""
    lines = (stderr or "").splitlines()
    first_line = lines[0].strip() if lines else ""
    if not first_line:
        first_line = f"gh issue create exited {returncode} with empty stderr"
    return sanitize_publication_error(first_line)[:_PROVIDER_ERROR_LIMIT]


def _create_epic_parent(*, epic_name: str, repo: str, plan_path: str) -> int | None:
    """Create the epic's container issue; typed `Epic` where the org defines it.

    The typed create is attempted first and retried untyped, because issue
    types are an org-level feature a repository may not have.

    The durable body marker identifies the container even if a human adds the
    `epic:<name>` label. The convention adapter always excludes marked
    containers. The label remains absent by default.
    """
    body = (
        f"Epic container for `{epic_name}`.\n\n"
        "kew reads this epic's membership from this issue's sub-issues. This "
        f"issue is not a node: the `epic:{epic_name}` label that names the "
        "nodes is deliberately absent here.\n\n"
        "The marker below is reserved kew metadata. Do not copy it into work "
        "issues.\n"
        f"\n{format_epic_container_marker(epic_name)}\n"
    )
    if plan_path:
        body += f"\nPlan: `{plan_path}`\n"
    base = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        f"epic: {epic_name}",
        "--body",
        body,
    ]
    typed = run(base + ["--type", "Epic"], check=False)
    if typed.returncode == 0:
        return _issue_number_from(typed.stdout)
    untyped = run(base, check=False)
    if untyped.returncode == 0:
        reason = _safe_provider_error(typed.stderr or "", returncode=typed.returncode)
        _warn(
            f"Epic container was created untyped because --type Epic failed: {reason}"
        )
        return _issue_number_from(untyped.stdout)
    typed_error = _safe_provider_error(typed.stderr or "", returncode=typed.returncode)
    untyped_error = _safe_provider_error(
        untyped.stderr or "", returncode=untyped.returncode
    )
    _warn(
        "Epic container creation failed; "
        f"typed create error: {typed_error}; "
        f"untyped create error: {untyped_error}"
    )
    return None


def _add_sub_issue(
    number: int, *, parent: int, ids: dict[int, int], repo: str
) -> list[str]:
    child = ids.get(number)
    if child is None:
        return []  # its id read already produced a warning
    result = _api(
        f"issues/{parent}/sub_issues",
        repo=repo,
        method="POST",
        fields={"sub_issue_id": child},
    )
    if result.returncode == 0:
        return []
    return [
        f"#{number}: could not be added as a sub-issue of #{parent} "
        f"({(result.stderr or '').strip()})"
    ]


def _write_blocked_by(
    issue: CreatedIssue, *, ids: dict[int, int], repo: str
) -> list[str]:
    warnings: list[str] = []
    for dep in issue.depends_on:
        blocker = ids.get(dep)
        if blocker is None:
            continue
        result = _api(
            f"issues/{issue.number}/dependencies/blocked_by",
            repo=repo,
            method="POST",
            fields={"issue_id": blocker},
        )
        if result.returncode != 0:
            warnings.append(
                f"#{issue.number}: could not record blocked_by #{dep} "
                f"({(result.stderr or '').strip()}); the kew:depends marker "
                "still carries this edge"
            )
    return warnings


def _write_relations(
    created: list[CreatedIssue],
    *,
    parent: int,
    ids: dict[int, int],
    repo: str,
    adopt: tuple[int, ...],
) -> list[str]:
    warnings: list[str] = []
    for number in adopt:
        warnings += _add_sub_issue(number, parent=parent, ids=ids, repo=repo)
    for issue in created:
        warnings += _add_sub_issue(issue.number, parent=parent, ids=ids, repo=repo)
        warnings += _write_blocked_by(issue, ids=ids, repo=repo)
    return warnings


def link_native_relations(
    created: list[CreatedIssue],
    *,
    epic_name: str,
    repo: str,
    plan_path: str = "",
    existing_ids: set[str] | None = None,
) -> list[str]:
    """Write GitHub's own linkage for the nodes `create_issues` just created.

    Membership becomes sub-issues of the epic's container issue (created on
    the first plan, reused by `--amend`); each `depends_on` becomes a
    `blocked_by` relation. `create_issues` still writes the `epic:<name>`
    label and the `<!-- kew:depends -->` marker for one release, so an API
    without the relations keeps working on the old keys.

    ``existing_ids`` names the epic's nodes that predate this run (the amend
    path). They matter only when this run has to create the container: an epic
    that lived on the label alone must bring *every* node across, or the
    native read would see an epic containing just the amendment.

    Returns warning lines and never raises because the issues already exist.
    The caller's mandatory read-back gate rejects any missing expected node.
    An unavailable `gh` is reported here and evaluated by that gate.
    """
    if not created:
        return []
    try:
        return _link_native_relations(
            created,
            epic_name=epic_name,
            repo=repo,
            plan_path=plan_path,
            existing_ids=existing_ids or set(),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return [f"GitHub-native linkage skipped — {exc}"]


def _link_native_relations(
    created: list[CreatedIssue],
    *,
    epic_name: str,
    repo: str,
    plan_path: str,
    existing_ids: set[str],
) -> list[str]:
    probe = probe_native_linkage(repo, created[0].number)
    if not probe.available:
        return [
            f"GitHub-native linkage skipped — {probe.reason}. Membership stays "
            f"on the epic:{epic_name} label and edges on the kew:depends marker."
        ]
    parent = GitHubNativeSource(repo=repo).epic_parent(epic_name)
    adopt: tuple[int, ...] = ()
    if parent is None:
        parent = _create_epic_parent(
            epic_name=epic_name, repo=repo, plan_path=plan_path
        )
        # The epic lived on the label alone until now: its existing nodes join
        # the new container too, ahead of the ones this run created.
        adopt = tuple(sorted(int(i) for i in existing_ids if i.isdigit()))
    if parent is None:
        return [
            f"GitHub-native linkage skipped — could not create the container "
            f"issue for epic {epic_name}. Membership stays on the "
            f"epic:{epic_name} label."
        ]
    wanted = (
        {i.number for i in created}
        | {dep for i in created for dep in i.depends_on}
        | set(adopt)
    )
    ids, warnings = _database_ids(wanted, repo=repo)
    return warnings + _write_relations(
        created, parent=parent, ids=ids, repo=repo, adopt=adopt
    )
