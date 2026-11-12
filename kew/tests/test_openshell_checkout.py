"""The realized checkout: download, caps, modes, denial evidence (node N7).

Nothing here contacts a gateway. :class:`FakeGateway` stands in for the N2
client and implements ``sandbox download`` as a host-to-host COPY of a tree the
test prepared — which is the only way to express the failures this node exists
for: a transfer that merges into an existing tree, one that flattens file modes,
one that loses ``.git``, and one that brings back more than the checkout caps
allow.

The copy is a copy rather than a move for a reason the assertions depend on:
the "sandbox" tree stays readable afterwards, so a test can show that the
ORIGINAL host checkout was left exactly as the launch uploaded it while the
realized one carries the agent's work.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew import openshell_checkout, subprocess_utils
from kew.config import DispatchConfig
from kew.openshell import OpenShellError, OpenShellNotFound
from kew.openshell_checkout import (
    REFUSAL_DESTINATION_OCCUPIED,
    REFUSAL_INDEX_UNREADABLE,
    REFUSAL_NO_GIT,
    REFUSAL_NO_HOST_ROOM,
    REFUSAL_OVER_QUOTA,
    REFUSAL_SANDBOX_RELEASED,
    CheckoutRealization,
    RealizedCheckout,
    denial_events,
    realize_sandbox_checkout,
    realized_checkout_dir,
)
from kew.openshell_launch import CHECKOUT_PATH, OpenShellSandboxHandle
from kew.sandbox import PROMPT_FILENAME
from kew.tracking import DispatchResult

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="the index read needs a real git"
)

SANDBOX = "kew-2322"
SANDBOX_ID = "sbx_01HN4Q8ZJ2WME5T"

#: A sandbox log carrying one denial among ordinary lines, in the shape the
#: 2026-10-03 spike's gateway printed: a timestamp, a level, a sentence.
LOG_WITH_DENIAL = (
    "2026-10-03T18:04:12Z info sandbox started\n"
    "2026-10-03T18:04:19Z warn egress denied: connect to 10.0.0.9:443 "
    "is not permitted by policy-claude-code.yaml\n"
    "2026-10-03T18:05:02Z info agent exited 0\n"
)


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """The index read goes through the subprocess chokepoint, whose allowlist
    a preflight normally fills. ``setitem`` restores the global afterwards."""
    from kew import subprocess_utils

    resolved = shutil.which("git")
    assert resolved is not None  # the module-level skipif guarantees it
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


@pytest.fixture(autouse=True)
def _a_host_with_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """The realization refuses a download the host has no room for.

    The room it asks for is a checkout at the cap (4 GiB) plus the host
    free-space floor (1 GiB), which the ``tmp_path`` device is not obliged to
    have — a 1 GiB tmpfs ``/tmp`` is a normal container shape, and every test
    here would then assert against a refusal it is not about. So the probe is
    faked for the whole module and the runner's disk stops being an input.
    :class:`TestTheHostRoom` fakes it the other way.
    """
    _fake_free_space(monkeypatch, 64 * 1024**3)


def _fake_free_space(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    """Report *free* bytes free on every device, for the test's duration."""
    monkeypatch.setattr(
        openshell_checkout.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=free, used=0, free=free),
    )


@dataclass
class Call:
    name: str
    detail: dict[str, Any] = field(default_factory=dict)


class FakeGateway:
    """The two client calls a realization makes, plus the delete that follows.

    ``tree`` is what the sandbox holds: ``sandbox_download`` copies it into the
    destination it is given. ``nest`` copies it into ``<destination>/work``
    instead, which is the other copy semantic the CLI could have.
    """

    def __init__(
        self,
        tree: Path | None = None,
        *,
        logs_text: str = LOG_WITH_DENIAL,
        logs_error: BaseException | None = None,
        download_error: BaseException | None = None,
        drop_modes: bool = False,
        all_executable: bool = False,
        nest: bool = False,
    ) -> None:
        self.calls: list[Call] = []
        self.tree = tree
        self.logs_text = logs_text
        self.logs_error = logs_error
        self.download_error = download_error
        self.drop_modes = drop_modes
        self.all_executable = all_executable
        self.nest = nest

    @property
    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def logs(self, name: str, *, since: str = "") -> str:
        self.calls.append(Call("logs", {"name": name, "since": since}))
        if self.logs_error is not None:
            raise self.logs_error
        return self.logs_text

    def sandbox_download(self, name: str, source: str, destination: Path) -> None:
        self.calls.append(
            Call(
                "sandbox_download",
                {"name": name, "source": source, "destination": destination},
            )
        )
        if self.download_error is not None:
            raise self.download_error
        assert self.tree is not None
        root = destination / "work" if self.nest else destination
        _copy_tree(
            self.tree,
            root,
            drop_modes=self.drop_modes,
            all_executable=self.all_executable,
        )

    def sandbox_delete(self, name: str) -> None:
        self.calls.append(Call("sandbox_delete", {"name": name}))


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    drop_modes: bool,
    all_executable: bool = False,
) -> None:
    """Copy *source* into *destination*, optionally flattening file modes.

    ``drop_modes`` and ``all_executable`` are the two transfers this node has
    to survive: every file arrives ``0o644``, so the executables the index
    names are no longer executable, or every file arrives ``0o755``, so the
    plain ones the index names now are.

    Symlinks are recreated as symlinks, because a transfer that resolved them
    would hide the escape :class:`TestTheIndexPaths` is about. ``rglob`` does
    not descend into one (``recurse_symlinks=False``), so a link pointing out
    of the tree copies its target path and nothing else.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        target = destination / path.relative_to(source)
        if path.is_symlink():
            target.symlink_to(os.readlink(path))
            continue
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.write_bytes(path.read_bytes())
        target.chmod(_transferred_mode(path, drop_modes, all_executable))


def _transferred_mode(path: Path, drop_modes: bool, all_executable: bool) -> int:
    if all_executable:
        return 0o755
    if drop_modes:
        return 0o644
    return stat.S_IMODE(path.lstat().st_mode)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-local repository
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "kew tests",
            "GIT_AUTHOR_EMAIL": "tests@kew.invalid",
            "GIT_COMMITTER_NAME": "kew tests",
            "GIT_COMMITTER_EMAIL": "tests@kew.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    return result.stdout


def _checkout(root: Path, *, files: dict[str, str] | None = None) -> Path:
    """A standalone checkout in the shape a dispatch uploads.

    One ``100644`` file and one ``100755`` script, both committed, so the index
    carries both modes the download is verified against — plus the prompt
    handoff the launch writes into the tree.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "script.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (root / "script.sh").chmod(0o755)
    for name, body in (files or {}).items():
        (root / name).write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "base")
    (root / PROMPT_FILENAME).write_text("do the thing\n", encoding="utf-8")
    return root


