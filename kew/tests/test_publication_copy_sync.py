"""#2457: a remediation must not sync its base by COPYING files.

The incident, on PR #2438: a review-remediation commit changed 68 files, and 63
of them were byte-identical to versions on ``main`` the branch never authored —
the agent had brought the base's newer content in by copying files rather than
merging. The PR's diff against its unmoved merge base grew from ~45 files to
108, the next review rounds reviewed the base's own code as if this node had
written it, and when the base moved again five of the copies conflicted.

Publication now holds such a commit for the operator and names the files. The
distinguishing fact is the host's own: kew integrated the base into the review
checkout before the agent ran, so content that is the base branch's *current*
head's own, and is held by neither the branch nor the commit kew integrated,
can only have been fetched and copied.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kew import subprocess_utils, worktree
from kew.config import DispatchConfig
from kew.publication_canonical import (
    CanonicalIdentity,
    CopySync,
    PublicationHeld,
    publish_checkout,
)
from kew.publication_copy_sync import copy_sync_flags
from kew.publication_filter import ManifestEntry
from kew.publication_repository import CanonicalRepository, CanonicalRepositoryError
from kew.review import build_review_prompt
from kew.review_checkout import _integrated_base_sha
from kew.worktree import _copy_sync_guard

BRANCH = "agent/issue-2457"
OWNED = "src/owned.py"
UNRELATED = "src/unrelated.py"
#: A file the base changed once and has not touched since, so the bytes kew's
#: own base integration left in the checkout are ALSO the base head's current
#: content. Only the integration exempts it, which makes it the path that
#: proves what an unreadable integration must not do.
CARRIED = "src/carried.py"
#: A file the base deleted AFTER kew integrated it: the branch and the
#: integration both still hold it, so only a hand-made sync would remove it.
GONE = "src/gone.py"
_IDENTITY = CanonicalIdentity(name="kew", email="kew@example.invalid")


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def _entry(path: str, digest: str) -> ManifestEntry:
    return ManifestEntry(path=path, mode="100644", sha256=digest)


def _flags(entries: list[ManifestEntry], **overrides: Any) -> tuple[str, ...]:
    kwargs: dict[str, Any] = {
        "published_paths": [entry.path for entry in entries],
        "base_digest": lambda _path: None,
        "integrated_digest": lambda _path: None,
        "upstream_digest": lambda _path: None,
        "label": "origin/main",
    }
    kwargs.update(overrides)
    return copy_sync_flags(entries, **kwargs)


def test_a_write_that_is_the_base_heads_own_content_is_a_copy() -> None:
    flags = _flags(
        [_entry(UNRELATED, "a" * 64)],
        base_digest={UNRELATED: "b" * 64}.get,
        upstream_digest={UNRELATED: "a" * 64}.get,
    )

    assert len(flags) == 1
    assert flags[0].startswith(f"{UNRELATED}: copied from origin/main")


def test_content_the_integrated_base_already_holds_is_not_a_copy() -> None:
    """kew's own ``git merge origin/<base>`` brought those bytes in before the
    agent ran. Holding them would stop every remediation whose base moved —
    which is exactly what #2362 fixed."""
    assert (
        _flags(
            [_entry(UNRELATED, "a" * 64)],
            base_digest={UNRELATED: "b" * 64}.get,
            integrated_digest={UNRELATED: "a" * 64}.get,
            upstream_digest={UNRELATED: "a" * 64}.get,
        )
        == ()
    )


def test_content_the_branch_already_holds_introduces_nothing() -> None:
    assert (
        _flags(
            [_entry(UNRELATED, "a" * 64)],
            base_digest={UNRELATED: "a" * 64}.get,
            upstream_digest={UNRELATED: "a" * 64}.get,
        )
        == ()
    )


def test_a_write_the_filter_dropped_is_never_judged() -> None:
    """Only paths that actually change the published tree can be a copy; an
    entry the filter removed never reaches the remote at all."""
    assert (
        _flags(
            [_entry(UNRELATED, "a" * 64)],
            published_paths=(),
            base_digest={UNRELATED: "b" * 64}.get,
            upstream_digest={UNRELATED: "a" * 64}.get,
        )
        == ()
    )


def test_an_entry_without_a_digest_is_never_a_copy() -> None:
    """Identity is the whole rule. Two unknown answers must not compare equal."""
    assert (
        _flags(
            [ManifestEntry(path=UNRELATED, mode="100644")],
            upstream_digest=lambda _path: None,
        )
        == ()
    )


