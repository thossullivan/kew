"""Operator attestation binds a head kew did not push itself (#2124).

The reported deadlock: an owner approves protected output, the operator
publishes it by hand, and from then on every governed remediation leg on that
branch refuses the head with ``publication ancestry is ambiguous``. Nothing in
the canonical flow can record the one missing fact, so the refusal is
permanent. These tests pin the fact, its verification, and the binding it
produces.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew.audit import Actor
from kew.audit_chain import verify_chain
from kew.publication_attest import (
    _MAX_ATTEST_CHAIN,
    HeadAttestationRefused,
    attest_published_head,
    record_head_attestation,
)
from kew.publication_credentials import (
    CredentialPublicationRefused,
    publication_producer_for_head,
)
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore

BRANCH = "agent/issue-7"
HEAD = "a" * 40
PARENT = "b" * 40
BASE_BRANCH = "main"
ON_BASE = "c" * 40
OFF_BASE = "d" * 40


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _run(
    store: SqliteRunStore,
    run_id: str,
    *,
    status: str = "success",
    branch: str = BRANCH,
    container_name: str | None = None,
    hour: int = 1,
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id="7",
            branch=branch,
            status=status,  # type: ignore[arg-type]
            exit_code=0,
            started_at=datetime(2026, 9, 25, hour, tzinfo=UTC),
            finished_at=datetime(2026, 9, 25, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=container_name,
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )


def _contained_profile(store: SqliteRunStore, run_id: str) -> None:
    """The posture the issue reports: a contained run holding a reusable key."""
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="kew-agent:latest",
                image_id="sha256:" + "a" * 64,
                auth_path="env:SYNTHETIC_PROVIDER_KEY",
            ),
        )
    )


def _producer_on_branch(store: SqliteRunStore) -> None:
    _run(store, "producer", container_name="kew-7")
    _contained_profile(store, "producer")


def _readers(head: str = HEAD, parents: tuple[str, ...] = (PARENT,)) -> dict[str, Any]:
    """The injected operator-origin reads, standing in for real git.

    ``ON_BASE`` is the one commit the fetched base branch reaches; every other
    sha stands for a commit that is not on it.
    """
    return {
        "base_branch": BASE_BRANCH,
        "remote_head": lambda branch: head,
        "commit_parents": lambda sha: parents,
        "commit_on_base": lambda sha, base: base == BASE_BRANCH and sha == ON_BASE,
    }


def _attested(store: SqliteRunStore, commit_sha: str) -> list[Any]:
    """The durable head claims naming exactly *commit_sha*."""
    return [
        event
        for event in store.list_audit_events(event="publication_head_attested")
        if isinstance(event.payload, dict)
        and event.payload.get("commit_sha") == commit_sha
    ]


# --------------------------------------------------------------------------- #
# The refusal this issue reports, and the binding that clears it.
# --------------------------------------------------------------------------- #


def test_operator_published_head_is_refused_before_it_is_attested(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_attested_head_binds_to_the_run_the_operator_named(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)

    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=HEAD,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )


def test_attestation_binds_only_the_head_it_names(store: SqliteRunStore) -> None:
    _producer_on_branch(store)
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=HEAD,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(CredentialPublicationRefused):
        publication_producer_for_head(store, branch=BRANCH, head_sha="c" * 40)


def test_attestation_binds_only_the_branch_it_names(store: SqliteRunStore) -> None:
    _producer_on_branch(store)
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch="agent/issue-8",
        commit_sha=HEAD,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(CredentialPublicationRefused):
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)


def test_two_claims_for_one_head_remain_ambiguous(store: SqliteRunStore) -> None:
    """The one-claim-per-head rule holds across the two claim shapes."""
    from kew.audit import emit

    _producer_on_branch(store)
    _run(store, "other", hour=2)
    _contained_profile(store, "other")
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="other",
        issue_num=7,
        item_id="7",
        payload={"branch": BRANCH, "commit_sha": HEAD, "parent_sha": PARENT},
    )
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=HEAD,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_attested_run_must_have_a_row_on_the_branch(store: SqliteRunStore) -> None:
    """``_require_bound_run_on_branch`` still governs the named run."""
    _producer_on_branch(store)
    _run(store, "elsewhere", branch="agent/issue-9", hour=2)
    record_head_attestation(
        store,
        run_id="elsewhere",
        item_id="7",
        branch=BRANCH,
        commit_sha=HEAD,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_a_policy_actor_cannot_write_a_head_attestation(store: SqliteRunStore) -> None:
    """Only a named human attests a head kew did not push."""
    _producer_on_branch(store)

    with pytest.raises(HeadAttestationRefused):
        record_head_attestation(
            store,
            run_id="producer",
            item_id="7",
            branch=BRANCH,
            commit_sha=HEAD,
            parent_shas=(PARENT,),
            actor=Actor("policy", "host-publication"),
        )


def test_an_agent_written_attestation_does_not_bind(store: SqliteRunStore) -> None:
    """Defence in depth: the lookup itself rejects a non-human claim."""
    from kew.audit import emit

    _producer_on_branch(store)
    emit(
        store,
        "publication_head_attested",
        actor=Actor("agent", "codex"),
        run_id="producer",
        issue_num=7,
        item_id="7",
        payload={"branch": BRANCH, "commit_sha": HEAD, "parent_sha": PARENT},
    )

    with pytest.raises(CredentialPublicationRefused) as raised:
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)

    assert raised.value.reason == "publication_lineage_ambiguous"


def test_a_lost_append_is_reported_not_assumed(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``emit`` is best-effort; a head claim is not."""
    import kew.publication_attest as attest

    _producer_on_branch(store)
    monkeypatch.setattr(attest, "emit", lambda *a, **k: None)

    with pytest.raises(HeadAttestationRefused) as raised:
        record_head_attestation(
            store,
            run_id="producer",
            item_id="7",
            branch=BRANCH,
            commit_sha=HEAD,
            parent_shas=(PARENT,),
            actor=Actor("human", "owner"),
        )

    assert "was not durably recorded" in str(raised.value)