def _add_executable(tree: Path, name: str) -> None:
    """Commit one more ``100755`` entry into *tree*.

    The flatten signature needs a side of the index it can read a WHOLE-TREE
    normalization off: with a single indexed executable, "all of them came
    back non-executable" is equally what the agent's own ``chmod -x`` on it
    produces, and that tie goes to the agent's edit
    (``_MIN_FLATTEN_EVIDENCE``, PR #2451 review). The flatten tests below
    therefore index two of the mode they are about.
    """
    (tree / name).write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (tree / name).chmod(0o755)
    _git(tree, "add", "-A")
    _git(tree, "commit", "--quiet", "-m", f"add {name}")


def _realize(gateway: FakeGateway, checkout: Path, **kwargs: Any) -> Any:
    return realize_sandbox_checkout(
        gateway,  # type: ignore[arg-type]
        SANDBOX,
        checkout_dir=checkout,
        **kwargs,
    )


# ── what comes back, and what does not ───────────────────────────────────────


class TestTheDownload:
    def test_a_deleted_file_stays_deleted_in_the_realized_checkout(
        self, tmp_path: Path
    ) -> None:
        """The whole reason the download may not land on the original tree.

        A download into an existing checkout MERGES: every file the agent
        removed is still sitting there afterwards, so the deletion never
        reaches the PR. The realized tree is a new directory, so the agent's
        removal is the realized tree's state.
        """
        uploaded = _checkout(tmp_path / "work", files={"doomed.txt": "delete me\n"})
        sandbox_tree = _checkout(tmp_path / "sandbox")
        gateway = FakeGateway(sandbox_tree)

        realization = _realize(gateway, uploaded)

        assert realization.refusal == ""
        realized = realization.checkout.path
        assert realized == realized_checkout_dir(uploaded)
        assert not (realized / "doomed.txt").exists()
        # The uploaded tree is untouched: it is still the launch snapshot, and
        # a refused realization has to be able to fall back to refusing rather
        # than to a half-overwritten tree.
        assert (uploaded / "doomed.txt").read_text(encoding="utf-8") == "delete me\n"

    def test_the_download_refuses_a_destination_that_already_holds_files(
        self, tmp_path: Path
    ) -> None:
        """Including, above all, a destination that is a checkout already."""
        uploaded = _checkout(tmp_path / "work")
        occupied = realized_checkout_dir(uploaded)
        occupied.mkdir(parents=True)
        (occupied / "left-over.txt").write_text("from an earlier run\n")
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, uploaded)

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_DESTINATION_OCCUPIED
        assert "sandbox_download" not in gateway.names

    def test_the_prompt_handoff_does_not_come_back_with_the_work(
        self, tmp_path: Path
    ) -> None:
        """It was uploaded with the checkout; it must not reach a PR (#725)."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert not (realization.checkout.path / PROMPT_FILENAME).exists()

    def test_a_download_that_nests_the_source_is_still_found(
        self, tmp_path: Path
    ) -> None:
        """``.git`` identifies the checkout, whichever copy semantic the CLI has."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"), nest=True)

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.checkout.path.name == "work"
        assert (realization.checkout.path / ".git").is_dir()

    def test_a_download_without_dot_git_is_refused(self, tmp_path: Path) -> None:
        """A checkout with no ``.git`` cannot be published and may be partial."""
        bare = tmp_path / "sandbox"
        bare.mkdir()
        (bare / "README.md").write_text("no repository here\n", encoding="utf-8")
        gateway = FakeGateway(bare)

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_NO_GIT
        assert ".git" in realization.refusal

    def test_a_failed_download_refuses_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """The caller is reached through a cleanup seam that eats exceptions."""
        gateway = FakeGateway(
            _checkout(tmp_path / "sandbox"),
            download_error=OpenShellNotFound(["openshell"], reason="no such sandbox"),
        )

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.checkout is None
        assert "could not be realized" in realization.refusal
        # Still evidence: what the gateway denied was read before the download.
        assert realization.denials