def test_every_copied_path_is_named_in_path_order() -> None:
    entries = [_entry(UNRELATED, "a" * 64), _entry(OWNED, "c" * 64)]
    flags = _flags(
        entries,
        base_digest=lambda _path: "b" * 64,
        upstream_digest={UNRELATED: "a" * 64, OWNED: "c" * 64}.get,
    )

    assert [flag.split(":")[0] for flag in flags] == [OWNED, UNRELATED]


def test_a_deletion_the_base_head_made_is_a_copy() -> None:
    """#2633: the base deleted the file since kew integrated it, and the agent
    deleted it too — the same hand-made sync as copying a newer version."""
    flags = _flags(
        [],
        published_paths=[GONE],
        deletions=[GONE],
        base_digest={GONE: "b" * 64}.get,
        integrated_digest={GONE: "b" * 64}.get,
    )

    assert len(flags) == 1
    assert flags[0].startswith(f"{GONE}: deleted because origin/main deleted it")


@pytest.mark.parametrize(
    "overrides",
    [
        # The base head still holds the file: an ordinary deletion.
        {"upstream_digest": {GONE: "b" * 64}.get},
        # The integration had already dropped it: kew's merge brought that.
        {"integrated_digest": lambda _path: None},
        # The filter did not publish the deletion.
        {"published_paths": ()},
    ],
)
def test_a_deletion_the_base_head_does_not_explain_is_not_a_copy(
    overrides: dict[str, Any],
) -> None:
    kwargs: dict[str, Any] = {
        "published_paths": [GONE],
        "deletions": [GONE],
        "base_digest": {GONE: "b" * 64}.get,
        "integrated_digest": {GONE: "b" * 64}.get,
    }
    kwargs.update(overrides)

    assert _flags([], **kwargs) == ()


def test_deletions_and_writes_are_named_together_in_path_order() -> None:
    flags = _flags(
        [_entry(UNRELATED, "a" * 64)],
        published_paths=[UNRELATED, GONE],
        deletions=[GONE],
        base_digest=lambda _path: "b" * 64,
        integrated_digest=lambda _path: "b" * 64,
        upstream_digest={UNRELATED: "a" * 64}.get,
    )

    assert [flag.split(":")[0] for flag in flags] == [GONE, UNRELATED]