# --------------------------------------------------------------------------- #
# `kew publication attest`: the operator's later commits.
# --------------------------------------------------------------------------- #


def test_attest_binds_a_later_commit_through_its_bound_parent(
    store: SqliteRunStore,
) -> None:
    """#2067's shape: cleanup commits pushed on top of an already-bound head."""
    _producer_on_branch(store)
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )

    bound = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )

    assert bound == "producer"
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )


# --------------------------------------------------------------------------- #
# The operator's hand-resolved base merge (#2177).
# --------------------------------------------------------------------------- #


def _bound_head_on_branch(store: SqliteRunStore) -> None:
    """``PARENT`` is the branch's already-bound head, as #2177 assumes."""
    _producer_on_branch(store)
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )


def test_attest_binds_a_hand_resolved_merge_of_the_base_into_the_bound_head(
    store: SqliteRunStore,
) -> None:
    """agent/issue-2151's shape: a textual conflict resolved by hand (#2177)."""
    _bound_head_on_branch(store)

    bound = attest_published_head(
        store,
        branch=BRANCH,
        head_sha=HEAD,
        item_id="7",
        **_readers(parents=(PARENT, ON_BASE)),
    )

    assert bound == "producer"
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )
    # Both parents are recorded: the bound head it extends, and the base
    # commit it merged in.
    [claim] = _attested(store, HEAD)
    assert claim.payload["parent_shas"] == [PARENT, ON_BASE]
    assert claim.payload["parent_sha"] == PARENT


