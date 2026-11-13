"""LinearTracker: a thin write vertical over :class:`LinearClient`.

Group L1 of the Linear drive slice — the mutating half of Phase C, kept
deliberately small: ``list_ready`` / ``claim`` / ``comment`` / ``close``. The
full ``Tracker`` port (WorkItem/WorkState/ClaimResult, flag vocabulary, epic
isolation) is Phase B and lands separately; this vertical is only what driving
one Linear-tracked DAG end-to-end needs today.

Three invariants carry over from the client and the spec:

* **Gated on tracker type.** A ``LinearTracker`` only exists for a config whose
  ``tracker.type == "linear"`` — construction rejects anything else, so no
  Linear mutation can fire against a GitHub-configured project.
* **Env-only key.** The key lives on the injected client (from
  ``LINEAR_API_KEY``); this module never sees, stores, or logs it.
* **Redaction on every error path.** All API traffic goes through
  ``client.execute``, which redacts the key from any raised message; the
  semantic errors this module raises are built from item ids / state names /
  the team key only — never response bodies — so they cannot carry the key.

Workflow-state names are resolved from the team's states once per process (the
``in_review_state`` disambiguation pattern, config.py:39-47) and cached on the
instance alongside the viewer id.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Phase C.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kew.ids import LINEAR_ID_RE
from kew.linear.client import LinearApiError, LinearClient
from kew.models import GhIssue
from kew.no_change import ItemObservation, item_observation

if TYPE_CHECKING:
    from kew.config import DispatchConfig


def claim_snapshot_matches(
    snapshot: Any, item_id: str, state_id: str, assignee_id: str
) -> bool:
    """Compare returned claim evidence with the named issue and mutation inputs."""
    if not isinstance(snapshot, dict) or not state_id or not assignee_id:
        return False
    identity_field = "identifier" if LINEAR_ID_RE.fullmatch(item_id) else "id"
    state = snapshot.get("state")
    assignee = snapshot.get("assignee")
    return (
        snapshot.get(identity_field) == item_id
        and isinstance(state, dict)
        and state.get("id") == state_id
        and isinstance(assignee, dict)
        and assignee.get("id") == assignee_id
    )


# The started state a claim moves an issue INTO — distinct from the review
# state kew writes on PR creation. When a team runs multiple `started` states,
# this name disambiguates (the in_review_state pattern, inverted).
_IN_PROGRESS_DEFAULT = "In Progress"

# The Linear state types an item is still OPEN in. ``completed`` and
# ``canceled`` are both closed for this purpose: a cancelled item is not one
# kew may claim it closed as a no-change.
_OPEN_STATE_TYPES = frozenset({"backlog", "unstarted", "started", "triage"})

_TEAM_STATES_QUERY = """
query TeamStates($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes {
      id
      states { nodes { id name type } }
    }
  }
}
"""

_VIEWER_QUERY = "query Viewer { viewer { id } }"

# includeArchived per API-F3 (Linear auto-archives completed issues); harmless
# for unstarted issues, uniform with the graph adapter's every-query rule.
_READY_ISSUES_QUERY = """
query ReadyIssues($key: String!, $stateId: ID!) {
  issues(
    filter: { team: { key: { eq: $key } }, state: { id: { eq: $stateId } } }
    includeArchived: true
  ) {
    nodes { id identifier title url }
  }
}
"""

# Dispatch fetch (L3): `issue(id:)` accepts BOTH the UUID and the human
# identifier ("SFD-133"), so title/description/url/labels all resolve from a
# bare identifier lookup — unlike the relation *projections* (linear_native.py),
# which are UUID-only. That quirk does not bite here: no relations are read.
_ISSUE_FETCH_QUERY = """
query IssueForDispatch($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    url
    labels { nodes { name } }
  }
}
"""

_ISSUE_CLAIM_STATE_QUERY = """
query IssueClaimState($id: String!) {
  issue(id: $id) {
    id identifier updatedAt state { id name type } assignee { id }
  }
}
"""

_ISSUE_UPDATE_MUTATION = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier updatedAt state { id name type } assignee { id } }
  }
}
"""

# The no-change outcome look (I11): is the item still open, and does it already
# carry this judgment's synchronization marker? One query, because the two
# answers have to describe the same moment — an item read as open in one call
# and unmarked in another is two moments, and a write between them is a
# duplicate.
_ISSUE_OUTCOME_QUERY = """
query IssueOutcome($id: String!) {
  issue(id: $id) {
    identifier
    url
    state { type }
    comments { nodes { body } }
  }
}
"""