class TestTheCaps:
    def test_a_cap_breach_is_refused_and_recorded(self, tmp_path: Path) -> None:
        """The checkout quota, enforced at the one moment it is knowable."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"), quota_bytes=1)

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_OVER_QUOTA
        assert "breaches the checkout caps" in realization.refusal
        assert realization.as_event()["refusal_code"] == REFUSAL_OVER_QUOTA

    def test_the_entry_cap_is_enforced_too(self, tmp_path: Path) -> None:
        """An inode bomb measures no bytes; entries are the other half."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"), quota_entries=1)

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_OVER_QUOTA

    def test_a_refused_download_does_not_keep_the_host_disk(
        self, tmp_path: Path
    ) -> None:
        """The over-cap tree is the agent's fill; the refusal gives it back.

        The size is only knowable once the tree has landed, so a refusal that
        left it there would let an agent that filled its sandbox hold the
        host's free space until the dispatch cleanup came past — having
        already been refused publication for exactly that.
        """
        uploaded = _checkout(tmp_path / "work")
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, uploaded, quota_bytes=1)

        assert realization.refusal_code == REFUSAL_OVER_QUOTA
        assert not realized_checkout_dir(uploaded).exists()

    def test_a_failed_download_leaves_no_partial_tree_behind(
        self, tmp_path: Path
    ) -> None:
        """A half-written transfer is not publishable and not kew's to keep."""
        uploaded = _checkout(tmp_path / "work")
        gateway = FakeGateway(
            _checkout(tmp_path / "sandbox"),
            download_error=OpenShellError(["openshell"], reason="transfer failed"),
        )

        realization = _realize(gateway, uploaded)

        assert realization.checkout is None
        assert not realized_checkout_dir(uploaded).exists()

    def test_an_occupied_destination_is_refused_and_never_removed(
        self, tmp_path: Path
    ) -> None:
        """The one refusal that must not delete: that tree is not this run's.

        Every other refusal removes what it refused, and this destination was
        refused precisely because something else put it there — an earlier
        run, or an operator. Deleting it would turn a conservative refusal
        into data loss.
        """
        uploaded = _checkout(tmp_path / "work")
        occupied = realized_checkout_dir(uploaded)
        occupied.mkdir(parents=True)
        (occupied / "left-over.txt").write_text("from an earlier run\n")
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, uploaded)

        assert realization.refusal_code == REFUSAL_DESTINATION_OCCUPIED
        assert (occupied / "left-over.txt").read_text() == "from an earlier run\n"

    @pytest.mark.parametrize("dangling", [False, True])
    def test_a_symlinked_destination_is_occupied_and_never_written_through(
        self, tmp_path: Path, dangling: bool
    ) -> None:
        """A pre-created ``<checkout>-realized -> /outside-empty`` (#2626).

        Following the link read it as the empty directory it points at, and
        the download landed outside. The link is refused whatever its target
        holds, nothing is downloaded, and neither the link nor its target is
        touched.
        """
        uploaded = _checkout(tmp_path / "work")
        outside = tmp_path / "outside-empty"
        if not dangling:
            outside.mkdir()
        link = realized_checkout_dir(uploaded)
        link.symlink_to(outside, target_is_directory=True)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, uploaded)

        assert realization.refusal_code == REFUSAL_DESTINATION_OCCUPIED
        assert "sandbox_download" not in gateway.names
        assert link.is_symlink()
        assert outside.exists() is not dangling
        if not dangling:
            assert list(outside.iterdir()) == []

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root can list a directory with no permissions"
    )
    def test_a_destination_that_cannot_be_listed_counts_as_occupied(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The probe may not raise, because a raise is what removes the tree.

        Every later refusal discards the destination, so the one stage whose
        destination might not be kew's has to answer rather than throw: a
        directory it cannot even list is occupied, and stays.
        """
        blocked = realized_checkout_dir(tmp_path / "work")
        blocked.mkdir(parents=True)
        (blocked / "left-over.txt").write_text("from an earlier run\n")
        blocked.chmod(0o000)
        try:
            with caplog.at_level("WARNING"):
                occupied = openshell_checkout._destination_is_occupied(blocked)
        finally:
            blocked.chmod(0o700)

        assert occupied is True
        assert "treated as occupied" in caplog.text
        assert (blocked / "left-over.txt").exists()

    def test_a_checkout_inside_the_caps_reports_its_size(self, tmp_path: Path) -> None:
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.checkout.used_bytes > 0
        assert realization.checkout.entries > 0


class TestTheHostRoom:
    """The half of the caps that has to be decided BEFORE the transfer.

    The download is the gateway CLI's: kew hands it a destination and gets
    control back when it is finished, so there is no chunk to charge against a
    budget the way the docker path's bounded copier charges its own writes.
    What is left is the room taken before the first byte.
    """

    def test_a_host_without_room_for_a_cap_sized_checkout_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And refused BEFORE the download, which is the only useful moment."""
        _fake_free_space(monkeypatch, 2 * 1024**3)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_NO_HOST_ROOM
        assert "free-space floor" in realization.refusal
        assert "sandbox_download" not in gateway.names

    def test_the_refused_destination_is_not_left_sitting_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The empty directory the check was made against goes with it."""
        _fake_free_space(monkeypatch, 2 * 1024**3)
        uploaded = _checkout(tmp_path / "work")
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        _realize(gateway, uploaded)

        assert not realized_checkout_dir(uploaded).exists()

    def test_the_floor_the_run_is_held_to_is_the_hosts_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Room for the cap plus the floor is enough; a byte less is not.

        The floor is the one the launch already required of this device and
        the docker watchdog enforces while a container runs — not a figure
        this module invented.
        """
        from kew.sandbox import CHECKOUT_QUOTA_BYTES, HOST_FREE_SPACE_FLOOR_BYTES

        exactly_enough = CHECKOUT_QUOTA_BYTES + HOST_FREE_SPACE_FLOOR_BYTES
        _fake_free_space(monkeypatch, exactly_enough)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        assert _realize(gateway, _checkout(tmp_path / "work")).refusal == ""

        _fake_free_space(monkeypatch, exactly_enough - 1)
        short = FakeGateway(_checkout(tmp_path / "sandbox2"))

        refused = _realize(short, _checkout(tmp_path / "work2"))

        assert refused.refusal_code == REFUSAL_NO_HOST_ROOM

    def test_an_unreadable_free_space_probe_does_not_discard_the_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """kew's own instrumentation failing is not the agent's run failing."""

        def _no_statvfs(_path: Path) -> SimpleNamespace:
            raise OSError("statvfs said no")

        monkeypatch.setattr(openshell_checkout.shutil, "disk_usage", _no_statvfs)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        with caplog.at_level("WARNING"):
            realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.refusal == ""
        assert "could not be probed" in caplog.text


class TestTheFileModes:
    def test_modes_and_dot_git_survive_a_faithful_download(
        self, tmp_path: Path
    ) -> None:
        """Both index modes are verified, and nothing is repaired."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()
        assert (realized.path / ".git").is_dir()
        assert _executable(realized.path / "script.sh")
        assert not _executable(realized.path / "README.md")

    def test_a_download_that_flattens_modes_is_repaired_from_the_index(
        self, tmp_path: Path
    ) -> None:
        """Recoverable, so it is repaired and RECORDED rather than refused."""
        sandbox_tree = _checkout(tmp_path / "sandbox")
        _add_executable(sandbox_tree, "setup.sh")
        gateway = FakeGateway(sandbox_tree, drop_modes=True)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is False
        assert realized.mode_repairs == ("script.sh", "setup.sh")
        assert _executable(realized.path / "script.sh")
        assert _executable(realized.path / "setup.sh")
        assert not _executable(realized.path / "README.md")

    def test_the_agents_own_chmod_survives_the_verification(
        self, tmp_path: Path
    ) -> None:
        """One executable losing its bit is an edit, not a broken transfer.

        Two scripts are indexed ``100755`` and the agent stripped one. The
        transfer plainly kept modes — the other one is still executable — so
        restoring from the index would silently undo the agent's work.
        """
        sandbox_tree = _checkout(tmp_path / "sandbox")
        (sandbox_tree / "second.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (sandbox_tree / "second.sh").chmod(0o755)
        _git(sandbox_tree, "add", "-A")
        _git(sandbox_tree, "commit", "--quiet", "-m", "second script")
        (sandbox_tree / "second.sh").chmod(0o644)
        gateway = FakeGateway(sandbox_tree)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()
        assert not _executable(realized.path / "second.sh")
        assert _executable(realized.path / "script.sh")

    def test_a_download_that_makes_everything_executable_is_repaired_too(
        self, tmp_path: Path
    ) -> None:
        """The other direction a flatten goes, and the one that reached a PR.

        A transfer that normalizes to ``0o755`` leaves every indexed
        ``100755`` file executable, so the lost-bit signature says nothing —
        and a ``100644`` → ``100755`` mode change on every tracked file went
        to publication reported as ``modes_preserved=True``.
        """
        sandbox_tree = _checkout(tmp_path / "sandbox", files={"NOTES.md": "notes\n"})
        gateway = FakeGateway(sandbox_tree, all_executable=True)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is False
        assert realized.mode_repairs == ("NOTES.md", "README.md")
        assert not _executable(realized.path / "README.md")
        assert not _executable(realized.path / "NOTES.md")
        assert _executable(realized.path / "script.sh")

    def test_the_agents_own_chmod_plus_x_survives_the_verification(
        self, tmp_path: Path
    ) -> None:
        """The twin of the ``chmod -x`` case, in the direction just added.

        Two plain files, one of which the agent made executable. The transfer
        plainly carried modes — the other one is still ``0o644`` — so the
        repair must not strip the bit the agent asked for.
        """
        sandbox_tree = _checkout(tmp_path / "sandbox", files={"notes.md": "notes\n"})
        (sandbox_tree / "notes.md").chmod(0o755)
        gateway = FakeGateway(sandbox_tree)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()
        assert _executable(realized.path / "notes.md")

    def test_a_file_the_agent_deleted_is_not_read_as_a_lost_mode(
        self, tmp_path: Path
    ) -> None:
        """An indexed path with no file on disk is a deletion, not a mode loss."""
        sandbox_tree = _checkout(tmp_path / "sandbox")
        (sandbox_tree / "script.sh").unlink()
        gateway = FakeGateway(sandbox_tree)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()

    def test_the_only_indexed_executables_chmod_minus_x_survives(
        self, tmp_path: Path
    ) -> None:
        """A side of the index holding ONE file cannot tell the two apart.

        ``_checkout`` indexes a single ``100755`` script, and the agent
        stripped its bit. "Every indexed executable came back
        non-executable" is then true of a deliberate ``chmod -x`` with nothing
        else on that side to break the tie — and the repair fired on it,
        silently reverting the agent's work (PR #2451 review).
        """
        sandbox_tree = _checkout(tmp_path / "sandbox")
        (sandbox_tree / "script.sh").chmod(0o644)
        gateway = FakeGateway(sandbox_tree)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()
        assert not _executable(realized.path / "script.sh")

    def test_the_only_indexed_plain_files_chmod_plus_x_survives(
        self, tmp_path: Path
    ) -> None:
        """The twin of it, on the other side of the index.

        One indexed ``100644`` file, made executable by the agent, is the same
        ambiguous observation in the direction the flatten-to-``0o755``
        signature reads.
        """
        sandbox_tree = _checkout(tmp_path / "sandbox")
        (sandbox_tree / "README.md").chmod(0o755)
        gateway = FakeGateway(sandbox_tree)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        assert realized.modes_preserved is True
        assert realized.mode_repairs == ()
        assert _executable(realized.path / "README.md")

    def test_an_index_read_that_times_out_refuses_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The read's own timeout is this realization's refusal.

        ``realize_sandbox_checkout`` catches typed failures only, so the
        ``TimeoutExpired`` the index read raises escaped it: a download that
        had SUCCEEDED wrote no realization evidence at all, lost the denial
        events already collected, left the undiscarded tree on host disk, and
        finalized the run saying the work was never downloaded off the sandbox
        (PR #2451 review).
        """

        def _never_answers(cmd: list[str], **_: Any) -> Any:
            raise subprocess.TimeoutExpired(
                cmd, openshell_checkout._INDEX_READ_TIMEOUT_SECONDS
            )

        monkeypatch.setattr(subprocess_utils, "run_bytes", _never_answers)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        uploaded = _checkout(tmp_path / "work")

        realization = _realize(gateway, uploaded)

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_INDEX_UNREADABLE
        assert "did not come back" in realization.refusal
        # The denial evidence read before the download is still reported…
        assert len(realization.denials) == 1
        # …and the download kew will not publish keeps no host disk either.
        assert not realized_checkout_dir(uploaded).exists()

    def test_an_index_read_that_fails_refuses_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same refusal for a non-zero ``ls-files``, whose stderr is kept."""

        def _failed(cmd: list[str], **_: Any) -> Any:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: not a git repo")

        monkeypatch.setattr(subprocess_utils, "run_bytes", _failed)
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        uploaded = _checkout(tmp_path / "work")

        realization = _realize(gateway, uploaded)

        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_INDEX_UNREADABLE
        assert "fatal: not a git repo" in realization.refusal
        assert not realized_checkout_dir(uploaded).exists()


class TestTheIndexPaths:
    """The index is agent-written, and the repair CHMODS what it names."""

    def test_an_entry_behind_a_symlinked_parent_chmods_nothing(
        self, tmp_path: Path
    ) -> None:
        """The escape no relative-path check catches.

        ``escape -> <outside>`` on disk and an index entry for
        ``escape/victim.sh`` name a path with no ``..`` and no leading slash,
        and ``lstat`` follows every component but the last — so the entry read
        as a regular file to restore, and the repair chmodded a HOST file
        outside the realized checkout entirely.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "victim.sh"
        victim.write_text("host file\n", encoding="utf-8")
        victim.chmod(0o600)
        sandbox_tree = _checkout(tmp_path / "sandbox")
        _add_executable(sandbox_tree, "setup.sh")
        (sandbox_tree / "escape").symlink_to(outside)
        blob = _git(sandbox_tree, "rev-parse", "HEAD:script.sh").strip()
        _git(
            sandbox_tree,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100755,{blob},escape/victim.sh",
        )
        gateway = FakeGateway(sandbox_tree, drop_modes=True)

        realized = _realize(gateway, _checkout(tmp_path / "work")).checkout

        # The flatten IS repaired — the tree's own scripts are restored — and
        # the entry pointing out of the tree is simply not one of the paths.
        assert realized.mode_repairs == ("script.sh", "setup.sh")
        assert stat.S_IMODE(victim.lstat().st_mode) == 0o600
        assert (realized.path / "escape").is_symlink()

    @pytest.mark.parametrize(
        "relative",
        ["../outside.sh", "/etc/crontab", "nested/../../outside.sh"],
    )
    def test_the_relative_escapes_are_refused_as_well(
        self, tmp_path: Path, relative: str
    ) -> None:
        """An absolute path and a ``..`` component, still checked here.

        git itself refuses to check such an entry out, but nothing proves it
        refuses to LIST one, and the restore walks the listing.
        """
        root = _checkout(tmp_path / "sandbox")

        assert openshell_checkout._escapes_tree(root, relative) is True

    def test_an_ordinary_nested_path_is_not_read_as_an_escape(
        self, tmp_path: Path
    ) -> None:
        """The check is conservative, not a ban on directories."""
        root = _checkout(tmp_path / "sandbox")
        (root / "pkg").mkdir()

        assert openshell_checkout._escapes_tree(root, "pkg/module.py") is False


def _executable(path: Path) -> bool:
    return bool(path.lstat().st_mode & stat.S_IXUSR)


# ── the denial evidence ──────────────────────────────────────────────────────


class TestTheDenialEvents:
    def test_the_denial_line_is_kept_and_the_ordinary_ones_are_not(self) -> None:
        kept = denial_events(LOG_WITH_DENIAL)

        assert len(kept) == 1
        assert "egress denied" in kept[0]

    def test_a_credential_in_a_denial_line_is_redacted(self) -> None:
        line = "denied: Authorization: Bearer sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF"

        kept = denial_events(line)

        assert "DEADBEEF" not in kept[0]

    def test_the_denials_are_read_before_the_download(self, tmp_path: Path) -> None:
        """The sandbox holding the log is deleted moments later."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert gateway.names[:2] == ["logs", "sandbox_download"]
        assert len(realization.denials) == 1

    def test_an_unreadable_log_is_no_denials_rather_than_a_failure(
        self, tmp_path: Path
    ) -> None:
        """An absent observation, not a claim that nothing was denied."""
        gateway = FakeGateway(
            _checkout(tmp_path / "sandbox"),
            logs_error=OpenShellError(["openshell"], reason="gateway said no"),
        )

        realization = _realize(gateway, _checkout(tmp_path / "work"))

        assert realization.denials == ()
        assert realization.checkout is not None


# ── the handle: the work comes back before the sandbox goes ──────────────────


def _handle(gateway: FakeGateway, checkout: Path | None) -> OpenShellSandboxHandle:
    process = MagicMock()
    process.stdout = None
    process.pid = 4242
    exec_handle = MagicMock()
    exec_handle.process = process
    return OpenShellSandboxHandle(
        exec_handle,
        client=gateway,  # type: ignore[arg-type]
        name=SANDBOX,
        identity=SANDBOX_ID,
        startup_nonce="n0nce",
        host_checkout_dir=checkout,
    )


class TestTheHandleRealization:
    def test_the_cleanup_downloads_before_it_deletes(self, tmp_path: Path) -> None:
        """The delete takes the agent's work with it, so the order is the point."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.cleanup_identity()

        assert gateway.names == ["logs", "sandbox_download", "sandbox_delete"]
        assert handle.realization.checkout is not None

    def test_a_refused_realization_still_deletes_the_sandbox(
        self, tmp_path: Path
    ) -> None:
        """Cleanup runs on every path, including the one that publishes nothing."""
        gateway = FakeGateway(
            _checkout(tmp_path / "sandbox"),
            download_error=OpenShellError(["openshell"], reason="transfer failed"),
        )
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.cleanup_identity()

        assert gateway.names[-1] == "sandbox_delete"
        assert handle.realization.checkout is None
        assert handle.realization.refusal

    def test_an_unmodelled_download_failure_still_deletes(self, tmp_path: Path) -> None:
        """Releasing the sandbox is the obligation that cannot be skipped.

        A failure the realization does not model propagates — the run fails —
        but not before the sandbox is gone: an orphan would otherwise hold a
        provider on the operator's gateway forever.
        """
        gateway = FakeGateway(
            _checkout(tmp_path / "sandbox"),
            download_error=MemoryError("no host memory for the transfer"),
        )
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        with pytest.raises(MemoryError):
            handle.cleanup_identity()

        assert gateway.names[-1] == "sandbox_delete"

    def test_a_second_cleanup_does_not_download_again(self, tmp_path: Path) -> None:
        """Every path releases the sandbox, so cleanup can run more than once.

        A second download would land on the destination the first one filled,
        and be refused for it — replacing a good realization with a refusal.
        """
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.cleanup_identity()
        first = handle.realization
        handle.cleanup_identity()

        assert gateway.names.count("sandbox_download") == 1
        assert handle.realization is first

    def test_a_kill_deletes_without_waiting_for_a_download(
        self, tmp_path: Path
    ) -> None:
        """A cap breach ends the sandbox; it does not first pull a tree back.

        The breach is the host refusing this run, and the realization of a run
        that will not publish is a transfer nobody reads. The LOG is read
        anyway: it is the only account of what the gateway denied the agent,
        and the delete below is the end of the only thing that holds it.
        """
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.kill()

        assert gateway.names == ["logs", "sandbox_delete"]
        assert handle.realization is None

    def test_a_kill_reads_the_denial_log_before_the_sandbox_goes(
        self, tmp_path: Path
    ) -> None:
        """Every cancel and interrupt path kills FIRST, then cleans up.

        ``ContainerHandle.cancel_launch`` is the shape of it: kill, remove,
        reap, then ``cleanup_identity`` — so the realization that records the
        denials was reached with the sandbox already deleted, and the run kept
        no account of what the gateway refused the agent (PR #2451 review).
        The kill therefore reads the log, and the realization it reaches
        afterwards carries what it read.
        """
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.cancel_launch()

        # One log read, before the delete, and no second one afterwards: the
        # sandbox is gone by then and a retry would record nothing.
        assert gateway.names == ["logs", "sandbox_delete"]
        realization = handle.realization
        assert realization is not None
        assert len(realization.denials) == 1
        assert "egress denied" in realization.denials[0]
        # No tree came back — the sandbox was released before the download —
        # and the refusal says that rather than reporting a failed transfer.
        assert realization.checkout is None
        assert realization.refusal_code == REFUSAL_SANDBOX_RELEASED

    def test_the_denials_a_kill_read_are_not_read_a_second_time(
        self, tmp_path: Path
    ) -> None:
        """The realization inherits the kill's read instead of repeating it.

        A second ``openshell logs`` after the delete is a call against a
        sandbox that no longer exists; the evidence has to come from the read
        that happened while it did.
        """
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))

        handle.kill()
        handle.cleanup_identity()
        handle.cleanup_identity()

        assert gateway.names.count("logs") == 1

    def test_a_cost_stop_downloads_before_it_deletes(self, tmp_path: Path) -> None:
        """A cost stop is not a cap breach: the work is the agent's (#1462).

        The stopped run fails and keeps its work for the operator, so the
        governor brings the tree back before the delete that ends the sandbox.
        """
        from kew.dispatch_sandbox import ContainedCostGovernor

        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, _checkout(tmp_path / "work"))
        result = DispatchResult(exit_code=0, issue_num=1, log_file=tmp_path / "log")
        observe = ContainedCostGovernor(budget_check=lambda cost: cost > 1.0).watch(
            result, handle
        )
        assert observe is not None

        assert observe(2.0, 0) is True

        assert gateway.names == ["logs", "sandbox_download", "sandbox_delete"]
        assert handle.realization is not None
        assert handle.realization.checkout is not None
        assert result.cost_stop_kill == "accepted"

    def test_a_handle_with_no_host_checkout_realizes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The certification probe launches that way and publishes nothing."""
        gateway = FakeGateway(_checkout(tmp_path / "sandbox"))
        handle = _handle(gateway, None)

        handle.cleanup_identity()

        assert gateway.names == ["sandbox_delete"]
        assert handle.realization is None


# ── the retry ladder, which re-enters one host checkout ──────────────────────


class TestASecondAttemptOnTheSameCheckout:
    """The retry loop threads ONE ``worktree_dir`` through every attempt.

    So the destination the realization derives from the checkout is the same
    directory on every attempt, and the tree the previous attempt realized is
    still sitting in it. The launch retires that tree
    (:func:`openshell_checkout.retire_realized_checkout`); without it every
    attempt after the first was refused as destination-occupied and its work
    was deleted with its sandbox having never been downloaded (PR #2451
    review).
    """

    def test_the_second_attempts_work_is_realized_not_refused(
        self, tmp_path: Path
    ) -> None:
        uploaded = _checkout(tmp_path / "work")
        first_sandbox = _checkout(tmp_path / "sandbox-1", files={"first.txt": "1\n"})
        first = _realize(FakeGateway(first_sandbox), uploaded)
        assert first.refusal == "", first.refusal

        # What the next attempt's launch does before it creates a sandbox.
        openshell_checkout.retire_realized_checkout(uploaded)
        second_sandbox = _checkout(tmp_path / "sandbox-2", files={"second.txt": "2\n"})
        second = _realize(FakeGateway(second_sandbox), uploaded)

        assert second.refusal == "" and second.refusal_code == ""
        realized = second.checkout.path
        assert (realized / "second.txt").is_file()
        # And it is this attempt's tree, not a merge over the last one's: a
        # download onto an existing checkout is what the refusal exists for.
        assert not (realized / "first.txt").exists()

    def test_retiring_nothing_is_not_an_error(self, tmp_path: Path) -> None:
        """The first attempt, and every run on every other runtime."""
        uploaded = _checkout(tmp_path / "work")

        openshell_checkout.retire_realized_checkout(uploaded)

        assert not realized_checkout_dir(uploaded).exists()

    def test_the_uploaded_checkout_is_never_what_gets_retired(
        self, tmp_path: Path
    ) -> None:
        """The retirement is the SIBLING's, and the launch is about to upload
        the original — which still has to be there when it does."""
        uploaded = _checkout(tmp_path / "work")
        realized_checkout_dir(uploaded).mkdir()

        openshell_checkout.retire_realized_checkout(uploaded)

        assert (uploaded / "README.md").is_file()
        assert not realized_checkout_dir(uploaded).exists()

    def test_a_tree_that_cannot_be_removed_leaves_the_refusal_to_fire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft: a launch does not refuse to start over this.

        The realization then meets the destination it always did and refuses
        it, which is a named refusal rather than a dispatch that never ran.
        """
        uploaded = _checkout(tmp_path / "work")
        occupied = realized_checkout_dir(uploaded)
        occupied.mkdir()
        (occupied / "left-over.txt").write_text("from the last attempt\n")
        monkeypatch.setattr(
            openshell_checkout.shutil,
            "rmtree",
            _raising_rmtree(PermissionError("read-only parent")),
        )

        openshell_checkout.retire_realized_checkout(uploaded)

        assert (occupied / "left-over.txt").is_file()
        realization = _realize(FakeGateway(_checkout(tmp_path / "sandbox")), uploaded)
        assert realization.refusal_code == REFUSAL_DESTINATION_OCCUPIED