def test_attest_refuses_a_merge_whose_second_parent_is_not_on_the_base(
    store: SqliteRunStore,
) -> None:
    """Merging some other branch in is not a base merge, and is not bound."""
    _bound_head_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_readers(parents=(PARENT, OFF_BASE)),
        )

    assert f"the second parent {OFF_BASE[:12]}" in str(raised.value)
    assert f"not reachable from the fetched {BASE_BRANCH!r}" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_refuses_a_merge_whose_first_parent_is_not_the_bound_head(
    store: SqliteRunStore,
) -> None:
    """Merging the bound head INTO the base swaps the parents; it is not bound."""
    _bound_head_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_readers(parents=(ON_BASE, PARENT)),
        )

    assert f"the first parent {ON_BASE[:12]}" in str(raised.value)
    assert "is not bound to a producer" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_refuses_an_octopus_merge_by_its_parent_count(
    store: SqliteRunStore,
) -> None:
    _bound_head_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_readers(parents=(PARENT, ON_BASE, "e" * 40)),
        )

    assert "has 3 parents" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_reports_an_unreadable_base_reachability_read(
    store: SqliteRunStore,
) -> None:
    """A base that cannot be read is a refusal, never a quiet "off the base"."""
    _bound_head_on_branch(store)
    readers = _readers(parents=(PARENT, ON_BASE))

    def unavailable(sha: str, base: str) -> bool:
        raise RuntimeError("the base branch could not be fetched from origin")

    readers["commit_on_base"] = unavailable

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **readers
        )

    assert "could not be read" in str(raised.value)
    assert not _attested(store, HEAD)


# --------------------------------------------------------------------------- #
# The operator's chain of commits (#2199).
# --------------------------------------------------------------------------- #

MERGE = "e" * 40


def _chain_readers(
    parents: dict[str, tuple[str, ...]], head: str = HEAD
) -> dict[str, Any]:
    """Injected operator-origin reads for a first-parent chain.

    One parent tuple per commit. A sha the map does not name stands for a
    commit the operator repository cannot answer for — a refusal, never a
    silent end to the walk.
    """
    return {
        "base_branch": BASE_BRANCH,
        "remote_head": lambda branch: head,
        "commit_parents": lambda sha: parents[sha],
        "commit_on_base": lambda sha, base: base == BASE_BRANCH and sha == ON_BASE,
    }


def _single_parent_chain(length: int) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """*length* single-parent operator commits above the bound ``PARENT``."""
    chain = [HEAD] + [format(index, "040x") for index in range(1, length)]
    parents = {sha: (chain[position + 1],) for position, sha in enumerate(chain[:-1])}
    parents[chain[-1]] = (PARENT,)
    return chain, parents


def test_attest_binds_a_two_commit_operator_chain(store: SqliteRunStore) -> None:
    """#2053's shape: the base merged into the bound head, then a fix on top.

    Folding the two into one commit would need a force push, which the
    operator hook blocks, so the chain is what there is to attest.
    """
    _bound_head_on_branch(store)

    bound = attest_published_head(
        store,
        branch=BRANCH,
        head_sha=HEAD,
        item_id="7",
        **_chain_readers({HEAD: (MERGE,), MERGE: (PARENT, ON_BASE)}),
    )

    assert bound == "producer"
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )
    # Every commit the claim binds through is recorded, head first, with the
    # already-bound commit the chain reached.
    [claim] = _attested(store, HEAD)
    assert claim.payload["chain_shas"] == [HEAD, MERGE]
    assert claim.payload["bound_parent_sha"] == PARENT
    assert claim.payload["parent_shas"] == [MERGE]


def test_attest_refuses_a_chain_whose_middle_merge_is_off_the_base(
    store: SqliteRunStore,
) -> None:
    """One commit failing the #2177 rules refuses the chain, naming it."""
    _bound_head_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_chain_readers({HEAD: (MERGE,), MERGE: (PARENT, OFF_BASE)}),
        )

    assert MERGE[:12] in str(raised.value)
    assert f"the second parent {OFF_BASE[:12]}" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_binds_a_chain_exactly_as_long_as_the_cap(
    store: SqliteRunStore,
) -> None:
    _bound_head_on_branch(store)
    chain, parents = _single_parent_chain(_MAX_ATTEST_CHAIN)

    bound = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_chain_readers(parents)
    )

    assert bound == "producer"
    [claim] = _attested(store, HEAD)
    assert claim.payload["chain_shas"] == chain


