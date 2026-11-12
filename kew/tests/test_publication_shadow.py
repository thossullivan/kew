"""W1.1 node 5c: the shadow comparison.

The property that matters most here is NOT that the comparison is right — node
5a proved the tree is exact. It is that the comparison **cannot break the
dispatch it observes**. A shadow that can raise makes "leave it off" the correct
operational choice, and then it never collects the evidence it exists to
collect. So every stage is fault-injected and every one must come back as a
:class:`ShadowOutcome`.

The live half needs docker and ``KEW_TEST_AGENT_IMAGE``. The "remote" in these
tests is a local bare repository — in production it must be the real remote, for
the reason ``provision_canonical`` documents, but what is under test here is the
comparison, not where the objects came from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.publication_artifact import Artifact
from kew.publication_repository import CanonicalRepositoryError
from kew.publication_shadow import (
    STATUS_FAILED,
    STATUS_MATCH,
    STATUS_REFUSED,
    STATUS_REVIEW_REQUIRED,
    ShadowOutcome,
    compare_publication,
    provision_canonical,
)


class _NotAnInterrupt(BaseException):
    """A BaseException that is not KeyboardInterrupt or SystemExit.

    `except Exception` would let this escape; `except BaseException` catches it.
    Nothing in the stdlib fills this role — KeyboardInterrupt and SystemExit are
    exactly the two the code re-raises — so the test declares one.
    """


#: Stands in for a successful extraction. Empty, because these tests fault the
#: stage that comes next — what the artifact CONTAINS is the docker-gated
#: suite's question, not this one's.
_EMPTY_ARTIFACT = Artifact(
    base_sha="0" * 40, entries=(), blobs_dir=Path("/nonexistent"), deletions=()
)

_IMAGE = os.environ.get("KEW_TEST_AGENT_IMAGE", "")

# Opted in without docker names docker, so KEW_REQUIRE_TOOLS=1 can tell an
# opt-in left off (intended) from a missing tool (tests/support/required_tools.py).
_REQUIRES_IMAGE = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker"),
    reason=(
        "needs docker for the KEW_TEST_AGENT_IMAGE live half"
        if _IMAGE
        else "opt-in: set KEW_TEST_AGENT_IMAGE to a locally built agent image"
    ),
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch):
    for tool in ("docker", "git"):
        resolved = shutil.which(tool)
        if resolved:
            monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, resolved)


@pytest.fixture
def scene(tmp_path: Path):
    """An agent checkout with work on it, and a bare 'remote' holding base."""

    def build(extra: dict[str, bytes] | None = None) -> dict[str, object]:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        _git(checkout, "init", "-q", ".")
        _git(checkout, "config", "user.email", "t@example.com")
        _git(checkout, "config", "user.name", "T")
        (checkout / "keep.txt").write_text("keep\n")
        (checkout / "edit.txt").write_text("before\n")
        (checkout / "gone.txt").write_text("doomed\n")
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "base")
        base = _git(checkout, "rev-parse", "HEAD")

        remote = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
        _git(checkout, "push", "-q", str(remote), f"{base}:refs/heads/main")

        (checkout / "edit.txt").write_text("after\n")
        (checkout / "added.txt").write_text("added\n")
        (checkout / "gone.txt").unlink()
        for name, content in (extra or {}).items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "work")

        return {
            "checkout": checkout,
            "base": base,
            "remote": str(remote),
            "legacy_tree": _git(checkout, "rev-parse", "HEAD^{tree}"),
            "workspace": tmp_path / "shadow",
        }

    return build


def _compare(scene_data: dict[str, object], **overrides: object) -> ShadowOutcome:
    kwargs: dict[str, object] = {
        "runtime": "docker",
        "proxy_environment": "none",
        "image": _IMAGE or "kew-agent:latest",
        "checkout_dir": scene_data["checkout"],
        "workspace": scene_data["workspace"],
        "remote": scene_data["remote"],
        "base_sha": scene_data["base"],
        "legacy_tree": scene_data["legacy_tree"],
        "slug": "shadow",
    }
    kwargs.update(overrides)
    return compare_publication(**kwargs)  # type: ignore[arg-type]


class TestProvisioning:
    def test_it_fetches_exactly_the_base_commit(self, tmp_path: Path, scene):
        data = scene()
        dest = tmp_path / "canonical.git"

        provision_canonical(dest, remote=data["remote"], base_sha=data["base"])

        assert _git(dest, "rev-parse", "--is-bare-repository") == "true"
        assert _git(dest, "cat-file", "-t", data["base"]) == "commit"

    def test_a_base_the_remote_does_not_have_is_refused(self, tmp_path: Path, scene):
        data = scene()

        with pytest.raises(RuntimeError, match="could not fetch"):
            provision_canonical(
                tmp_path / "canonical.git", remote=data["remote"], base_sha="0" * 40
            )

    def test_it_provisions_through_the_one_canonical_provisioner(
        self, tmp_path: Path, scene
    ):
        """I09A: shadow mode has no provisioner of its own any more.

        Two implementations of "fetch the base somewhere the host owns" is two
        places for the host-owned check to be forgotten, and the shadow's copy
        was the one without it. It now delegates to I07's, so a rule added
        there covers both callers.
        """
        data = scene()
        seen: list[dict[str, object]] = []

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "kew.publication_shadow.provision",
                lambda dest, **kwargs: seen.append({"dest": dest, **kwargs}),
            )
            provision_canonical(
                tmp_path / "canonical.git", remote=data["remote"], base_sha=data["base"]
            )

        assert seen == [
            {
                "dest": tmp_path / "canonical.git",
                "source": data["remote"],
                "base_sha": data["base"],
            }
        ]

    def test_a_base_that_is_not_a_commit_id_never_reaches_git(
        self, tmp_path: Path, scene
    ):
        """Inherited from I07's provisioner, which the shadow never had."""
        data = scene()

        with pytest.raises(CanonicalRepositoryError, match="commit id"):
            provision_canonical(
                tmp_path / "canonical.git", remote=data["remote"], base_sha="HEAD"
            )

    def test_an_agent_writable_local_source_is_refused(self, tmp_path: Path, scene):
        """The shadow's own provisioner handed a local path straight to git,
        which runs ``upload-pack`` inside it and reads its configuration."""
        data = scene()
        Path(str(data["remote"])).chmod(0o777)

        with pytest.raises(CanonicalRepositoryError, match="writable"):
            provision_canonical(
                tmp_path / "canonical.git", remote=data["remote"], base_sha=data["base"]
            )