# ---------------------------------------------------------------------------
# ...end to end, through a real canonical publication
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def scene(tmp_path: Path) -> dict[str, Any]:
    """The #2438 shape: a branch, the base kew integrated, and a newer base.

    ``head`` is the branch head the remediation checkout was cut at and the
    base its publication applies to. ``integrated`` is the base commit kew's
    checkout integration merged in — the checkout holds its ``unrelated``
    bytes. ``upstream`` is where the base branch has moved since, holding a
    third version of that file the checkout was never given.

    ``carried`` moved once, in the integrated commit, and is unchanged at
    ``upstream``: the integration is the only tree that can excuse it.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    for path, body in (
        (OWNED, "owned = 1\n"),
        (UNRELATED, "unrelated = 1\n"),
        (CARRIED, "carried = 1\n"),
        (GONE, "gone = 1\n"),
    ):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")

    # The branch's own work, and the head its remediation publishes onto.
    (seed / OWNED).write_text("owned = 2\n")
    _git(seed, "commit", "-qam", "the node's own change")
    head = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")

    _git(seed, "checkout", "-q", "main")
    (seed / UNRELATED).write_text("unrelated = 2\n")
    (seed / CARRIED).write_text("carried = 2\n")
    _git(seed, "commit", "-qam", "the base moves")
    _git(seed, "push", "-q", "origin", "main")
    integrated = _git(seed, "rev-parse", "HEAD")

    (seed / UNRELATED).write_text("unrelated = 3\n")
    _git(seed, "rm", "-q", GONE)
    _git(seed, "commit", "-qam", "the base moves again")
    _git(seed, "push", "-q", "origin", "main")
    upstream = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    # What kew's base integration left behind, before the agent touched it.
    (checkout / UNRELATED).write_text("unrelated = 2\n")
    (checkout / CARRIED).write_text("carried = 2\n")
    return {
        "origin": origin,
        "checkout": checkout,
        "head": head,
        "integrated": integrated,
        "upstream": upstream,
    }


def _publish(scene: dict[str, Any], scratch: Path, **overrides: Any) -> Any:
    head = str(scene["head"])
    kwargs: dict[str, Any] = {
        "remote": str(scene["origin"]),
        "branch": BRANCH,
        "parents": (head,),
        "base_sha": head,
        "merged_base_sha": str(scene["integrated"]),
        "copy_sync": CopySync(sha=str(scene["upstream"]), label="origin/main"),
        "message": "fix: address review feedback on PR #2438",
        "identity": _IDENTITY,
        "force_with_lease": f"{BRANCH}:{head}",
        "output_guard": None,
        "scratch": scratch,
    }
    kwargs.update(overrides)
    return publish_checkout(Path(scene["checkout"]), **kwargs)


def _branch_head(origin: Path) -> str:
    return _git(origin, "rev-parse", f"refs/heads/{BRANCH}")


def test_a_copied_newer_base_blob_is_held_and_named(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC. The agent fetched the base and copied its newest version of a file
    the node has no business in. Nothing is pushed and the file is named."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path / "scratch")

    assert any(UNRELATED in flag for flag in raised.value.flags)
    assert all(OWNED not in flag for flag in raised.value.flags)
    assert all(CARRIED not in flag for flag in raised.value.flags)
    assert "kew resolve" in raised.value.flags[0]
    assert _branch_head(Path(scene["origin"])) == str(scene["head"])


def test_the_host_record_judges_copies_when_no_merged_base_is_declared(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """#2632. Without a declared integration parent there is no merged base for
    the protected-path exemption, but the rule still reads the host's record of
    what it merged: that content rides along and a copy is still held."""
    checkout = Path(scene["checkout"])
    (checkout / OWNED).write_text("owned = 3\n")
    armed = CopySync(
        sha=str(scene["upstream"]),
        label="origin/main",
        integrated=str(scene["integrated"]),
    )

    published = _publish(
        scene, tmp_path / "carried", merged_base_sha=None, copy_sync=armed
    )
    assert CARRIED in published.changed_paths

    (checkout / UNRELATED).write_text("unrelated = 3\n")
    with pytest.raises(PublicationHeld) as raised:
        _publish(
            scene,
            tmp_path / "copied",
            merged_base_sha=None,
            copy_sync=armed,
            parents=(published.commit_sha,),
            base_sha=published.commit_sha,
            force_with_lease=f"{BRANCH}:{published.commit_sha}",
        )
    assert any(UNRELATED in flag for flag in raised.value.flags)
    assert all(CARRIED not in flag for flag in raised.value.flags)


def test_a_deletion_copied_from_the_base_head_is_held_and_named(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """#2633: the base removed a file after kew integrated it; an agent that
    removes it too has synced the base by hand, and was never judged."""
    (Path(scene["checkout"]) / GONE).unlink()
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path / "scratch")

    assert [flag.split(":")[0] for flag in raised.value.flags] == [GONE]
    assert "kew resolve" in raised.value.flags[0]
    assert _branch_head(Path(scene["origin"])) == str(scene["head"])


def test_an_ordinary_deletion_publishes(scene: dict[str, Any], tmp_path: Path) -> None:
    """The control: a file the base head still holds is the agent's own call."""
    (Path(scene["checkout"]) / OWNED).unlink()

    published = _publish(scene, tmp_path / "scratch")

    assert published.commit_sha == _branch_head(Path(scene["origin"]))
    assert OWNED in published.changed_paths


def test_an_ordinary_fix_commit_publishes(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """The control: the same scene, the same arming, only the agent's own edit
    instead of a copy — and the base content kew itself integrated riding along
    exactly as #2362 requires."""
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(scene, tmp_path / "scratch")

    origin = Path(scene["origin"])
    assert published.commit_sha == _branch_head(origin)
    assert sorted(published.changed_paths) == [CARRIED, OWNED, UNRELATED]
    assert _git(origin, "show", f"{BRANCH}:{UNRELATED}") == "unrelated = 2"
    assert _git(origin, "show", f"{BRANCH}:{CARRIED}") == "carried = 2"


def test_a_real_merge_commit_is_never_judged(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """``kew resolve`` publishes the base as a second parent. That IS the tool
    this rule points at, so the base's own content is expected there."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")

    published = _publish(
        scene,
        tmp_path / "scratch",
        parents=(str(scene["head"]), str(scene["upstream"])),
    )

    assert published.commit_sha == _branch_head(Path(scene["origin"]))
    assert published.parent_shas == (str(scene["head"]), str(scene["upstream"]))


def test_a_base_that_has_not_moved_since_the_integration_judges_nothing(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """The integration already accounts for every byte of that commit, so there
    is nothing to copy and no reason to spend a fetch asking."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")

    published = _publish(
        scene,
        tmp_path / "scratch",
        merged_base_sha=str(scene["upstream"]),
        copy_sync=CopySync(sha=str(scene["upstream"]), label="origin/main"),
    )

    assert published.commit_sha == _branch_head(Path(scene["origin"]))


# ---------------------------------------------------------------------------
# A tree that will not answer leaves the publication unjudged, so it is held
# ---------------------------------------------------------------------------


def _assert_held_unjudged(scene: dict[str, Any], scratch: Path) -> None:
    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, scratch)

    assert len(raised.value.flags) == 1
    assert raised.value.flags[0].startswith("copy-sync check could not run")
    assert _branch_head(Path(scene["origin"])) == str(scene["head"])