def test_attest_refuses_a_chain_longer_than_the_cap(store: SqliteRunStore) -> None:
    """Uncapped, the walk would read back through the whole base history."""
    _bound_head_on_branch(store)
    _, parents = _single_parent_chain(_MAX_ATTEST_CHAIN + 1)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_chain_readers(parents)
        )

    assert str(_MAX_ATTEST_CHAIN) in str(raised.value)
    assert "is not bound to a producer" in str(raised.value)
    assert not _attested(store, HEAD)


def _merge_chain(length: int) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """*length* hand-resolved base merges, each binding through the next.

    Every sha differs inside the first twelve characters, which is all a
    refusal prints — so a test can say which commit was named.
    """
    chain = [HEAD] + [f"{index:02x}" + "0" * 38 for index in range(1, length)]
    parents = {
        sha: (chain[position + 1], ON_BASE) for position, sha in enumerate(chain[:-1])
    }
    parents[chain[-1]] = (PARENT, ON_BASE)
    return chain, parents


def _counting_readers(
    parents: dict[str, tuple[str, ...]], reads: list[str]
) -> dict[str, Any]:
    """``_chain_readers`` with every base-reachability read recorded.

    Each of those reads is a fetch from the operator's origin in the real
    reader (``kew.worktree.operator_commit_on_base``), so the count is the
    network cost of a walk, not an implementation detail.
    """
    readers = _chain_readers(parents)
    base_reader = readers["commit_on_base"]

    def counted(sha: str, base: str) -> bool:
        reads.append(sha)
        return bool(base_reader(sha, base))

    readers["commit_on_base"] = counted
    return readers


def test_a_capped_walk_fetches_the_base_once_for_the_head_it_was_given(
    store: SqliteRunStore,
) -> None:
    """Nothing on the branch is bound, so the shapes below the head decide nothing."""
    _producer_on_branch(store)
    chain, parents = _merge_chain(_MAX_ATTEST_CHAIN + 1)
    reads: list[str] = []

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_counting_readers(parents, reads),
        )

    # One read, for the head's own merge — not one per merge up to the cap.
    assert reads == [ON_BASE]
    # And the refusal names the head's own unbound first parent, which is the
    # operator's mistake, not whichever ancestor the cap stopped the walk on.
    assert chain[1][:12] in str(raised.value)
    assert chain[_MAX_ATTEST_CHAIN][:12] not in str(raised.value)
    assert str(_MAX_ATTEST_CHAIN) in str(raised.value)
    assert not _attested(store, HEAD)


def test_a_bound_chain_still_checks_every_merge_it_binds_through(
    store: SqliteRunStore,
) -> None:
    """Deferring the path's shape checks does not skip them: #2177 still holds."""
    _bound_head_on_branch(store)
    chain, parents = _merge_chain(3)
    reads: list[str] = []

    bound = attest_published_head(
        store,
        branch=BRANCH,
        head_sha=HEAD,
        item_id="7",
        **_counting_readers(parents, reads),
    )

    assert bound == "producer"
    assert reads == [ON_BASE, ON_BASE, ON_BASE]
    [claim] = _attested(store, HEAD)
    assert claim.payload["chain_shas"] == chain


# --------------------------------------------------------------------------- #
# A claimed commit on the path is never stepped over (#2199).
# --------------------------------------------------------------------------- #

MID = "f" * 40