@_REQUIRES_IMAGE
class TestTheComparisonItself:
    def test_faithful_work_matches(self, scene):
        outcome = _compare(scene())

        assert outcome.status == STATUS_MATCH
        assert outcome.agrees
        assert outcome.broker_tree == outcome.legacy_tree
        assert (outcome.entries, outcome.deletions) == (2, 1)

    def test_a_matching_comparison_keeps_no_evidence(self, scene):
        data = scene()

        outcome = _compare(data)

        assert outcome.evidence is None
        # Nothing to look at, so nothing is kept — otherwise every green
        # dispatch would leave a canonical clone on disk forever.
        assert not Path(data["workspace"]).exists()  # type: ignore[arg-type]

    def test_a_symlink_in_the_work_fails_the_shadow_and_keeps_the_evidence(
        self, tmp_path: Path, scene
    ):
        """Producer refusal is a failed comparison, never a false match.

        Silently thinning the artifact would build a divergent broker tree. The
        producer instead refuses the symlink, so the shadow preserves that
        failure and its evidence without claiming the trees were compared.
        """
        data = scene()
        checkout = Path(data["checkout"])  # type: ignore[arg-type]
        (checkout / "link").symlink_to("edit.txt")
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "symlink")
        data["legacy_tree"] = _git(checkout, "rev-parse", "HEAD^{tree}")

        outcome = _compare(data)

        assert outcome.status == STATUS_FAILED
        assert "symlink" in outcome.detail
        assert outcome.broker_tree is None
        # Kept, because there is now something worth opening.
        assert outcome.evidence == Path(data["workspace"])  # type: ignore[arg-type]
        assert outcome.evidence.exists()

    def test_it_asks_the_destination_what_it_already_has(self, scene):
        """Making an existing file executable is only detectable against the
        destination's modes.

        The rule needs to know ``keep.txt`` was 100644 at base. Without the
        destination's state the filter has nothing to compare against and
        reports clean — because it never asked the question, not because the
        answer was clean. That is the failure mode a shadow must not have.
        """
        data = scene()
        checkout = Path(data["checkout"])  # type: ignore[arg-type]
        (checkout / "keep.txt").chmod(0o755)
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-qm", "chmod")
        data["legacy_tree"] = _git(checkout, "rev-parse", "HEAD^{tree}")

        outcome = _compare(data)

        assert outcome.status == STATUS_REFUSED
        assert any("executable" in r for r in outcome.rejections)

    def test_a_flagged_entry_holds_for_review_and_names_the_flag(self, scene):
        """CI code is the flag tier, not the reject tier.

        A workflow runs with the repository's secrets, so it is held for a human
        rather than refused outright — a distinction the outcome has to carry,
        because "a human should look" and "this must not publish" call for
        different responses.
        """
        data = scene(extra={".github/workflows/ci.yml": b"on: push\njobs: {}\n"})

        outcome = _compare(data)

        assert outcome.status == STATUS_REVIEW_REQUIRED
        assert any("workflows/ci.yml" in f for f in outcome.flags)
        assert outcome.rejections == ()
        assert outcome.broker_tree is None

    def test_a_refused_entry_reports_the_rule_that_fired(self, scene):
        # A .gitattributes introducing a filter driver is code the destination
        # would run on checkout, so the filter refuses the publication outright.
        data = scene(extra={".gitattributes": b"* filter=pwn\n"})

        outcome = _compare(data)

        assert outcome.status == STATUS_REFUSED
        assert any("gitattributes" in r for r in outcome.rejections)
        # A refusal is NOT a divergence: the trees were never compared, because
        # the broker would have published nothing at all.
        assert outcome.broker_tree is None


