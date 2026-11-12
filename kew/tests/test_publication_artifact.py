"""W1.1 node 4b: reading the container's artifact as opaque bytes (D3).

The artifact is produced INSIDE the container, into a directory the agent can
write. So these tests treat the artifact itself as hostile input — not only its
manifest, but its filenames, its blob directory, and the file types it uses.

No callers yet, by construction — node 5 shadow-compares before anything flips.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.publication_artifact import (
    BLOBS_DIR,
    MANIFEST_NAME,
    WORKSPACE_SCOPE,
    ArtifactRejected,
    package_digest,
    read_artifact,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _build(
    tmp_path: Path,
    files: dict[str, bytes] | None = None,
    *,
    base: str = "a" * 40,
    manifest: object | None = None,
    modes: dict[str, str] | None = None,
) -> Path:
    """Write a well-formed artifact, which individual tests then damage."""
    artifact = tmp_path / "artifact"
    blobs = artifact / BLOBS_DIR
    blobs.mkdir(parents=True)
    files = files if files is not None else {"src/main.py": b"print(1)\n"}
    modes = modes or {}

    entries = []
    for path, payload in files.items():
        digest = _digest(payload)
        (blobs / digest).write_bytes(payload)
        entries.append(
            {"path": path, "mode": modes.get(path, "100644"), "sha256": digest}
        )

    document = manifest if manifest is not None else {"base": base, "entries": entries}
    (artifact / MANIFEST_NAME).write_text(json.dumps(document), encoding="utf-8")
    return artifact


class TestAWellFormedArtifact:
    def test_it_reads(self, tmp_path: Path):
        artifact = read_artifact(_build(tmp_path))

        assert artifact.base_sha == "a" * 40
        assert [e.path for e in artifact.entries] == ["src/main.py"]
        assert artifact.content(artifact.entries[0]) == b"print(1)\n"

    def test_content_is_reachable_by_path_for_the_filter(self, tmp_path: Path):
        """`text_for` plugs into filter_manifest(read_text=...)."""
        artifact = read_artifact(
            _build(tmp_path, {".gitattributes": b"*.py filter=evil\n"})
        )

        assert artifact.text_for(".gitattributes") == "*.py filter=evil\n"

    def test_a_path_the_artifact_does_not_carry_is_an_error(self, tmp_path: Path):
        artifact = read_artifact(_build(tmp_path))

        with pytest.raises(ArtifactRejected, match="no artifact entry"):
            artifact.text_for("nope.py")


class TestTheHostRehashesEverything:
    """The manifest's sha256 is a CLAIM, written by the container."""

    def test_content_that_does_not_match_its_digest_is_refused(self, tmp_path: Path):
        artifact = _build(tmp_path)
        blob = next((artifact / BLOBS_DIR).iterdir())
        blob.write_bytes(b"something else entirely")

        with pytest.raises(ArtifactRejected, match="hashes to"):
            read_artifact(artifact)

    def test_a_missing_blob_is_refused(self, tmp_path: Path):
        artifact = _build(tmp_path)
        next((artifact / BLOBS_DIR).iterdir()).unlink()

        with pytest.raises(ArtifactRejected, match="could not be opened"):
            read_artifact(artifact)

    def test_an_unreferenced_blob_is_refused_not_ignored(self, tmp_path: Path):
        """ "Ignore what we did not expect" is how a second payload rides along."""
        artifact = _build(tmp_path)
        payload = b"stowaway"
        (artifact / BLOBS_DIR / _digest(payload)).write_bytes(payload)

        with pytest.raises(ArtifactRejected, match="not referenced"):
            read_artifact(artifact)