def test_attest_refuses_a_chain_over_a_commit_two_claims_hold(
    store: SqliteRunStore,
) -> None:
    """Two claims on one commit refuse it permanently; the walk stops there.

    Stepping over that refusal to bind ``HEAD`` to ``PARENT``'s older producer
    would hand the new head exactly the binding the lookup refuses for the
    commit directly below it.
    """
    from kew.audit import emit

    _bound_head_on_branch(store)
    _run(store, "other", hour=2)
    _contained_profile(store, "other")
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="other",
        issue_num=7,
        item_id="7",
        payload={"branch": BRANCH, "commit_sha": MID, "parent_sha": PARENT},
    )
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=MID,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_chain_readers({HEAD: (MID,), MID: (PARENT,)}),
        )

    assert f"{MID[:12]} on {BRANCH!r} is claimed" in str(raised.value)
    assert not _attested(store, HEAD)
    with pytest.raises(CredentialPublicationRefused):
        publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD)


def test_attest_refuses_a_chain_over_a_commit_whose_claim_does_not_bind(
    store: SqliteRunStore,
) -> None:
    """A claim naming a run with no row on the branch refuses, hard (#2124).

    The commit is claimed, so its refusal is the claim's own — not the
    ordinary "this branch has nothing bound on it" the walk continues past.
    """
    _bound_head_on_branch(store)
    _run(store, "elsewhere", branch="agent/issue-9", hour=2)
    record_head_attestation(
        store,
        run_id="elsewhere",
        item_id="7",
        branch=BRANCH,
        commit_sha=MID,
        parent_shas=(PARENT,),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_chain_readers({HEAD: (MID,), MID: (PARENT,)}),
        )

    assert MID[:12] in str(raised.value)
    assert "elsewhere" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_refuses_a_chain_over_a_commit_whose_claims_cannot_be_read(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverifiable commit is not one the walk may step over.

    The claim read fails where both the producer lookup and the claimant check
    page it, so the walk meets the failure at the head's first parent and
    cannot read it as an unclaimed commit to step past.
    """
    import kew.publication_credentials as credentials

    _bound_head_on_branch(store)

    def unreadable(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("the recorded claims could not be paged")

    monkeypatch.setattr(credentials, "_scoped_intent_events", unreadable)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_chain_readers({HEAD: (MID,), MID: (PARENT,)}),
        )

    assert "could not be read" in str(raised.value)
    assert not _attested(store, HEAD)


def test_attest_refuses_a_sha_that_is_not_the_remote_head(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            **_readers(head="c" * 40),
        )

    assert "is not the remote head" in str(raised.value)


def test_attest_refuses_an_unbound_parent(store: SqliteRunStore) -> None:
    """The operator may extend a bound history, never invent a producer."""
    _producer_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "is not bound to a producer" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_attest_refuses_a_parent_only_the_legacy_heuristic_names(
    store: SqliteRunStore,
) -> None:
    """An unclaimed commit on a legacy branch is not a bound history (#2635).

    The run here is uncontained, so the branch-wide newest-run heuristic would
    name it for any head on the branch, claimed or not. That guess is not a
    record that it produced the parent, so binding the operator's head through
    it would attest the head to a run with no claim on its history.
    """
    _run(store, "legacy")
    assert (
        publication_producer_for_head(store, branch=BRANCH, head_sha=PARENT) == "legacy"
    )

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "is not bound to a producer" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_attest_reports_an_unreadable_remote_instead_of_binding(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)

    def unavailable(branch: str) -> str:
        raise RuntimeError("operator origin query failed")

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            base_branch=BASE_BRANCH,
            remote_head=unavailable,
            commit_parents=lambda sha: (PARENT,),
            commit_on_base=lambda sha, base: True,
        )

    assert "could not be read" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_attest_reports_an_unreadable_parent_instead_of_binding(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)

    def absent(sha: str) -> str:
        raise RuntimeError("commit is not in the operator repository")

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store,
            branch=BRANCH,
            head_sha=HEAD,
            item_id="7",
            base_branch=BASE_BRANCH,
            remote_head=lambda branch: HEAD,
            commit_parents=absent,
            commit_on_base=lambda sha, base: True,
        )

    assert "cannot be bound through them" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_attest_refuses_an_item_the_lookup_would_never_page(
    store: SqliteRunStore,
) -> None:
    """A mistyped --item would record a durable event nobody ever reads."""
    _producer_on_branch(store)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="9", **_readers()
        )

    assert "would never be read" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_attest_refuses_a_branch_with_more_than_one_tracker_item(
    store: SqliteRunStore,
) -> None:
    _producer_on_branch(store)
    store.save(
        RunRecord(
            run_id="other-item",
            issue_num=8,
            item_id="8",
            branch=BRANCH,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 25, 2, tzinfo=UTC),
            finished_at=datetime(2026, 9, 25, 2, 30, tzinfo=UTC),
            attempt_number=1,
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "more than one tracker item" in str(raised.value)


@pytest.mark.parametrize(
    ("branch", "sha", "item", "base"),
    [
        ("", HEAD, "7", BASE_BRANCH),
        (BRANCH, "not-a-sha", "7", BASE_BRANCH),
        (BRANCH, HEAD.upper(), "7", BASE_BRANCH),
        (BRANCH, HEAD, "", BASE_BRANCH),
        (BRANCH, HEAD, "7", ""),
    ],
    ids=["no-branch", "malformed-sha", "uppercase-sha", "no-item", "no-base"],
)
def test_attest_validates_its_arguments_before_any_read(
    store: SqliteRunStore, branch: str, sha: str, item: str, base: str
) -> None:
    def never(*args: Any) -> str:  # pragma: no cover - must not be called
        raise AssertionError("no git read may happen for an invalid argument")

    with pytest.raises(HeadAttestationRefused):
        attest_published_head(
            store,
            branch=branch,
            head_sha=sha,
            item_id=item,
            base_branch=base,
            remote_head=never,
            commit_parents=never,
            commit_on_base=never,
        )


# --------------------------------------------------------------------------- #
# The one-claim-per-head rule, enforced where the claim is written.
# --------------------------------------------------------------------------- #


def _host_intent(store: SqliteRunStore, *, run_id: str, commit_sha: str) -> None:
    """The claim shape the host publisher writes for a head it pushed itself."""
    from kew.audit import emit

    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        issue_num=7,
        item_id="7",
        payload={"branch": BRANCH, "commit_sha": commit_sha, "parent_sha": PARENT},
    )


def test_attest_refuses_a_head_another_run_already_claims(
    store: SqliteRunStore,
) -> None:
    """A second claim would refuse the branch as ambiguous, with no undo."""
    _producer_on_branch(store)
    _run(store, "second", hour=2)
    _contained_profile(store, "second")
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )
    _host_intent(store, run_id="second", commit_sha=HEAD)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "already claimed by second" in str(raised.value)
    # The claim that was already working is still the one that binds.
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "second"
    )


def test_attesting_the_same_head_twice_is_idempotent(store: SqliteRunStore) -> None:
    """This run's own claim is no obstacle to re-running the command."""
    _producer_on_branch(store)
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )

    first = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )
    second = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )

    assert (first, second) == ("producer", "producer")
    claims = [
        event
        for event in store.list_audit_events(event="publication_head_attested")
        if event.payload["commit_sha"] == HEAD
    ]
    assert len(claims) == 1
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )


