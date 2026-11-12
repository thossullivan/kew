"""The approval and prompt-profile pages say what the CLI and console ship.

Each sentence the code prints is imported, never retyped, so a page that
drifts from the code goes red here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kew.service.prompt_profiles import (
    NO_CONFIG,
    UNCONFIGURED_APPROVERS,
    block_opener_refusal,
)

REPO_ROOT = Path(__file__).parent.parent.parent

#: The console's old non-approver sentence; the write area now says once that
#: only an approver can change a profile, or prints the server's fix.
STALE_NON_APPROVER = "Only an approver can approve a profile."


def _flat(rel: str) -> str:
    """*rel*'s text with every run of whitespace as one space."""
    return " ".join((REPO_ROOT / rel).read_text(encoding="utf-8").split())


#: Words that tie a sentence to an empty or unset approver list.
_EMPTY_LIST = re.compile(
    r"\bempty\b|\bunset\b|`\[\]`|\bno approvers\b|\bby default\b", re.I
)
#: Words that limit approval to people with repository write access.
_WRITE_ACCESS = re.compile(
    r"write[- ]?(access|permission)|push access|repo writers|any collaborator",
    re.I,
)
#: The true host-side rule: `kew approve`/`kew deny` admit any actor.
_HOST_SIDE = re.compile(r"any actor|on the kew host", re.I)
#: Other approver lists whose empty-list rule really is write permission: the
#: no-change and merge gates (`[human_review]`, `[epic]`) and the publication
#: hold. A sentence naming one of them is not about `[approval].approvers`.
_OTHER_GATE = re.compile(
    r"merge gate|no-change|human_review|\[epic\]|publication hold|unhold", re.I
)


def _write_access_claims_for_an_empty_list(text: str) -> list[str]:
    """Sentences of *text* that tie an empty approver list to write access.

    `kew approve`/`kew deny` admit any actor while `[approval].approvers` is
    empty; only the `/approve` comment workflow checks write access. A
    sentence that pairs the empty list with collaborators or write access
    is true only when it also states that host-side rule, or when it is
    about another gate's approver list.
    """
    plain = " ".join(text.replace("*", "").split())
    return [
        sentence
        for sentence in re.split(r"(?<=[.:;])\s+", plain)
        if _EMPTY_LIST.search(sentence)
        and _WRITE_ACCESS.search(sentence)
        and not _HOST_SIDE.search(sentence)
        and not _OTHER_GATE.search(sentence)
    ]


def _approvers_docstring() -> str:
    """The attribute docstring under `ApprovalConfig.approvers` in config.py."""
    tree = ast.parse((REPO_ROOT / "kew/src/kew/config.py").read_text("utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApprovalConfig"
    )
    for field, doc in zip(cls.body, cls.body[1:], strict=False):
        if (
            isinstance(field, ast.AnnAssign)
            and isinstance(field.target, ast.Name)
            and field.target.id == "approvers"
            and isinstance(doc, ast.Expr)
            and isinstance(doc.value, ast.Constant)
            and isinstance(doc.value.value, str)
        ):
            return doc.value.value
    raise AssertionError("ApprovalConfig.approvers has no docstring")


def test_an_empty_list_claim_is_caught_hyphenated_or_not() -> None:
    for claim in (
        "Empty = any write-access collaborator decides a hold.",
        "Empty = any collaborator with write access decides a hold.",
        "Empty = any write-access collaborator decides a hold, see `/approve` below.",
        "While the list is empty, anyone with push access can approve.",
        "Empty means only users with write permission may approve.",
        "With no approvers configured, write-access collaborators decide holds.",
        "The default `[]` admits any collaborator with write access.",
        "If approvers is unset, approval falls to repo writers.",
    ):
        assert _write_access_claims_for_an_empty_list(claim) == [claim]


def test_an_empty_list_claim_that_states_the_host_rule_is_not_flagged() -> None:
    for claim in (
        "Empty = `kew approve` admits any actor; `/approve` checks write access.",
        "Empty admits all on the kew host; `/approve` checks write access too.",
    ):
        assert _write_access_claims_for_an_empty_list(claim) == []


def test_an_empty_list_claim_about_another_gate_is_not_flagged() -> None:
    other = "An empty approvers list requires fresh write access on the merge gate."
    assert _write_access_claims_for_an_empty_list(other) == []


def test_the_replaced_stale_sentences_are_flagged() -> None:
    """The three sentences this fix replaced in the docs stay flagged."""
    for stale in (
        "# approve/deny. Empty = any write-access collaborator # decides a hold,"
        " but every prompt profile decision # is refused until the list names"
        " someone.",
        "Empty means any collaborator with repo write access may approve (the"
        " boundary the remote-approval workflow checks).",
        "Releasing a cost hold keeps its own rule, where an empty list lets any"
        " collaborator approve.",
    ):
        assert _write_access_claims_for_an_empty_list(stale), stale


def test_approve_page_says_setup_does_not_install_the_approval_workflow() -> None:
    page = _flat("docs/cli/approve.md")
    assert "neither `kew setup` nor `create-kew` installs it" in page
    remote = page.split("## Remote approval", 1)[1].split("## Delivery", 1)[0]
    assert "`kew approve`" in remote
    delivery = page.split("## Delivery", 1)[1]
    assert "`kew approve <issue>`" in delivery
    assert "`.github/workflows/approval.yml`" in delivery


def test_prompts_pages_name_the_empty_approvers_refusal() -> None:
    for rel in ("docs/prompts.md", "docs/cli/prompts.md", "docs/web/index.md"):
        assert UNCONFIGURED_APPROVERS in _flat(rel), rel
    cli = _flat("docs/cli/prompts.md")
    assert "`list` and `propose` exit 2" in cli
    exit_row = next(
        line for line in cli.split("| `2` |")[1:] if line.startswith(" Nothing")
    )
    assert "the ledger is lost" in exit_row.split(" |", 1)[0]
    concepts = _flat("docs/prompts.md")
    assert "answers 409" in concepts
    assert "asks you to confirm" in concepts


def test_console_page_names_the_server_refusals_and_no_stale_sentence() -> None:
    page = _flat("docs/web/index.md")
    assert STALE_NON_APPROVER not in page
    assert "Only an approver can change a profile." in page
    assert NO_CONFIG in page
    assert block_opener_refusal("```python") is not None
    assert "opens a code fence or an HTML block" in page
    assert "`kew serve` reads `[approval].approvers` once, at start" in page


def test_config_and_prompts_pages_say_an_empty_list_admits_any_host_actor() -> None:
    for rel in ("docs/configuration.md", "docs/prompts.md"):
        page = _flat(rel)
        assert _write_access_claims_for_an_empty_list(page) == [], rel
        assert "an empty list admits any actor" in page, rel
        assert "collaborator with repo write access" in page, rel
    assert "repo write access, and the list narrows that further." in _flat(
        "docs/configuration.md"
    )
    assert "narrows those comment approvals too" in _flat("docs/prompts.md")
    assert "(cli/approve.md)" in _flat("docs/prompts.md")


@pytest.mark.policy
def test_no_page_says_an_empty_list_limits_host_approval_to_write_access() -> None:
    sources = {
        str(page.relative_to(REPO_ROOT)): page.read_text("utf-8")
        for page in [
            *sorted((REPO_ROOT / "docs").rglob("*.md")),
            REPO_ROOT / "README.md",
        ]
    }
    sources["ApprovalConfig.approvers"] = _approvers_docstring()
    claims = {
        name: found
        for name, text in sources.items()
        if (found := _write_access_claims_for_an_empty_list(text))
    }
    assert claims == {}