@_REQUIRES_IMAGE
class TestItCannotBreakTheDispatchItObserves:
    """Fault at every stage. Each must return, not raise."""

    def test_an_unreachable_remote(self, scene):
        outcome = _compare(scene(), remote="/nonexistent/remote.git")

        assert outcome.status == STATUS_FAILED
        assert "fetch" in outcome.detail

    def test_a_base_that_is_not_an_object_id(self, scene):
        # build_extract_argv raises ValueError; provisioning fails first, but
        # either way the caller sees an outcome.
        outcome = _compare(scene(), base_sha="origin/main")

        assert outcome.status == STATUS_FAILED

    def test_an_image_that_does_not_exist(self, scene):
        outcome = _compare(scene(), image="kew-agent:definitely-not-built")

        assert outcome.status == STATUS_FAILED

    def test_a_checkout_that_is_not_there(self, tmp_path: Path, scene):
        outcome = _compare(scene(), checkout_dir=tmp_path / "missing")

        assert outcome.status == STATUS_FAILED

    def test_a_workspace_that_already_exists(self, scene):
        data = scene()
        Path(data["workspace"]).mkdir()  # type: ignore[arg-type]

        outcome = _compare(data)

        assert outcome.status == STATUS_FAILED
        assert "workspace" in outcome.detail

    def test_a_runtime_that_is_not_on_the_allowlist(self, scene):
        outcome = _compare(scene(), runtime="definitely-not-a-runtime")

        assert outcome.status == STATUS_FAILED


class TestItCannotBreakTheDispatchWithoutDockerEither:
    def test_every_stage_failure_still_returns_an_outcome(
        self, scene, monkeypatch: pytest.MonkeyPatch
    ):
        """Every stage faulted directly, with no container involved.

        The stages after extraction are only reachable once extraction has
        succeeded, so extraction is STUBBED for those rather than run. Two
        reasons, and the first is why this test was green while proving nothing:

        * Depending on a real container made the last two faults unreachable
          whenever the image was absent — which is how ``verify.sh`` runs, and
          which is where this first failed honestly. A guarantee this
          load-bearing must be checked everywhere the suite runs, not only on a
          machine that happens to have built the image.
        * An earlier version reused one workspace path across iterations. The
          directory was removed and recreated at the same path, and the
          runtime's file-sharing layer kept mapping the old inode, so
          ``/artifact`` did not exist inside the container. Extraction failed
          first and the assertion was satisfied by an unrelated failure.

        Each iteration still gets its own workspace, because a comparison
        requires one that does not yet exist.
        """
        data = scene()
        for index, (target, boom) in enumerate(
            (
                (
                    "kew.publication_shadow.provision_canonical",
                    RuntimeError("no remote"),
                ),
                ("kew.publication_shadow.extract_in_container", OSError("no docker")),
                ("kew.publication_shadow._destination_state", MemoryError("absurd")),
                ("kew.publication_shadow.filter_artifact", RecursionError("deep")),
                # A BaseException that is NOT an operator interrupt — the case
                # that proves the guard is not `except Exception`.
                ("kew.publication_shadow.apply_to_tree", _NotAnInterrupt("odd")),
            )
        ):

            def explode(*args: object, _boom=boom, **kwargs: object) -> object:
                raise _boom

            # A nested context, so undo() drops only THIS patch and leaves the
            # autouse allowlist entries in place. Undoing everything and putting
            # TOOL_PATHS back by hand worked, but it meant the fixture's
            # guarantee lasted exactly one iteration.
            with pytest.MonkeyPatch.context() as patch:
                if target != "kew.publication_shadow.extract_in_container":
                    # Stubbed so the later stages are reachable with no docker.
                    patch.setattr(
                        "kew.publication_shadow.extract_in_container",
                        lambda *a, **k: _EMPTY_ARTIFACT,
                    )
                patch.setattr(target, explode)
                outcome = _compare(data, workspace=data["workspace"] / f"i{index}")

            assert outcome.status == STATUS_FAILED, target
            assert outcome.legacy_tree == data["legacy_tree"], target
            assert boom.args[0] in outcome.detail, target

    def test_a_deliberate_interrupt_is_the_one_thing_that_gets_through(self, scene):
        """Ctrl-C is the operator, not a shadow failure.

        Swallowing it would leave kew unresponsive for the length of a container
        run and then publish anyway, having been told to stop — worse than the
        thing the never-raises rule exists to prevent.
        """
        data = scene()

        for boom in (KeyboardInterrupt("ctrl-c"), SystemExit(1)):

            def explode(*args: object, _boom=boom, **kwargs: object) -> object:
                raise _boom

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr("kew.publication_shadow.provision_canonical", explode)
                with pytest.raises(type(boom)):
                    _compare(data, workspace=data["workspace"] / type(boom).__name__)