def test_attest_reports_an_unreadable_branch_history_as_a_refusal(
    store: SqliteRunStore,
) -> None:
    """A branch past the producer guard's window refuses, never raises through.

    ``kew publication attest`` documents exit codes 0 and 2 only. An evidence
    refusal escaping as a ``CredentialPublicationRefused`` would traceback out
    of the CLI with exit code 1 instead.
    """
    for index in range(40):
        _run(store, f"run-{index}", hour=index % 24)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "could not be read as one tracker item" in str(raised.value)
    assert not store.list_audit_events(event="publication_head_attested")


def test_two_writers_racing_for_one_head_cannot_both_claim_it(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """Asking first is a check-then-append; the append is what serializes it.

    Both writers pass ``head_unclaimable_for_run`` before either appends —
    that is the window two stranded runs finalized at once, or ``kew finalize``
    racing ``kew publication attest``, actually run in. Exactly one claim may
    reach the chain: two would refuse the branch as
    ``publication_lineage_ambiguous`` permanently, which is the failure this
    module exists to prevent (#2124).
    """
    from kew.publication_attest import head_unclaimable_for_run

    _producer_on_branch(store)
    _run(store, "second", hour=2)
    _contained_profile(store, "second")
    db_path = str(tmp_path / "dispatch.db")
    past_the_guard = threading.Barrier(2, timeout=10)
    results: list[tuple[str, str]] = []
    guard = threading.Lock()

    def claim(run_id: str) -> None:
        outcome = "claimed"
        try:
            with SqliteRunStore(db_path) as writer:
                assert (
                    head_unclaimable_for_run(
                        writer,
                        branch=BRANCH,
                        head_sha=HEAD,
                        run_id=run_id,
                        item_id="7",
                    )
                    is None
                )
                past_the_guard.wait()
                record_head_attestation(
                    writer,
                    run_id=run_id,
                    item_id="7",
                    branch=BRANCH,
                    commit_sha=HEAD,
                    parent_shas=(PARENT,),
                    actor=Actor("human", "owner"),
                )
        except HeadAttestationRefused as exc:
            outcome = str(exc)
        except Exception as exc:  # pragma: no cover - diagnostic capture
            outcome = f"unexpected: {exc!r}"
        with guard:
            results.append((run_id, outcome))

    writers = [
        threading.Thread(target=claim, args=(run_id,))
        for run_id in ("producer", "second")
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=20)

    assert not any(writer.is_alive() for writer in writers)
    claims = [
        event
        for event in store.list_audit_events(event="publication_head_attested")
        if event.payload["commit_sha"] == HEAD
    ]
    assert len(claims) == 1
    refused = [outcome for _run_id, outcome in results if outcome != "claimed"]
    assert len(refused) == 1, results
    assert "already claimed by" in refused[0]
    # The head still binds — to the writer that won, never to nobody.
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        claims[0].run_id
    )
    assert verify_chain(store.read_chain_rows()).ok