_COMMENT_CREATE_MUTATION = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}
"""


def _outcome_observation(issue: dict[str, Any], marker: str) -> ItemObservation:
    """One ``IssueOutcome`` node as an observation: open/closed, marked or not."""
    state_type = str((issue.get("state") or {}).get("type") or "")
    return item_observation(
        closed=state_type not in _OPEN_STATE_TYPES,
        bodies=[str(node.get("body") or "") for node in _comment_nodes(issue)],
        marker=marker,
        ref=str(issue.get("url") or ""),
        detail=f"Linear reports a {state_type or 'nameless'} state",
    )


def _comment_nodes(issue: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = (issue.get("comments") or {}).get("nodes") or []
    return list(nodes)


class LinearTracker:
    """Mutating operations on a single Linear team's issues."""

    def __init__(self, config: DispatchConfig, client: LinearClient) -> None:
        if config.tracker.type != "linear":
            raise ValueError(
                "LinearTracker requires tracker.type == 'linear'; this project "
                f"is configured with '{config.tracker.type}'."
            )
        self._team = config.tracker.linear.team
        self._in_review_state = config.tracker.linear.in_review_state
        self._client = client
        # Resolved once per process (per instance) and reused across calls.
        self._states: list[dict[str, Any]] | None = None
        self._viewer_id: str | None = None
        self.mutation_outcome_ambiguous = False
        self._claim_fields: dict[str, Any] = {}

    # -- read ---------------------------------------------------------------

    def list_ready(self) -> list[dict[str, Any]]:
        """Issues in the team's ready (``unstarted``-type) workflow state."""
        state_id = self._resolve_state_id("unstarted")
        data = self._client.execute(
            _READY_ISSUES_QUERY, {"key": self._team, "stateId": state_id}
        )
        return list(data["issues"]["nodes"])

    def fetch_issue(self, item_id: str) -> GhIssue:
        """Fetch ``item_id`` shaped like ``select_next_issue``'s dispatch dict.

        The agent-facing fields mirror the GitHub selector's JSON so
        ``execute_dispatch`` consumes both without a branch: ``title``, ``body``
        (the Linear description — the seeded spec), ``url``, and ``labels`` (as
        ``[{"name": ...}]`` so complexity routing reads them uniformly; an empty
        list is acceptable — routing falls back to the default). ``number`` is
        the ``0`` sentinel: a non-numeric tracker id is never a GitHub issue
        number, so the durable id travels beside this dict as ``item_id`` (L3),
        not inside ``number``.
        """
        issue = self._client.execute(_ISSUE_FETCH_QUERY, {"id": item_id})["issue"]
        if issue is None:
            raise LinearApiError(f"Linear issue {item_id} not found.")
        label_nodes = (issue.get("labels") or {}).get("nodes") or []
        return {
            "number": 0,
            "title": issue.get("title") or "",
            "body": issue.get("description") or "",
            "url": issue.get("url"),
            "labels": [
                {"name": node["name"]} for node in label_nodes if node.get("name")
            ],
        }

    # -- mutations (gated on tracker.type == "linear" at construction) -------

    def claim_state(self) -> dict[str, str]:
        """Resolve the mutation target before recording durable claim intent."""
        state_id = self._resolve_state_id(
            "started", prefer=_IN_PROGRESS_DEFAULT, exclude=self._in_review_state
        )
        state = next(s for s in self._workflow_states() if s["id"] == state_id)
        return {"id": state_id, "name": str(state["name"])}

    def claim_assignee(self) -> str:
        """Return the authenticated viewer used by the claim mutation."""
        return self._viewer()

    def claim(self, item_id: str) -> dict[str, Any]:
        """Assign the viewer and transition ``item_id`` to the started state.

        The target is the ``started``-type state that is *not* the review state
        (defaulting to ``In Progress``) — kew reserves ``in_review_state`` for
        PR-open, so a claim must not land there.
        """
        self.mutation_outcome_ambiguous = False
        self._claim_fields = {}
        self._claim_fields = {
            "stateId": self.claim_state()["id"],
            "assigneeId": self.claim_assignee(),
        }
        try:
            return self._issue_update(item_id, self._claim_fields)
        finally:
            self.mutation_outcome_ambiguous = (
                getattr(self._client, "mutation_outcome_ambiguous", False) is True
            )

    def verify_claim(self, item_id: str) -> dict[str, Any] | None:
        """Read the claim result without changing remote ownership."""
        issue = self._client.execute(_ISSUE_CLAIM_STATE_QUERY, {"id": item_id})["issue"]
        if not issue or not self._claim_fields:
            return None
        if not claim_snapshot_matches(
            issue,
            item_id,
            self._claim_fields["stateId"],
            self._claim_fields["assigneeId"],
        ):
            return None
        return dict(issue)

    def reset_to_ready(self, item_id: str) -> dict[str, Any]:
        """Unclaim ``item_id`` — the inverse of :meth:`claim`.

        Unassigns the viewer and moves the item to the ready state.
        Only an explicit operator action may request this unconditional update.
        Automatic refusal and recovery paths retain claims.
        """
        state_id = self._resolve_state_id("unstarted")
        return self._issue_update(item_id, {"stateId": state_id, "assigneeId": None})

    def comment(self, item_id: str, body: str) -> None:
        """Post ``body`` as a comment on ``item_id``."""
        data = self._client.execute(
            _COMMENT_CREATE_MUTATION, {"input": {"issueId": item_id, "body": body}}
        )
        if not data["commentCreate"].get("success"):
            raise LinearApiError(f"Linear comment on {item_id} did not succeed.")

    def close(self, item_id: str) -> None:
        """Transition ``item_id`` to the team's done (``completed``) state."""
        state_id = self._resolve_state_id("completed")
        self._issue_update(item_id, {"stateId": state_id})

    # -- no-change outcome adapter (I11) ------------------------------------
    #
    # The Linear dialect of the same two moves ``kew.labels`` gives GitHub:
    # look once, then write once. Every decision between them belongs to the
    # reconciler, so this pair reports what it saw and whether the write was
    # confirmed — never what should happen next.

    def observe_no_change_item(self, item_id: str, *, marker: str) -> ItemObservation:
        """One look at a Linear issue: open or closed, and marked or not.

        An API error comes back ``available=False`` rather than raising: an
        outage must leave the outcome sync-pending, and a tracker kew could
        not read is never a tracker kew may write to blind.
        """
        try:
            issue = self._client.execute(_ISSUE_OUTCOME_QUERY, {"id": item_id})["issue"]
        except LinearApiError as exc:
            return ItemObservation(available=False, detail=str(exc))
        if issue is None:
            return ItemObservation(
                available=False, detail=f"Linear issue {item_id} not found."
            )
        return _outcome_observation(issue, marker)

    def apply_no_change_outcome(
        self, item_id: str, *, body: str | None, close: bool
    ) -> bool:
        """Comment (and optionally close). True ONLY if Linear confirmed both.

        ``commentCreate``/``issueUpdate`` answer ``success: false`` for a
        mutation the API accepted but did not perform. That is a warning, not
        an acknowledgement, and it returns False here so the sync job stays
        unacknowledged. ``body=None`` finishes a closure whose comment already
        landed, so a resumed pass never posts the note twice.
        """
        try:
            if body is not None:
                self.comment(item_id, body)
            if close:
                self.close(item_id)
        except LinearApiError:
            return False
        return True

    # -- internals ----------------------------------------------------------

    def _workflow_states(self) -> list[dict[str, Any]]:
        if self._states is None:
            nodes = self._client.execute(_TEAM_STATES_QUERY, {"key": self._team})[
                "teams"
            ]["nodes"]
            if not nodes:
                raise LinearApiError(
                    f"Linear team {self._team!r} not found (check "
                    "tracker.linear.team and the key's team access)."
                )
            self._states = list(nodes[0]["states"]["nodes"])
        return self._states

    def _resolve_state_id(
        self, state_type: str, *, prefer: str | None = None, exclude: str | None = None
    ) -> str:
        """Pick one state id of ``state_type``, disambiguating by name.

        ``exclude`` drops a state by name (e.g. the review state when claiming);
        ``prefer`` picks a named match among the survivors before falling back
        to the first — the in_review_state disambiguation pattern, generalized.
        """
        matches = [s for s in self._workflow_states() if s.get("type") == state_type]
        # ``exclude=None`` never matches a name, so survivors == matches then.
        survivors = [s for s in matches if s.get("name") != exclude]
        candidates = survivors or matches
        if not candidates:
            raise LinearApiError(
                f"Linear team {self._team!r} has no {state_type!r} workflow state."
            )
        # ``prefer=None`` matches nothing, so this falls through to the first.
        chosen = next((s for s in candidates if s.get("name") == prefer), candidates[0])
        return str(chosen["id"])

    def _viewer(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = str(self._client.execute(_VIEWER_QUERY)["viewer"]["id"])
        return self._viewer_id

    def _issue_update(
        self, item_id: str, input_fields: dict[str, Any]
    ) -> dict[str, Any]:
        data = self._client.execute(
            _ISSUE_UPDATE_MUTATION, {"id": item_id, "input": input_fields}
        )
        result = data.get("issueUpdate") if isinstance(data, dict) else None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinearApiError(
                f"Linear issueUpdate for {item_id} did not succeed.",
                ambiguous=self._client.mutation_outcome_ambiguous is True,
            )
        return result
