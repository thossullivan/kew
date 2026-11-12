"""Regression test: every non-.py runtime file ships in the built wheel.

Guards against "works from a source checkout, broken when installed".
If you add a runtime data file under src/kew/, add it to RUNTIME_FILES —
test_runtime_file_list_matches_source_tree will fail until you do.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]  # the kew/ subdirectory

RUNTIME_FILES = {
    # IBM Plex, inlined into every evidence pack as base64 (#1126). Latin
    # subsets from @fontsource; LICENSE.txt is the SIL OFL 1.1 they ship under.
    "kew/assets/fonts/LICENSE.txt",
    "kew/assets/fonts/ibm-plex-mono-latin-400-normal.woff2",
    "kew/assets/fonts/ibm-plex-mono-latin-600-normal.woff2",
    "kew/assets/fonts/ibm-plex-sans-latin-400-normal.woff2",
    "kew/assets/fonts/ibm-plex-sans-latin-600-normal.woff2",
    "kew/_skills/plan-to-issues/SKILL.md",
    "kew/_skills/playbook-skill/SKILL.md",
    "kew/_skills/verify-parity/SKILL.md",
    "kew/_skills/handoff/SKILL.md",
    "kew/_skills/operate-kew/SKILL.md",
    "kew/_skills/adversarial-reviewer/PROMPT.md",
    "kew/_skills/manifest.json",
    "kew/_templates/_operator_addendum.md.j2",
    "kew/_templates/ci-fix.md.j2",
    "kew/_templates/dispatch.md.j2",
    "kew/_templates/epic-plan.md.j2",
    "kew/_templates/resolve.md.j2",
    "kew/_templates/review.md.j2",
    "kew/_templates/triage.md.j2",
    "kew/_templates/security-guard.sh",
    "kew/_docker/docker/agent-codex.Dockerfile",
    "kew/_docker/docker/agent-entrypoint.sh",
    "kew/_docker/docker/agent-opencode-entrypoint.sh",
    "kew/_docker/docker/agent-opencode.Dockerfile",
    "kew/_docker/docker/agent.Dockerfile",
    "kew/_docker/docker/extract-artifact.sh",
    "kew/_docker/docker/kew-openshell-launch",
    "kew/_docker/docker/opencode.json",
    # Host-side inputs to `openshell sandbox exec` — an installed kew must
    # carry the containment policies, not only a source checkout.
    "kew/_docker/docker/openshell/policy-claude-code.yaml",
    "kew/_docker/docker/openshell/policy-codex.yaml",
    "kew/_docker/docker/openshell/policy-opencode.yaml",
    "kew/_docker/docker/openshell/policy-review-claude-code.yaml",
    "kew/_docker/docker/openshell/policy-review-provision-claude-code.yaml",
    "kew/_docker/docker/switchyard.Dockerfile",
    "kew/_docker/docker/verify-npm-overlay.js",
    # The model catalog (#2708): the console and /api/models read it from the
    # installed package, and its lifecycle rows are the runtime lifecycle
    # default (#2709), so the wheel must carry it.
    "kew/models.json",
    # The provisioning recipe `kew hosts provision` runs (#2381). recipe_path()
    # resolves it inside the package, so an installed kew that lacks it cannot
    # build a host at all.
    "kew/hosts/provision/ubuntu-24.04.sh",
    # The scanner pin that recipe reads instead of carrying a literal (#2462).
    # `kew hosts provision` copies it to the host beside the recipe, and a
    # recipe that cannot read it refuses to install an unpinned scanner — so an
    # installed kew without this file cannot build a host either.
    "kew/hosts/provision/trivy.pin",
    "kew/py.typed",
}


@pytest.fixture(scope="session")
def wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a fresh wheel, or inspect one provided via KEW_WHEEL in CI."""
    env_wheel = os.environ.get("KEW_WHEEL")
    if env_wheel:
        return Path(env_wheel)
    out_dir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=PACKAGE_DIR,
        check=True,
        capture_output=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_wheel_contains_all_runtime_files(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as whl:
        names = set(whl.namelist())
    missing = RUNTIME_FILES - names
    assert not missing, f"runtime files missing from wheel: {sorted(missing)}"


def test_wheel_ships_no_internal_playbook(wheel_path: Path) -> None:
    """kew's own concurrent-state-change playbook stays in this repository (#2016)."""
    with zipfile.ZipFile(wheel_path) as whl:
        names = whl.namelist()
        manifest = json.loads(whl.read("kew/_skills/manifest.json"))
    assert not [
        n for n in names if n.startswith("kew/_skills/concurrent-state-change/")
    ]
    assert "concurrent-state-change" not in {a["name"] for a in manifest["artifacts"]}


def test_wheel_metadata_refuses_python_3_14(wheel_path: Path) -> None:
    """The wheel installs on Python 3.13 and refuses 3.14 (#2019)."""
    from packaging.specifiers import SpecifierSet

    with zipfile.ZipFile(wheel_path) as whl:
        metadata_name = next(
            n for n in whl.namelist() if n.endswith(".dist-info/METADATA")
        )
        metadata = whl.read(metadata_name).decode()
    requires = [
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Python:")
    ]
    assert len(requires) == 1, metadata
    spec = SpecifierSet(requires[0])
    assert "3.13.0" in spec
    assert "3.13.9" in spec
    assert "3.14.0" not in spec
    assert "3.12.9" not in spec


# Apache-2.0 section 4(d): NOTICE (the IBM Plex attribution) travels with
# every redistribution, next to LICENSE (F22).
LICENSE_FILES = ("LICENSE", "NOTICE")


def test_notice_matches_the_repository_notice() -> None:
    packaged = PACKAGE_DIR / "NOTICE"
    assert packaged.read_bytes() == (PACKAGE_DIR.parent / "NOTICE").read_bytes()


def test_wheel_carries_license_and_notice(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        licenses = {
            Path(name).name: wheel.read(name)
            for name in wheel.namelist()
            if ".dist-info/licenses/" in name
        }
    for name in LICENSE_FILES:
        assert licenses.get(name) == (PACKAGE_DIR / name).read_bytes(), name


def test_wheel_contains_the_python_dash_m_entry_point(wheel_path: Path) -> None:
    """#2003: ``python -m kew`` needs kew/__main__.py in the installed wheel."""
    with zipfile.ZipFile(wheel_path) as wheel:
        assert "kew/__main__.py" in wheel.namelist()


def test_wheel_contains_the_default_provisioning_recipe(wheel_path: Path) -> None:
    """#2381: ``kew hosts provision`` reads the recipe out of the package.

    Asserted against ``RECIPE_DIR``/``DEFAULT_RECIPE`` rather than a literal:
    the thing that must ship is whatever path :func:`recipe_path` resolves, so
    renaming the recipe or its directory without repackaging fails here.
    """
    from kew.hosts.provisioning import DEFAULT_RECIPE, RECIPE_DIR

    recipe = RECIPE_DIR / DEFAULT_RECIPE
    archived = f"kew/{recipe.relative_to(PACKAGE_DIR / 'src' / 'kew').as_posix()}"
    with zipfile.ZipFile(wheel_path) as wheel:
        assert archived in wheel.namelist()
        assert wheel.read(archived) == recipe.read_bytes()


def test_wheel_contains_the_scanner_pin_the_recipe_reads(wheel_path: Path) -> None:
    """#2462: the recipe reads its trivy version out of the package, not itself.

    Asserted against ``pin_path()`` rather than a literal, like the recipe
    above: whatever file kew resolves is the file that must ship, because a host
    is provisioned by copying both of them up and a recipe with no pin refuses
    to install a scanner nobody chose.
    """
    from kew.hosts.provisioning import pin_path

    pin = pin_path()
    archived = f"kew/{pin.relative_to(PACKAGE_DIR / 'src' / 'kew').as_posix()}"
    with zipfile.ZipFile(wheel_path) as wheel:
        assert archived in wheel.namelist()
        assert wheel.read(archived) == pin.read_bytes()


def test_wheel_contains_switchyard_dockerfile(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        assert "kew/_docker/docker/switchyard.Dockerfile" in wheel.namelist()


def test_packaged_switchyard_dockerfile_matches_the_build_input() -> None:
    repository_copy = PACKAGE_DIR.parent / "docker" / "switchyard.Dockerfile"
    packaged_copy = (
        PACKAGE_DIR / "src" / "kew" / "_docker" / "docker" / repository_copy.name
    )

    assert packaged_copy.read_bytes() == repository_copy.read_bytes()


def test_wheel_preserves_docker_input_executable_bits(wheel_path: Path) -> None:
    source = PACKAGE_DIR / "src"
    docker_files = sorted(path for path in RUNTIME_FILES if "/_docker/" in path)
    with zipfile.ZipFile(wheel_path) as wheel:
        for name in docker_files:
            archived_mode = wheel.getinfo(name).external_attr >> 16
            source_mode = (source / name).stat().st_mode
            assert bool(archived_mode & 0o111) == bool(source_mode & 0o111)


def _install_wheel(wheel: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
    )
    python = venv / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(wheel),
        ],
        check=True,
        capture_output=True,
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        entry for entry in sys.path if "site-packages" in Path(entry).parts
    )
    return venv, environment


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "docker-calls.jsonl"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["DOCKER_FAKE_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["network", "inspect"]:
    raise SystemExit(1)
if args[:2] == ["image", "inspect"]:
    print(json.dumps([{"Id": "sha256:" + "a" * 64}]))
"""
    )
    docker.chmod(0o755)
    return fake_bin, log


def _docker_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_installed_wheel_cli_runs_the_packaged_builder(
    wheel_path: Path, tmp_path: Path
) -> None:
    from kew.sandbox import agent_image_inputs_digest

    venv, environment = _install_wheel(wheel_path, tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    environment["DOCKER_FAKE_LOG"] = str(log)
    environment["PATH"] = os.pathsep.join((str(fake_bin), environment["PATH"]))
    expected_tag = (
        f"kew-agent:docker-{agent_image_inputs_digest(PACKAGE_DIR.parent)[:12]}"
    )

    built = subprocess.run(
        [str(venv / "bin" / "kew"), "sandbox", "build"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    assert expected_tag in built.stderr
    calls = _docker_calls(log)
    # W42 setup daemon preflight refuses unavailable runtimes before building.
    assert calls[0] == ["info"]
    assert calls[1] == ["network", "inspect", "kew-allowlist"]
    build = calls[2]
    assert build[0] == "build"
    # Drop the explicit proxy build-args; the pinning of those is covered in
    # tests/test_sandbox.py. What matters here is the installed context.
    while build[1] == "--build-arg":
        del build[1:3]
    assert build[0:2] == ["build", "-f"]
    dockerfile = Path(build[2])
    context = Path(build[-1])
    assert dockerfile == context / "docker" / "agent.Dockerfile"
    assert context.is_relative_to(venv)
    # The image carries the full digest of the inputs it was built from (#3160).
    assert build[3:] == [
        "-t",
        expected_tag,
        "--label",
        f"dev.kew.agent-image-inputs={agent_image_inputs_digest(PACKAGE_DIR.parent)}",
        str(context),
    ]
    assert calls[3] == ["image", "inspect", expected_tag]
    assert all(call[:2] != ["network", "create"] for call in calls)
    assert "kew-allowlist is absent" in built.stdout
    assert "Provision a restricted network" in built.stdout

    log.unlink()
    refused = subprocess.run(
        [
            str(venv / "bin" / "kew"),
            "sandbox",
            "build",
            "--tag",
            "kew-agent:docker-000000000000",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert refused.returncode == 2
    assert "does not match the packaged inputs" in refused.stderr
    assert expected_tag in refused.stderr
    assert not log.exists(), "wrong digest reached the container runtime"


@pytest.fixture()
def mutated_wheel_path(wheel_path: Path, tmp_path: Path) -> Path:
    mutated = tmp_path / wheel_path.name
    shutil.copyfile(wheel_path, mutated)
    with zipfile.ZipFile(mutated, "a") as wheel:
        wheel.writestr("kew/_docker/docker/unexpected.txt", "injected")
    return mutated


def test_installed_builder_refuses_a_wheel_with_an_extra_input_before_runtime(
    mutated_wheel_path: Path, tmp_path: Path
) -> None:
    venv, environment = _install_wheel(mutated_wheel_path, tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    environment["DOCKER_FAKE_LOG"] = str(log)
    environment["PATH"] = os.pathsep.join((str(fake_bin), environment["PATH"]))

    refused = subprocess.run(
        [str(venv / "bin" / "kew"), "sandbox", "build"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert refused.returncode == 2
    assert "manifest mismatch" in refused.stderr
    assert "unexpected.txt" in refused.stderr
    assert not log.exists(), "mutated package reached the container runtime"


def test_packaged_docker_manifest_rejects_switchyard_drift(tmp_path: Path) -> None:
    from kew.sandbox import _validate_packaged_agent_image_manifest

    packaged = tmp_path / "packaged"
    shutil.copytree(PACKAGE_DIR / "src" / "kew" / "_docker", packaged)
    (packaged / "docker" / "switchyard.Dockerfile").unlink()

    with pytest.raises(ValueError, match="switchyard.Dockerfile"):
        _validate_packaged_agent_image_manifest(packaged)


def test_runtime_file_list_matches_source_tree() -> None:
    """RUNTIME_FILES must stay in lockstep with the actual source tree."""
    src = PACKAGE_DIR / "src" / "kew"
    on_disk = {
        f"kew/{path.relative_to(src).as_posix()}"
        for path in src.rglob("*")
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
        and not path.name.startswith(".")
        # serve/web/ is the generated SvelteKit bundle (git-ignored, shipped via
        # hatch artifacts) — a build output, not tracked runtime source.
        and path.relative_to(src).parts[:2] != ("serve", "web")
    }
    assert on_disk == RUNTIME_FILES


def test_sdist_contains_packaged_docker_inputs(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    out_dir = tmp_path_factory.mktemp("sdist")
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(out_dir)],
        cwd=PACKAGE_DIR,
        check=True,
        capture_output=True,
    )
    archives = list(out_dir.glob("*.tar.gz"))
    assert len(archives) == 1
    expected = {f"src/{path}" for path in RUNTIME_FILES if "/_docker/" in path}
    expected |= set(LICENSE_FILES)
    with tarfile.open(archives[0], "r:gz") as archive:
        members = {
            "/".join(Path(member.name).parts[1:]): member
            for member in archive.getmembers()
        }
        assert expected <= members.keys()
        for name in expected:
            member = members[name]
            assert member.isfile()
            source = PACKAGE_DIR / name
            assert bool(member.mode & 0o111) == bool(source.stat().st_mode & 0o111)
            archived = archive.extractfile(member)
            assert archived is not None
            assert archived.read() == source.read_bytes()