# --------------------------------------------------------------------------- #
# A branch whose runs name the node and its own PR (#3193).
# --------------------------------------------------------------------------- #

#: The PR opened from ``agent/issue-7``.
PR = 70


def _pr_subject_run(store: SqliteRunStore, run_id: str, *, run_kind: str) -> None:
    """A run on the node's PR that recorded the PR number as its item."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=PR,
            item_id=str(PR),
            branch=BRANCH,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 25, 2, tzinfo=UTC),
            finished_at=datetime(2026, 9, 25, 2, 30, tzinfo=UTC),
            attempt_number=1,
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind=run_kind,
        )
    )


def test_attest_binds_an_operator_commit_on_a_branch_that_also_names_its_pr(
    store: SqliteRunStore,
) -> None:
    """Dispatch 7, PR 70, a run on PR 70, then the operator's commit.

    The branch's runs name 7 and 70, so ``--item 7`` used to be refused as
    owned by more than one item, and the next review on the PR stayed refused.
    """
    from kew.publication_credentials import recorded_head_claims

    _producer_on_branch(store)
    _pr_subject_run(store, "ci-fix-70", run_kind="ci_fix")
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )

    bound = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )

    assert bound == "producer"
    # What the next `kew review 70` asks before it launches.
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )
    # A writer reads every scope the lookup reads, so a second claim is seen.
    assert recorded_head_claims(store, branch=BRANCH, head_sha=HEAD) == {"producer"}


def test_attest_still_refuses_an_item_beside_another_nodes_run(
    store: SqliteRunStore,
) -> None:
    """Only the node's own PR's runs are allowed beside it, not other work."""
    _producer_on_branch(store)
    _pr_subject_run(store, "ci-fix-70", run_kind="ci_fix")
    store.save(
        RunRecord(
            run_id="other-node",
            issue_num=8,
            item_id="8",
            branch=BRANCH,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 25, 3, tzinfo=UTC),
            attempt_number=1,
        )
    )
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "more than one tracker item" in str(raised.value)


def test_attest_names_an_unreadable_owner_lookup_instead_of_a_mixed_branch(
    store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run store failing on the owner read refuses with the read error.

    The branch names 7 and its PR 70, so attesting reads the runs twice: once
    as one tracker item, once for the owner beside its PR runs. A store that
    fails on the second read used to be reported as a branch naming more than
    one item, hiding that the history could not be read at all.
    """
    _producer_on_branch(store)
    _pr_subject_run(store, "ci-fix-70", run_kind="ci_fix")
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )
    attested_before = len(store.list_audit_events(event="publication_head_attested"))
    real_page = store.list_runs_by_branch_after
    pages_read = 0

    def page_then_fail(*args: Any, **kwargs: Any) -> Any:
        nonlocal pages_read
        pages_read += 1
        if pages_read > 1:
            raise OSError("disk I/O error")
        return real_page(*args, **kwargs)

    monkeypatch.setattr(store, "list_runs_by_branch_after", page_then_fail)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "could not be read for the item owning the branch" in str(raised.value)
    assert "more than one tracker item" not in str(raised.value)
    assert (
        len(store.list_audit_events(event="publication_head_attested"))
        == attested_before
    )