def _failing_listing(monkeypatch: pytest.MonkeyPatch, commit_sha: str) -> None:
    listing = CanonicalRepository.tree_blob_oids

    def flaky(self: CanonicalRepository, sha: str) -> dict[str, str]:
        if sha == commit_sha:
            raise CanonicalRepositoryError("ls-tree failed: transient")
        return listing(self, sha)

    monkeypatch.setattr(CanonicalRepository, "tree_blob_oids", flaky)


def test_an_integrated_base_that_will_not_list_holds(
    scene: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2633: a transient ``ls-tree`` failure on the commit kew merged used to
    stand the rule down, so the copy below — the exact incident — published.
    A rule that cannot read its evidence holds instead of judging blind, and
    says so rather than naming the path as a copy."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")
    _failing_listing(monkeypatch, str(scene["integrated"]))

    _assert_held_unjudged(scene, tmp_path / "scratch")


def test_an_unfetchable_integrated_base_holds(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """Same demand one step earlier: the integrated commit cannot be brought
    into the host's repository at all."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path / "scratch", merged_base_sha="d" * 40)

    assert raised.value.flags[0].startswith("copy-sync check could not run")
    assert _branch_head(Path(scene["origin"])) == str(scene["head"])


def test_a_base_head_that_will_not_list_holds(
    scene: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2633: an unreadable base head was read as "nothing to recognise", so
    every copy of it published unjudged."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")
    _failing_listing(monkeypatch, str(scene["upstream"]))

    _assert_held_unjudged(scene, tmp_path / "scratch")


def test_an_unfetchable_base_head_holds(scene: dict[str, Any], tmp_path: Path) -> None:
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(
            scene,
            tmp_path / "scratch",
            copy_sync=CopySync(sha="e" * 40, label="origin/main"),
        )

    assert raised.value.flags[0].startswith("copy-sync check could not run")
    assert _branch_head(Path(scene["origin"])) == str(scene["head"])


def test_a_branch_tree_that_will_not_read_holds(
    scene: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch's own tree is the other exemption: a blob that will not read
    says nothing about whether the branch holds the content."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")
    origin = Path(scene["origin"])
    stale = _git(origin, "rev-parse", f"refs/heads/{BRANCH}:{UNRELATED}")
    reading = CanonicalRepository.read_blob

    def flaky(self: CanonicalRepository, oid: str) -> bytes:
        if oid == stale:
            raise CanonicalRepositoryError("cat-file blob failed: transient")
        return reading(self, oid)

    monkeypatch.setattr(CanonicalRepository, "read_blob", flaky)

    _assert_held_unjudged(scene, tmp_path / "scratch")


def test_an_unarmed_publication_behaves_exactly_as_before(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """Every publication that names no base head — the dispatch path, the
    resolve path — is not judged for a copy at all."""
    (Path(scene["checkout"]) / UNRELATED).write_text("unrelated = 3\n")

    published = _publish(scene, tmp_path / "scratch", copy_sync=None)

    assert published.commit_sha == _branch_head(Path(scene["origin"]))


# ---------------------------------------------------------------------------
# What arms the rule, and what tells the agent not to earn it
# ---------------------------------------------------------------------------


def test_the_guard_is_armed_only_when_the_host_knows_what_it_integrated(
    scene: dict[str, Any],
) -> None:
    armed = _copy_sync_guard(
        integrated_base_sha=str(scene["integrated"]),
        upstream_head=str(scene["upstream"]),
        merged_base_branch="main",
    )

    assert armed == CopySync(
        sha=str(scene["upstream"]),
        label="origin/main",
        integrated=str(scene["integrated"]),
    )


@pytest.mark.parametrize("missing", ["integrated_base_sha", "upstream_head"])
def test_an_unresolvable_fact_disarms_rather_than_holds(missing: str) -> None:
    """Without the commit kew integrated there is no way to tell the base
    content the HOST brought from base content an agent copied, and holding on
    that ambiguity would stop every remediation whose base had moved (#2362)."""
    facts: dict[str, Any] = {
        "integrated_base_sha": "a" * 40,
        "upstream_head": "b" * 40,
        "merged_base_branch": "main",
    }
    facts[missing] = None

    assert _copy_sync_guard(**facts) is None


BRANCH_HEAD = "a" * 40
UPSTREAM_HEAD = "b" * 40
INTEGRATED = "c" * 40


def _canonical_publication_kwargs(
    worktree_dir: Path,
    *,
    integration: tuple[str, ...] = (INTEGRATED,),
    **overrides: Any,
) -> dict[str, Any]:
    """What the remediation publisher hands :func:`publish_checkout`.

    The production call site, run for real with the remote and the publication
    itself stubbed out — the only place in kew that arms this rule. The
    checkout's verdict on the integration parent is *integration*.
    """
    captured: dict[str, Any] = {}

    def fake_publish_checkout(_worktree_dir: Path, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    def head(_remote: str, ref: str, **_kwargs: Any) -> str:
        return {BRANCH: BRANCH_HEAD, "main": UPSTREAM_HEAD}[ref]

    facts: dict[str, Any] = {
        "merged_base_branch": "main",
        "integrated_base_sha": INTEGRATED,
    }
    facts.update(overrides)
    with (
        patch.object(worktree, "publication_remote", return_value="/remote.git"),
        patch.object(worktree, "remote_branch_sha", side_effect=head),
        patch.object(worktree, "_integration_parent", return_value=integration),
        patch.object(worktree, "publish_checkout", fake_publish_checkout),
    ):
        worktree._publish_existing_pr_branch_canonical(
            worktree_dir,
            BRANCH,
            commit_message="fix: address review feedback on PR #2438",
            expected_remote_sha=BRANCH_HEAD,
            repo=None,
            **facts,
        )
    return captured


def test_the_remediation_publisher_arms_the_rule_with_the_base_head(
    tmp_path: Path,
) -> None:
    """The rule is dead code unless this one call site passes it. Dropping the
    ``copy_sync`` argument has to fail here, and so has dropping the
    integrated commit that makes the exemption kew's own merge earns the
    INTEGRATED commit rather than wherever the base has drifted to since
    (#2457 review)."""
    kwargs = _canonical_publication_kwargs(tmp_path)

    assert kwargs["copy_sync"] == CopySync(
        sha=UPSTREAM_HEAD, label="origin/main", integrated=INTEGRATED
    )
    assert kwargs["parents"] == (BRANCH_HEAD, INTEGRATED)
    assert kwargs["merged_base_sha"] == INTEGRATED


def test_a_rejected_integration_parent_earns_no_protected_path_exemption(
    tmp_path: Path,
) -> None:
    """#2632. The checkout vetoed the integration (abandoned, undone, or
    already in the branch), so no parent of the published commit accounts for
    that sha and protected bytes matching it are not excused. The copy-sync
    rule still knows what the host merged, so it judges exactly as before."""
    kwargs = _canonical_publication_kwargs(tmp_path, integration=())

    assert kwargs["parents"] == (BRANCH_HEAD,)
    assert kwargs["merged_base_sha"] is None
    assert kwargs["copy_sync"] == CopySync(
        sha=UPSTREAM_HEAD, label="origin/main", integrated=INTEGRATED
    )


def test_no_recorded_integration_never_exempts_the_base_head(
    tmp_path: Path,
) -> None:
    """Without the commit kew integrated — a checkout from before #2362 —
    nothing is judged for a copy, and the base branch's current head earns no
    protected-path exemption either: no parent of the commit is that head
    (#2632)."""
    kwargs = _canonical_publication_kwargs(
        tmp_path, integration=(), integrated_base_sha=None
    )

    assert kwargs["copy_sync"] is None
    assert kwargs["merged_base_sha"] is None


def test_the_integrated_base_is_read_from_the_tree_the_host_fetched(
    scene: dict[str, Any],
) -> None:
    """The checkout's ``origin/main`` is where kew's own base integration
    merged from, so that ref IS the host's record of what it brought in."""
    assert _integrated_base_sha(Path(scene["checkout"]), "main") == str(
        scene["upstream"]
    )
    assert _integrated_base_sha(Path(scene["checkout"]), "") == ""


def test_the_remediation_brief_forbids_bringing_the_base_in_by_copy() -> None:
    prompt = build_review_prompt(
        pr_number=2438,
        issue_number=2304,
        branch_name=BRANCH,
        comments=[],
        config=DispatchConfig(),
        base_branch="main",
    )

    rule = next(line for line in prompt.splitlines() if "by copying files" in line)
    assert "origin/main" in rule
    assert "kew resolve" in rule