class TestTheArtifactDirectoryIsHostileToo:
    """It is written inside the container, so every name in it is agent-chosen."""

    def test_a_symlinked_manifest_is_not_followed(self, tmp_path: Path):
        secret = tmp_path / "operator-secret"
        secret.write_text('{"base": "' + "a" * 40 + '", "entries": []}')
        artifact = _build(tmp_path)
        (artifact / MANIFEST_NAME).unlink()
        (artifact / MANIFEST_NAME).symlink_to(secret)

        with pytest.raises(ArtifactRejected, match="could not be opened"):
            read_artifact(artifact)

    def test_a_symlinked_blob_is_not_followed(self, tmp_path: Path):
        """Otherwise the host hashes, and later publishes, a file off-artifact."""
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"PRIVATE KEY")
        artifact = _build(tmp_path)
        blob = next((artifact / BLOBS_DIR).iterdir())
        blob.unlink()
        blob.symlink_to(secret)

        with pytest.raises(ArtifactRejected, match="could not be opened"):
            read_artifact(artifact)

    def test_a_symlinked_blobs_directory_is_not_followed(self, tmp_path: Path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        artifact = _build(tmp_path)
        blobs = artifact / BLOBS_DIR
        for child in blobs.iterdir():
            child.unlink()
        blobs.rmdir()
        blobs.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(ArtifactRejected, match="could not be opened"):
            read_artifact(artifact)

    def test_a_blob_name_that_is_not_a_digest_is_refused(self, tmp_path: Path):
        """The name shape IS the defence — a traversal cannot be spelled in hex."""
        artifact = _build(tmp_path)
        (artifact / BLOBS_DIR / "..evil").write_bytes(b"x")

        with pytest.raises(ArtifactRejected, match="not referenced"):
            read_artifact(artifact)

    def test_a_manifest_digest_that_is_not_hex_never_reaches_open(self, tmp_path: Path):
        """A path in the sha256 field must be refused as a FIELD, not opened."""
        artifact = _build(
            tmp_path,
            manifest={
                "base": "a" * 40,
                "entries": [
                    {"path": "x", "mode": "100644", "sha256": "../../etc/passwd"}
                ],
            },
        )

        with pytest.raises(ArtifactRejected, match="sha256"):
            read_artifact(artifact)


class TestABlobMustBeABoundedRegularFile:
    """Adversarial review, round 2 on this module."""

    def test_a_fifo_blob_is_refused_rather_than_hanging_the_host(self, tmp_path: Path):
        """Opening a FIFO for reading BLOCKS until a writer appears.

        Without O_NONBLOCK this test would not fail — it would never finish,
        which is the failure mode. O_NOFOLLOW does not help: a FIFO is not a
        symlink.
        """
        artifact = _build(tmp_path)
        blob = next((artifact / BLOBS_DIR).iterdir())
        blob.unlink()
        os.mkfifo(blob)

        with pytest.raises(ArtifactRejected, match="not a regular file"):
            read_artifact(artifact)

    def test_a_directory_blob_is_refused_rather_than_crashing(self, tmp_path: Path):
        """A directory opens fine and raises IsADirectoryError at READ time —
        a crash where a refusal belongs."""
        artifact = _build(tmp_path)
        blob = next((artifact / BLOBS_DIR).iterdir())
        blob.unlink()
        blob.mkdir()

        with pytest.raises(ArtifactRejected, match="not a regular file"):
            read_artifact(artifact)

    def test_an_oversized_blob_is_refused(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kew.publication_artifact.MAX_BLOB_BYTES", 4)
        artifact = _build(tmp_path, {"big.txt": b"more than four bytes"})

        with pytest.raises(ArtifactRejected, match="limit"):
            read_artifact(artifact)

    def test_many_small_blobs_are_bounded_in_aggregate(
        self, tmp_path: Path, monkeypatch
    ):
        """A per-blob bound alone waves through a thousand small ones."""
        monkeypatch.setattr("kew.publication_artifact.MAX_TOTAL_BYTES", 10)
        artifact = _build(
            tmp_path, {f"f{i}.txt": f"payload-{i}".encode() for i in range(5)}
        )

        with pytest.raises(ArtifactRejected, match="expands to"):
            read_artifact(artifact)


class TestContentIsVerifiedWhenItIsREAD:
    """Validated-once-then-reopened leaves a window; the blobs are agent-writable."""

    def test_a_blob_swapped_after_verification_is_caught_on_read(self, tmp_path: Path):
        artifact_dir = _build(tmp_path)
        artifact = read_artifact(artifact_dir)

        # Whatever could swap it mid-flight, simulated directly.
        blob = next((artifact_dir / BLOBS_DIR).iterdir())
        blob.write_bytes(b"swapped after the check passed")

        with pytest.raises(ArtifactRejected, match="now hashes to"):
            artifact.content(artifact.entries[0])

    def test_the_same_protection_covers_text_for(self, tmp_path: Path):
        artifact_dir = _build(tmp_path, {".gitattributes": b"*.png binary\n"})
        artifact = read_artifact(artifact_dir)
        next((artifact_dir / BLOBS_DIR).iterdir()).write_bytes(b"*.py filter=evil\n")

        with pytest.raises(ArtifactRejected, match="now hashes to"):
            artifact.text_for(".gitattributes")


class TestTheManifestIsParsedStrictly:
    """A JSON document is not a schema."""

    @pytest.mark.parametrize(
        ("document", "fragment"),
        [
            ([], "not an object"),
            ({"entries": []}, "base"),
            ({"base": "nope", "entries": []}, "base"),
            ({"base": "a" * 40}, "entries"),
            ({"base": "a" * 40, "entries": {}}, "entries"),
            ({"base": "a" * 40, "entries": ["x"]}, "not an object"),
            ({"base": "a" * 40, "entries": [{"mode": "100644"}]}, "no path"),
            ({"base": "a" * 40, "entries": [{"path": None}]}, "no path"),
        ],
    )
    def test_a_malformed_manifest_is_refused(
        self, tmp_path: Path, document: object, fragment: str
    ):
        artifact = _build(tmp_path, {}, manifest=document)

        with pytest.raises(ArtifactRejected, match=fragment):
            read_artifact(artifact)

    def test_a_mode_outside_the_allowlist_is_refused_here_too(self, tmp_path: Path):
        """The filter would also catch it; refusing at the boundary is cheaper
        and keeps a non-string mode away from the filter's string operations."""
        artifact = _build(
            tmp_path,
            manifest={
                "base": "a" * 40,
                "entries": [{"path": "x", "mode": "120000", "sha256": "b" * 64}],
            },
        )

        with pytest.raises(ArtifactRejected, match="mode"):
            read_artifact(artifact)

    def test_a_non_string_mode_is_refused_rather_than_crashing(self, tmp_path: Path):
        artifact = _build(
            tmp_path,
            manifest={
                "base": "a" * 40,
                "entries": [{"path": "x", "mode": {}, "sha256": "b" * 64}],
            },
        )

        with pytest.raises(ArtifactRejected, match="mode"):
            read_artifact(artifact)

    def test_a_commit_id_with_a_trailing_newline_is_refused(self, tmp_path: Path):
        """Python's `$` also matches just BEFORE a trailing newline.

        So `match()` accepted "<40 hex>\\n" as a commit id — which would then be
        handed to a git command. `fullmatch()` is what makes the anchor mean
        what it looks like it means.
        """
        artifact = _build(
            tmp_path, {}, manifest={"base": "a" * 40 + "\n", "entries": []}
        )

        with pytest.raises(ArtifactRejected, match="base"):
            read_artifact(artifact)

    def test_a_digest_with_a_trailing_newline_is_refused(self, tmp_path: Path):
        artifact = _build(
            tmp_path,
            manifest={
                "base": "a" * 40,
                "entries": [{"path": "x", "mode": "100644", "sha256": "b" * 64 + "\n"}],
            },
        )

        with pytest.raises(ArtifactRejected, match="sha256"):
            read_artifact(artifact)

    def test_invalid_json_is_refused(self, tmp_path: Path):
        artifact = _build(tmp_path)
        (artifact / MANIFEST_NAME).write_text("{not json", encoding="utf-8")

        with pytest.raises(ArtifactRejected, match="not valid UTF-8 JSON"):
            read_artifact(artifact)

    def test_a_missing_manifest_is_refused(self, tmp_path: Path):
        artifact = _build(tmp_path)
        (artifact / MANIFEST_NAME).unlink()

        with pytest.raises(ArtifactRejected, match="could not be opened"):
            read_artifact(artifact)


class TestBounds:
    def test_an_oversized_manifest_is_refused_before_it_is_parsed(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr("kew.publication_artifact.MAX_MANIFEST_BYTES", 32)
        artifact = _build(tmp_path)

        with pytest.raises(ArtifactRejected, match="bytes"):
            read_artifact(artifact)

    def test_too_many_entries_is_refused(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kew.publication_artifact.MAX_ENTRIES", 1)
        artifact = _build(tmp_path, {"a.py": b"1", "b.py": b"2"})

        with pytest.raises(ArtifactRejected, match="more than"):
            read_artifact(artifact)


class TestItHandsOffToTheFilter:
    def test_a_verified_artifact_still_has_to_pass_the_filter(self, tmp_path: Path):
        """Well-formed is not the same as safe to write.

        The artifact reader establishes that the container's manifest is
        internally honest. Whether its PATHS may touch the destination is the
        filter's question, and this proves the two compose.
        """
        from kew.publication_filter import filter_artifact

        artifact = read_artifact(_build(tmp_path, {".git/config": b"[core]\n"}))
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])

        assert {r.rule for r in verdict.rejections} == {"dot_git"}


class TestAnEmptyPackageHasToProveItself:
    """The one artifact whose meaning is not in its own bytes.

    "The agent changed nothing" and "the agent never committed, and the
    producer only reads commits" are the same empty manifest. The second is
    silent data loss wearing the first one's clothes, so an empty package is
    accepted only from a producer that says it looked at the whole workspace.
    """

    def test_an_empty_package_that_declares_the_workspace_reads(self, tmp_path: Path):
        artifact = read_artifact(
            _build(
                tmp_path,
                {},
                manifest={"scope": "workspace", "base": "a" * 40, "entries": []},
            )
        )

        assert artifact.entries == ()
        assert artifact.deletions == ()
        assert artifact.scope == WORKSPACE_SCOPE

    def test_an_empty_package_that_declares_nothing_is_refused(self, tmp_path: Path):
        artifact = _build(tmp_path, {}, manifest={"base": "a" * 40, "entries": []})

        with pytest.raises(ArtifactRejected, match="cannot be read as a no-change"):
            read_artifact(artifact)

    def test_a_package_that_only_deletes_is_not_empty(self, tmp_path: Path):
        """Removals are work. A package carrying one is evidence of itself."""
        artifact = read_artifact(
            _build(
                tmp_path,
                {},
                manifest={
                    "base": "a" * 40,
                    "entries": [],
                    "deletions": ["src/gone.py"],
                },
            )
        )

        assert artifact.deletions == ("src/gone.py",)
        assert artifact.scope is None

    @pytest.mark.parametrize("scope", ["commits", "", "WORKSPACE", 7, []])
    def test_a_scope_that_is_not_one_this_host_knows_is_refused(
        self, tmp_path: Path, scope: object
    ):
        """Including on a NON-empty package: an unknown scope is a producer
        making a claim this host cannot check, which is worse than silence."""
        artifact = _build(
            tmp_path, manifest={"scope": scope, "base": "a" * 40, "entries": []}
        )

        with pytest.raises(ArtifactRejected, match="scope"):
            read_artifact(artifact)

    def test_the_declared_scope_is_carried_to_the_caller(self, tmp_path: Path):
        """Lineage records what the package covered, so it must survive the read."""
        artifact = read_artifact(
            _build(
                tmp_path,
                {"src/main.py": b"print(1)\n"},
                manifest={
                    "scope": "workspace",
                    "base": "a" * 40,
                    "entries": [
                        {
                            "path": "src/main.py",
                            "mode": "100644",
                            "sha256": _digest(b"print(1)\n"),
                        }
                    ],
                },
            )
        )

        assert artifact.scope == WORKSPACE_SCOPE


class TestDeletions:
    """A manifest that cannot express removals publishes work it did not do."""

    def test_deletions_are_carried(self, tmp_path: Path):
        artifact = read_artifact(
            _build(
                tmp_path,
                {},
                manifest={
                    "base": "a" * 40,
                    "entries": [],
                    "deletions": ["src/gone.py", "docs/old.md"],
                },
            )
        )

        assert artifact.deletions == ("src/gone.py", "docs/old.md")

    def test_absent_deletions_mean_none(self, tmp_path: Path):
        """Most artifacts delete nothing; the key stays optional."""
        assert read_artifact(_build(tmp_path)).deletions == ()

    @pytest.mark.parametrize(
        "document",
        [
            {"base": "a" * 40, "entries": [], "deletions": "nope"},
            {"base": "a" * 40, "entries": [], "deletions": [123]},
            {"base": "a" * 40, "entries": [], "deletions": [""]},
        ],
    )
    def test_malformed_deletions_are_refused(self, tmp_path: Path, document: object):
        artifact = _build(tmp_path, {}, manifest=document)

        with pytest.raises(ArtifactRejected, match="deletion"):
            read_artifact(artifact)

    def test_deletions_still_have_to_pass_the_filter(self, tmp_path: Path):
        """The reader proves the artifact is honest, not that its paths are safe."""
        from kew.publication_filter import filter_artifact

        artifact = read_artifact(
            _build(
                tmp_path,
                {},
                manifest={
                    "base": "a" * 40,
                    "entries": [],
                    "deletions": [".git/hooks/pre-commit"],
                },
            )
        )
        verdict = filter_artifact(artifact, existing_modes={}, destination_paths=[])

        assert {r.rule for r in verdict.rejections} == {"dot_git"}


class TestVerificationCannotBeOutrunOrRerouted:
    """Round-two review of this module: races, bounds, and parser escapes."""

    def test_a_blob_bigger_than_its_pre_open_size_is_refused(self, tmp_path: Path):
        """The size seen before reading is not a bound on what a read returns.

        A blob the agent is still writing keeps the loop fed, so hashing "until
        EOF" has no end and the reported size understates what was actually
        read. Simulated by understating the pre-open size, which is exactly the
        state a growth race produces — patching the read loop itself would test
        the mock.
        """
        import kew.publication_artifact as module

        artifact = _build(tmp_path, {"big.txt": b"x" * 4096})

        def _understated(blobs_dir, digest):
            # Opened directly, bypassing the PRE-OPEN size check, so the only
            # thing left that can refuse this is the bound inside the read loop.
            fd = module._open_regular(Path(digest), f"blob {digest}", dir_fd=blobs_dir)
            return fd, 0

        with (
            patch.object(module, "MAX_BLOB_BYTES", 512),
            patch.object(module, "_open_blob", _understated),
            pytest.raises(ArtifactRejected, match="while being read"),
        ):
            read_artifact(artifact)

    def test_many_paths_sharing_one_blob_count_once_each(self, tmp_path: Path):
        """20,000 paths can reference one blob.

        Counting unique digests bounds the ARTIFACT while the work of applying
        it is measured in terabytes, so the aggregate is over entries.
        """
        payload = b"shared content"
        digest = _digest(payload)
        artifact = tmp_path / "artifact"
        (artifact / BLOBS_DIR).mkdir(parents=True)
        (artifact / BLOBS_DIR / digest).write_bytes(payload)
        (artifact / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "base": "a" * 40,
                    "entries": [
                        {"path": f"copy{i}.txt", "mode": "100644", "sha256": digest}
                        for i in range(50)
                    ],
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("kew.publication_artifact.MAX_TOTAL_BYTES", 100),
            pytest.raises(ArtifactRejected, match="expands to"),
        ):
            read_artifact(artifact)

    def test_a_spare_blob_is_refused_without_listing_the_whole_directory(
        self, tmp_path: Path
    ):
        """MAX_ENTRIES bounds the MANIFEST, not what the agent put in blobs/."""
        artifact = _build(tmp_path)
        payload = b"stowaway"
        (artifact / BLOBS_DIR / _digest(payload)).write_bytes(payload)

        with pytest.raises(ArtifactRejected, match="not referenced"):
            read_artifact(artifact)

    @pytest.mark.parametrize(
        ("document", "why"),
        [
            ("[" * 2000 + "]" * 2000, "deeply nested arrays raise RecursionError"),
            ('{"base": ' + "9" * 6000 + "}", "a huge int literal raises ValueError"),
        ],
    )
    def test_hostile_json_becomes_a_refusal_not_a_crash(
        self, tmp_path: Path, document: str, why: str
    ):
        """Neither RecursionError nor ValueError is a JSONDecodeError, so both
        escaped a boundary whose entire job is turning bad input into a
        refusal."""
        artifact = _build(tmp_path, {})
        (artifact / MANIFEST_NAME).write_text(document, encoding="utf-8")

        with pytest.raises(ArtifactRejected):
            read_artifact(artifact)


class TestThePackageHasAName:
    """I07: a digest over what the package MEANS, so lineage can bind to it."""

    def test_the_name_is_a_sha256_reference(self, tmp_path: Path):
        digest = package_digest(read_artifact(_build(tmp_path)))
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64

    def test_the_same_work_produced_twice_is_one_name(self, tmp_path: Path):
        first = read_artifact(_build(tmp_path / "a", {"x.py": b"1\n", "y.py": b"2\n"}))
        second = read_artifact(_build(tmp_path / "b", {"y.py": b"2\n", "x.py": b"1\n"}))
        assert package_digest(first) == package_digest(second)

    def test_a_different_base_is_a_different_package(self, tmp_path: Path):
        one = read_artifact(_build(tmp_path / "a", base="a" * 40))
        two = read_artifact(_build(tmp_path / "b", base="b" * 40))
        assert package_digest(one) != package_digest(two)

    def test_a_different_mode_is_a_different_package(self, tmp_path: Path):
        plain = read_artifact(_build(tmp_path / "a"))
        executable = read_artifact(
            _build(tmp_path / "b", modes={"src/main.py": "100755"})
        )
        assert package_digest(plain) != package_digest(executable)

    def test_a_deletion_is_part_of_the_package(self, tmp_path: Path):
        without = read_artifact(_build(tmp_path / "a"))
        with_deletion = read_artifact(
            _build(
                tmp_path / "b",
                {},
                manifest={
                    "base": "a" * 40,
                    "entries": [],
                    "deletions": ["gone.py"],
                    "scope": WORKSPACE_SCOPE,
                },
            )
        )
        assert package_digest(without) != package_digest(with_deletion)

    def test_a_declared_scope_is_part_of_the_package(self, tmp_path: Path):
        silent = read_artifact(_build(tmp_path / "a"))
        declared = read_artifact(
            _build(
                tmp_path / "b",
                manifest={
                    "base": "a" * 40,
                    "entries": [
                        {
                            "path": "src/main.py",
                            "mode": "100644",
                            "sha256": _digest(b"print(1)\n"),
                        }
                    ],
                    "scope": WORKSPACE_SCOPE,
                },
            )
        )
        assert silent.scope is None and declared.scope == WORKSPACE_SCOPE
        assert package_digest(silent) != package_digest(declared)
