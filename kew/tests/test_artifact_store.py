"""I07 node 1: the work package becomes durable, host-owned evidence.

The package the container produced lives in a directory the agent can write.
Everything downstream — the tree, the receipt, the audit prefix — is a claim
about *those bytes*, so the bytes have to stop being agent-reachable before any
of it means anything. This module is that move: read the artifact once, verify
it, and place a canonical copy under a root only the host owns.

Two properties carry the weight here and both are tested by construction rather
than by assertion about what we think happened:

* **The name is the content.** A package is addressed by a digest over its own
  manifest, so "the same work" and "the same directory" are the same statement.
  Producer ordering and producer timestamps are not in the preimage, which is
  what makes a re-run of the same workspace recoverable rather than merely
  similar.
* **A read re-proves the write.** Loading a package re-hashes every blob and
  re-derives the package digest, so evidence that was edited after it was
  sealed is refused instead of returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.artifact_store import (
    PackageStore,
    PackageStoreError,
    PackageTampered,
    PackageUnavailable,
    default_root,
)
from kew.publication_artifact import (
    BLOBS_DIR,
    MANIFEST_NAME,
    WORKSPACE_SCOPE,
    Artifact,
    package_digest,
    read_artifact,
)

_BASE = "a" * 40


def _artifact(
    where: Path,
    files: dict[str, bytes] | None = None,
    *,
    base: str = _BASE,
    deletions: tuple[str, ...] = (),
    scope: str | None = WORKSPACE_SCOPE,
    order: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> Artifact:
    """Write a well-formed producer artifact at *where* and read it back."""
    blobs = where / BLOBS_DIR
    blobs.mkdir(parents=True)
    files = {"src/main.py": b"print(1)\n"} if files is None else files
    entries = []
    for path in order or list(files):
        payload = files[path]
        digest = hashlib.sha256(payload).hexdigest()
        (blobs / digest).write_bytes(payload)
        entries.append({"path": path, "mode": "100644", "sha256": digest})
    document: dict[str, object] = {
        "base": base,
        "entries": entries,
        "deletions": list(deletions),
    }
    if scope is not None:
        document["scope"] = scope
    document.update(extra or {})
    (where / MANIFEST_NAME).write_text(json.dumps(document), encoding="utf-8")
    return read_artifact(where)


def _store(tmp_path: Path) -> PackageStore:
    return PackageStore(tmp_path / "store")


def _mode(path: Path) -> int:
    return stat_module.S_IMODE(path.lstat().st_mode)


class TestDurableOwnerOnlyPlacement:
    def test_a_preserved_package_is_owner_only_all_the_way_down(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced"))

        assert _mode(store.root) == 0o700
        assert _mode(preserved.root) == 0o700
        assert _mode(preserved.root / BLOBS_DIR) == 0o700
        blobs = list((preserved.root / BLOBS_DIR).iterdir())
        assert blobs and all(_mode(blob) == 0o400 for blob in blobs)
        assert _mode(preserved.root / MANIFEST_NAME) == 0o400

    def test_the_placement_is_under_the_hosts_private_root(self, tmp_path):
        root = default_root(tmp_path)
        assert root.parts[-3:] == (".kew", "private", "packages")

    def test_a_symlinked_store_root_is_refused_outright(self, tmp_path):
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "store").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(PackageStoreError):
            PackageStore(tmp_path / "store")

    def test_the_package_is_named_by_its_content_not_by_the_agent(self, tmp_path):
        store = _store(tmp_path)
        artifact = _artifact(tmp_path / "produced", {"../../escape": b"x"})
        preserved = store.preserve(artifact)

        assert preserved.root.parent == store.root / "packages"
        assert preserved.root.name == package_digest(artifact).removeprefix("sha256:")
        assert preserved.root.name.isalnum()


class TestTheSameWorkIsTheSamePackage:
    def test_preserving_the_same_package_twice_places_it_once(self, tmp_path):
        store = _store(tmp_path)
        first = store.preserve(_artifact(tmp_path / "one"))
        second = store.preserve(_artifact(tmp_path / "two"))

        assert first.digest == second.digest
        assert first.root == second.root
        assert len(list((store.root / "packages").iterdir())) == 1

    def test_producer_ordering_and_timestamps_are_not_the_package(self, tmp_path):
        files = {"a.py": b"1\n", "b.py": b"2\n"}
        forwards = _artifact(tmp_path / "fw", files, order=["a.py", "b.py"])
        backwards = _artifact(tmp_path / "bw", files, order=["b.py", "a.py"])
        os.utime(tmp_path / "bw" / MANIFEST_NAME, (0, 0))

        assert package_digest(forwards) == package_digest(backwards)

    def test_two_paths_sharing_one_blob_are_stored_once_and_counted_twice(
        self, tmp_path
    ):
        store = _store(tmp_path)
        preserved = store.preserve(
            _artifact(tmp_path / "produced", {"a.py": b"same\n", "b.py": b"same\n"})
        )

        assert len(list((preserved.root / BLOBS_DIR).iterdir())) == 1
        assert preserved.entry_count == 2
        assert preserved.byte_length == 2 * len(b"same\n")


class TestTheStoredManifestIsTheHostsOwn:
    def test_the_container_manifests_extra_fields_never_land(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(
            _artifact(tmp_path / "produced", extra={"note": "trust me"})
        )
        stored = json.loads((preserved.root / MANIFEST_NAME).read_text())

        assert set(stored) == {"base", "entries", "deletions", "scope"}

    def test_a_package_recovers_as_exactly_the_same_work(self, tmp_path):
        store = _store(tmp_path)
        artifact = _artifact(
            tmp_path / "produced",
            {"src/a.py": b"one\n", "src/b.py": b"two\n"},
            deletions=("gone.py",),
        )
        preserved = store.preserve(artifact)

        recovered = PackageStore(store.root).load(preserved.digest)
        assert recovered.base_sha == artifact.base_sha
        assert recovered.scope == artifact.scope
        assert recovered.deletions == artifact.deletions
        assert {(e.path, e.mode, e.sha256) for e in recovered.entries} == {
            (e.path, e.mode, e.sha256) for e in artifact.entries
        }
        assert package_digest(recovered) == preserved.digest


class TestSealedBytesCannotBeEditedInPlace:
    def test_a_rewritten_blob_is_refused_rather_than_returned(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced"))
        blob = next((preserved.root / BLOBS_DIR).iterdir())
        blob.chmod(0o600)
        blob.write_bytes(b"print(2)\n")

        with pytest.raises(PackageTampered):
            store.load(preserved.digest)

    def test_a_rewritten_manifest_is_refused_rather_than_returned(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced"))
        manifest = preserved.root / MANIFEST_NAME
        document = json.loads(manifest.read_text())
        document["entries"][0]["path"] = "somewhere/else.py"
        manifest.chmod(0o600)
        manifest.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(PackageTampered):
            store.load(preserved.digest)


class TestCrashAndRestart:
    def test_a_crash_before_placement_leaves_no_half_package(self, tmp_path):
        store = _store(tmp_path)
        artifact = _artifact(tmp_path / "produced")
        with (
            patch("kew.artifact_store.os.rename", side_effect=OSError("power cut")),
            pytest.raises(PackageStoreError),
        ):
            store.preserve(artifact)

        assert not (store.root / "packages").exists() or not list(
            (store.root / "packages").iterdir()
        )
        with pytest.raises(PackageUnavailable):
            store.load(package_digest(artifact))

    def test_the_retry_after_a_crash_recovers_the_same_package(self, tmp_path):
        store = _store(tmp_path)
        artifact = _artifact(tmp_path / "produced")
        with (
            patch("kew.artifact_store.os.rename", side_effect=OSError("power cut")),
            pytest.raises(PackageStoreError),
        ):
            store.preserve(artifact)

        preserved = PackageStore(store.root).preserve(artifact)
        assert preserved.digest == package_digest(artifact)
        assert not list((store.root / ".staging").iterdir())

    def test_a_crash_after_placement_is_a_complete_package(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced"))

        restarted = PackageStore(store.root)
        assert restarted.describe(preserved.digest) == preserved


class TestReferencesAreNeverPaths:
    @pytest.mark.parametrize(
        "reference",
        [
            "../../etc/passwd",
            "sha256:../../etc",
            "sha256:" + "a" * 63,
            "sha256:" + "A" * 64,
            "",
        ],
    )
    def test_a_reference_that_is_not_a_digest_never_reaches_the_filesystem(
        self, tmp_path, reference
    ):
        store = _store(tmp_path)
        with pytest.raises(PackageStoreError):
            store.load(reference)

    def test_an_absent_package_is_unavailable_not_empty(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(PackageUnavailable):
            store.load("sha256:" + "b" * 64)


class TestRetention:
    def test_purging_removes_the_bytes_and_says_whether_it_did(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced"))

        assert store.purge(preserved.digest) is True
        assert not preserved.root.exists()
        assert store.purge(preserved.digest) is False
        with pytest.raises(PackageUnavailable):
            store.load(preserved.digest)

    def test_purge_takes_a_digest_and_never_a_path(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(PackageStoreError):
            store.purge("../packages")


class TestAnEmptyPackageStillProvesItself:
    def test_an_empty_declared_package_is_preserved_and_recoverable(self, tmp_path):
        store = _store(tmp_path)
        preserved = store.preserve(_artifact(tmp_path / "produced", {}))

        assert preserved.entry_count == 0
        assert preserved.byte_length == 0
        assert store.load(preserved.digest).scope == WORKSPACE_SCOPE

    def test_an_empty_package_is_not_the_same_package_as_work(self, tmp_path):
        empty = _artifact(tmp_path / "empty", {})
        work = _artifact(tmp_path / "work")
        assert package_digest(empty) != package_digest(work)