def _raising_rmtree(error: BaseException) -> Any:
    def rmtree(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return rmtree


# ── the publication gate ─────────────────────────────────────────────────────


def _openshell_config() -> DispatchConfig:
    config = DispatchConfig()
    config.sandbox.runtime = "openshell"
    return config


def _result(tmp_path: Path) -> DispatchResult:
    return DispatchResult(exit_code=0, issue_num=2322, log_file=tmp_path / "run.log")


class TestThePublicationGate:
    def test_the_realized_checkout_replaces_the_uploaded_one(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _publication_checkout

        result = _result(tmp_path)
        result.realized_checkout = tmp_path / "work-realized"
        result.realized_checkout_generation = "gen-2"
        finalize = MagicMock()

        resolved = _publication_checkout(
            _openshell_config(),
            result,
            finalize,
            checkout_dir=tmp_path / "work",
            checkout_generation="gen-1",
        )

        assert resolved == (tmp_path / "work-realized", "gen-2")
        finalize.assert_not_called()

    def test_a_refused_realization_refuses_publication(self, tmp_path: Path) -> None:
        """The cap breach's consequence: no push, and a failure row that says why."""
        from kew.dispatch_sandbox import (
            _REALIZED_CHECKOUT_CATEGORY,
            _publication_checkout,
        )

        result = _result(tmp_path)
        result.realized_checkout_refusal = "breaches the checkout caps"
        finalize = MagicMock()

        resolved = _publication_checkout(
            _openshell_config(),
            result,
            finalize,
            checkout_dir=tmp_path / "work",
            checkout_generation="gen-1",
        )

        assert resolved is None
        assert result.exit_code == 1
        assert result.error_category == _REALIZED_CHECKOUT_CATEGORY
        finalize.assert_called_once_with(
            1,
            _REALIZED_CHECKOUT_CATEGORY,
            status="failure",
            reason="breaches the checkout caps",
        )

    def test_a_missing_realization_never_publishes_the_uploaded_tree(
        self, tmp_path: Path
    ) -> None:
        """That tree is the launch snapshot: it holds none of the agent's work."""
        from kew.dispatch_sandbox import _publication_checkout

        finalize = MagicMock()

        resolved = _publication_checkout(
            _openshell_config(),
            _result(tmp_path),
            finalize,
            checkout_dir=tmp_path / "work",
            checkout_generation="gen-1",
        )

        assert resolved is None
        assert "never downloaded" in finalize.call_args.kwargs["reason"]

    def test_every_other_runtime_publishes_the_checkout_it_mounted(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _publication_checkout

        config = DispatchConfig()
        config.sandbox.runtime = "docker"

        resolved = _publication_checkout(
            config,
            _result(tmp_path),
            MagicMock(),
            checkout_dir=tmp_path / "work",
            checkout_generation="gen-1",
        )

        assert resolved == (tmp_path / "work", "gen-1")


def _success_context(tmp_path: Path) -> SimpleNamespace:
    """The dispatch context ``_collect_success`` reads, and nothing else."""
    return SimpleNamespace(
        config=_openshell_config(),
        issue_num=2322,
        item_id="2322",
        issue_title="Bring the agent's work back off the gateway",
        branch="agent/issue-2322",
        worktree_dir=None,
        publication_style="agent",
        publication_base_sha="a" * 40,
        store=None,
        run_id="run-2322",
        notifier=None,
    )


class TestTheReportedChangedFiles:
    """What a finished dispatch tells its caller it changed.

    Publication swaps to the realized tree (:class:`TestThePublicationGate`),
    so the outcome has to follow it. Read off the uploaded checkout instead, a
    successful OpenShell dispatch reported an EMPTY changed-file set to the
    overlap coordinator that consumes ``DispatchOutcome.changed_files``: that
    tree is the launch snapshot and holds none of the agent's edits
    (PR #2451 review).
    """

    def test_a_successful_openshell_dispatch_reports_the_realized_tree(
        self, tmp_path: Path
    ) -> None:
        from kew import dispatch_core

        result = _result(tmp_path)
        result.realized_checkout = tmp_path / "work-realized"
        result.realized_checkout_generation = "gen-2"

        with (
            patch.object(dispatch_core, "_handle_success_pr", return_value=None),
            patch.object(
                dispatch_core, "_get_changed_files", return_value=["widget.py"]
            ) as changed,
            patch.object(dispatch_core, "_cleanup_dispatch_dir"),
            patch.object(dispatch_core, "_notify_dispatch_success"),
        ):
            outcome = dispatch_core._collect_success(
                _success_context(tmp_path), result, tmp_path / "work", True
            )

        assert changed.call_args.args[0] == tmp_path / "work-realized"
        assert outcome.changed_files == ["widget.py"]

    def test_every_other_runtime_reports_the_checkout_the_agent_wrote(
        self, tmp_path: Path
    ) -> None:
        """No realization, no swap: the dispatch's own tree IS the work."""
        from kew import dispatch_core

        with (
            patch.object(dispatch_core, "_handle_success_pr", return_value=None),
            patch.object(
                dispatch_core, "_get_changed_files", return_value=[]
            ) as changed,
            patch.object(dispatch_core, "_cleanup_dispatch_dir"),
            patch.object(dispatch_core, "_notify_dispatch_success"),
        ):
            dispatch_core._collect_success(
                _success_context(tmp_path), _result(tmp_path), tmp_path / "work", True
            )

        assert changed.call_args.args[0] == tmp_path / "work"


class TestTheCapturedEvidence:
    def test_the_denials_and_the_sizes_land_beside_the_run_log(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _capture_realized_checkout

        realized = _checkout(tmp_path / "work-realized")
        handle = MagicMock()
        handle.realization = CheckoutRealization(
            checkout=RealizedCheckout(
                path=realized,
                used_bytes=17,
                entries=3,
                modes_preserved=False,
                mode_repairs=("script.sh",),
            ),
            refusal="",
            refusal_code="",
            denials=("egress denied: 10.0.0.9:443",),
        )
        result = _result(tmp_path)

        _capture_realized_checkout(handle, result)

        event = _realization_event(result.log_file)
        assert event["denials"] == ["egress denied: 10.0.0.9:443"]
        assert event["bytes"] == 17
        assert event["mode_repairs"] == ["script.sh"]
        assert event["modes_preserved"] is False
        assert result.realized_checkout == realized
        # The realized tree carries its own generation: the original's binds
        # the original's inode, so it could never speak for this one.
        assert result.realized_checkout_generation

    def test_a_refusal_lands_in_the_evidence_with_no_checkout(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _capture_realized_checkout

        handle = MagicMock()
        handle.realization = CheckoutRealization(
            checkout=None,
            refusal="breaches the checkout caps",
            refusal_code=REFUSAL_OVER_QUOTA,
            denials=(),
        )
        result = _result(tmp_path)

        _capture_realized_checkout(handle, result)

        event = _realization_event(result.log_file)
        assert event["refusal_code"] == REFUSAL_OVER_QUOTA
        assert event["path"] is None
        assert result.realized_checkout is None
        assert result.realized_checkout_refusal == "breaches the checkout caps"

    def test_a_handle_from_another_runtime_writes_nothing(self, tmp_path: Path) -> None:
        """A bare ``MagicMock`` is every other suite's launch double.

        It answers ``realization`` with another mock, so this seam has to ask
        for the TYPE: a name check alone reads a realization off every docker
        handle a test ever built and then writes it out as evidence.
        """
        from kew.dispatch_sandbox import _capture_realized_checkout

        handle = MagicMock()
        result = _result(tmp_path)

        _capture_realized_checkout(handle, result)

        assert not result.log_file.with_suffix(".events.jsonl").exists()
        assert result.realized_checkout is None


def _realization_event(log_file: Path) -> dict[str, Any]:
    events = [
        json.loads(line)
        for line in log_file.with_suffix(".events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    realized = [
        event for event in events if event["type"] == "openshell_realized_checkout"
    ]
    assert len(realized) == 1
    return realized[0]


def test_the_realized_directory_is_a_sibling_of_the_checkout(tmp_path: Path) -> None:
    """Derived, not remembered: the dispatch that publishes it and the cleanup
    that removes it both name it from the checkout path alone."""
    checkout = tmp_path / "kew-sandbox-2322"

    realized = realized_checkout_dir(checkout)

    assert realized.parent == checkout.resolve().parent
    assert realized.name == "kew-sandbox-2322-realized"


def test_the_dispatch_cleanup_removes_the_realized_tree_too(tmp_path: Path) -> None:
    """Both trees are kew's; neither may outlive the dispatch that made them.

    The realized one is reached from the original's path, so the cleanup needs
    nothing remembered — and on every other runtime there is no sibling there
    to remove.
    """
    from kew.dispatch_core import _cleanup_dispatch_dir

    checkout = _checkout(tmp_path / "kew-sandbox-2322")
    realized = _checkout(realized_checkout_dir(checkout))

    _cleanup_dispatch_dir(checkout, sandboxed=True, branch="agent/issue-2322")

    assert not checkout.exists()
    assert not realized.exists()


def test_the_source_path_is_where_the_launch_uploads(tmp_path: Path) -> None:
    """The download reads back the path the upload wrote, not a second guess."""
    gateway = FakeGateway(_checkout(tmp_path / "sandbox"))

    _realize(gateway, _checkout(tmp_path / "work"))

    download = next(call for call in gateway.calls if call.name == "sandbox_download")
    assert download.detail["source"] == CHECKOUT_PATH


@pytest.mark.parametrize(
    "line",
    [
        "egress DENIED to api.example",
        "policy: request blocked",
        "connection refused by gateway rule",
        "403 forbidden by policy",
        "that destination is not allowed",
    ],
)
def test_every_denial_spelling_the_markers_cover(line: str) -> None:
    """The markers are a conservative superset, and that is deliberate: the
    sandbox holding the log is deleted as soon as it is read."""
    assert denial_events(line) == (line,)


class TestTheSubtreeRealization:
    """One directory out of a sandbox, under the same caps as a checkout.

    The review-environment provisioner (epic node N12) runs the reviewed PR's
    locked install in its OWN sandbox and has to bring the built ``.venv`` back
    to the host, where ``_promote`` moves it into the reviewed checkout. That
    tree is deliberately NOT a git checkout, so the two checks a published tree
    gets — the ``.git`` requirement and the index-mode comparison — would refuse
    every provisioning download for a property it is never going to have. Every
    check that bounds the TRANSFER is shared.

    BOTH of the CLI's copy semantics are exercised, because the realization
    supports both: ``_subtree`` prepares the one that nests the source
    directory under the destination, ``_subtree_contents`` the one that writes
    its contents straight into it.
    """

    MARKER = "pyvenv.cfg"

    def _built(self, root: Path) -> Path:
        """An environment in the shape ``uv sync`` leaves one."""
        (root / "bin").mkdir(parents=True)
        (root / self.MARKER).write_text("home = /usr\n", encoding="utf-8")
        return root

    def _subtree(self, tmp_path: Path, **kwargs: Any) -> tuple[FakeGateway, Path]:
        """A gateway whose download NESTS ``.venv`` under the destination."""
        self._built(tmp_path / "sandbox" / ".venv")
        gateway = FakeGateway(tmp_path / "sandbox", **kwargs)
        return gateway, tmp_path / "landing"

    def _subtree_contents(
        self, tmp_path: Path, **kwargs: Any
    ) -> tuple[FakeGateway, Path]:
        """A gateway whose download writes the CONTENTS into the destination."""
        built = self._built(tmp_path / "sandbox" / ".venv")
        gateway = FakeGateway(built, **kwargs)
        return gateway, tmp_path / "landing"

    def _realize(self, gateway: FakeGateway, destination: Path, **kwargs: Any) -> Path:
        from kew.openshell_checkout import realize_sandbox_subtree

        return realize_sandbox_subtree(
            gateway,  # type: ignore[arg-type]
            "kew-review-env-1",
            source="/work/kew/.venv",
            destination=destination,
            marker=self.MARKER,
            **kwargs,
        )

    def test_the_built_environment_lands_at_the_sources_own_name(
        self, tmp_path: Path
    ) -> None:
        gateway, destination = self._subtree(tmp_path)

        root = self._realize(gateway, destination)

        assert root == destination / ".venv"
        assert (root / self.MARKER).read_text(encoding="utf-8") == "home = /usr\n"
        download = next(
            call for call in gateway.calls if call.name == "sandbox_download"
        )
        assert download.detail["source"] == "/work/kew/.venv"

    def test_a_download_of_the_contents_alone_is_realized_not_refused(
        self, tmp_path: Path
    ) -> None:
        """The CLI's OTHER copy semantic, which the checkout path already took.

        ``openshell sandbox download`` may write the source's contents into the
        destination instead of nesting the source directory under it, and
        ``_download_root`` has accepted either since node N7. A subtree
        realization that assumed the nesting refused EVERY provisioning
        download as ``realized_subtree_absent`` under the other one — on a
        gateway build no test double exercised (PR #2520 review).
        """
        gateway, destination = self._subtree_contents(tmp_path)

        root = self._realize(gateway, destination)

        assert root == destination
        assert (root / self.MARKER).read_text(encoding="utf-8") == "home = /usr\n"

    def test_an_occupied_destination_is_refused_rather_than_merged(
        self, tmp_path: Path
    ) -> None:
        """The same refusal a checkout download gets, for the same reason."""
        from kew.openshell_checkout import (
            REFUSAL_DESTINATION_OCCUPIED,
            RealizedCheckoutRefused,
        )

        gateway, destination = self._subtree(tmp_path)
        (destination / ".venv").mkdir(parents=True)

        with pytest.raises(RealizedCheckoutRefused) as raised:
            self._realize(gateway, destination)

        assert raised.value.code == REFUSAL_DESTINATION_OCCUPIED

    def test_a_download_that_brought_nothing_back_is_refused_not_returned(
        self, tmp_path: Path
    ) -> None:
        """A transfer the gateway called fine with no tree at the other end.

        Returning the path anyway would have the caller move nothing into the
        reviewed checkout and report a provisioned environment.
        """
        from kew.openshell_checkout import (
            REFUSAL_SUBTREE_ABSENT,
            RealizedCheckoutRefused,
        )

        gateway = FakeGateway(tmp_path / "empty")
        (tmp_path / "empty").mkdir()

        with pytest.raises(RealizedCheckoutRefused) as raised:
            self._realize(gateway, tmp_path / "landing")

        assert raised.value.code == REFUSAL_SUBTREE_ABSENT
        assert self.MARKER in str(raised.value)

    def test_a_tree_without_the_marker_is_refused_under_either_semantic(
        self, tmp_path: Path
    ) -> None:
        """The marker is the proof, so a tree that is not an environment refuses.

        A download that brought SOMETHING back — the sandbox's own ``.venv``
        emptied by the PR's build backend, say — must not be promoted into the
        reviewed checkout as the installed environment it is not.
        """
        from kew.openshell_checkout import (
            REFUSAL_SUBTREE_ABSENT,
            RealizedCheckoutRefused,
        )

        (tmp_path / "sandbox" / ".venv" / "bin").mkdir(parents=True)
        gateway = FakeGateway(tmp_path / "sandbox")

        with pytest.raises(RealizedCheckoutRefused) as raised:
            self._realize(gateway, tmp_path / "landing")

        assert raised.value.code == REFUSAL_SUBTREE_ABSENT

    def test_a_subtree_over_the_entry_cap_is_refused(self, tmp_path: Path) -> None:
        """The checkout caps bound it: a venv is not a licence to fill the disk."""
        from kew.openshell_checkout import (
            REFUSAL_OVER_QUOTA,
            RealizedCheckoutRefused,
        )

        gateway, destination = self._subtree(tmp_path)

        with pytest.raises(RealizedCheckoutRefused) as raised:
            self._realize(gateway, destination, quota_entries=1)

        assert raised.value.code == REFUSAL_OVER_QUOTA

    def test_the_host_room_probe_runs_before_the_first_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host with no room refuses without starting an unstoppable transfer."""
        from kew.openshell_checkout import (
            REFUSAL_NO_HOST_ROOM,
            RealizedCheckoutRefused,
        )

        gateway, destination = self._subtree(tmp_path)
        _fake_free_space(monkeypatch, 16 * 1024**2)

        with pytest.raises(RealizedCheckoutRefused) as raised:
            self._realize(gateway, destination)

        assert raised.value.code == REFUSAL_NO_HOST_ROOM
        assert "sandbox_download" not in gateway.names