def _pr_scoped_intent(store: SqliteRunStore, *, run_id: str, commit_sha: str) -> None:
    """The host publisher's claim for a head it pushed for a run on the PR."""
    from kew.audit import emit

    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        issue_num=PR,
        item_id=str(PR),
        payload={
            "branch": BRANCH,
            "commit_sha": commit_sha,
            "parent_sha": ON_BASE,
            "scope": "item",
        },
    )


def test_attest_refuses_the_same_run_claiming_a_head_under_another_item(
    store: SqliteRunStore,
) -> None:
    """A mixed branch's lookup counts (run, scope) claims: one run's claim on
    a head under the PR's item and a second one under the node's item are two,
    and a chained claim cannot be withdrawn. So the second is refused and
    nothing is written; the head keeps binding to the run that pushed it."""
    _producer_on_branch(store)
    _pr_subject_run(store, "ci-fix-70", run_kind="ci_fix")
    _contained_profile(store, "ci-fix-70")
    # kew's host publisher pushed PARENT, then HEAD, for the ci-fix run.
    _pr_scoped_intent(store, run_id="ci-fix-70", commit_sha=PARENT)
    _pr_scoped_intent(store, run_id="ci-fix-70", commit_sha=HEAD)

    with pytest.raises(HeadAttestationRefused) as raised:
        attest_published_head(
            store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
        )

    assert "already claimed by ci-fix-70 under '70'" in str(raised.value)
    assert _attested(store, HEAD) == []
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "ci-fix-70"
    )


def test_reattesting_a_head_on_a_branch_naming_its_pr_is_idempotent(
    store: SqliteRunStore,
) -> None:
    """The same run under the same item is still no obstacle on a mixed branch."""
    _producer_on_branch(store)
    _pr_subject_run(store, "ci-fix-70", run_kind="ci_fix")
    record_head_attestation(
        store,
        run_id="producer",
        item_id="7",
        branch=BRANCH,
        commit_sha=PARENT,
        parent_shas=(),
        actor=Actor("human", "owner"),
    )

    first = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )
    second = attest_published_head(
        store, branch=BRANCH, head_sha=HEAD, item_id="7", **_readers()
    )

    assert (first, second) == ("producer", "producer")
    assert len(_attested(store, HEAD)) == 1
    assert publication_producer_for_head(store, branch=BRANCH, head_sha=HEAD) == (
        "producer"
    )
