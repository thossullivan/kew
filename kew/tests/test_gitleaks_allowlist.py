"""The repository gitleaks allowlists must never hide a real credential.

`.gitleaks.toml` allowlists the bounded-run-key fixture ``bounded-secret-<n>``
(#1957). The allowlist is scoped to the captured secret and anchored, so a real
token that shares a line with the fixture, or that merely contains the phrase,
is still reported. These cases run the installed gitleaks against a scratch
directory with the repository configuration; they skip when the binary is
absent (CI's ``secrets`` job installs it; ``scripts/verify.sh`` requires it).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".gitleaks.toml"
THIS_MODULE = "kew/tests/test_gitleaks_allowlist.py"

# Built at runtime so this module never carries a token-shaped literal itself.
_FAKE_PAT = "ghp_" + "A1b2C3d4E5f6" * 3  # 36 characters after the prefix


def _scan(
    tmp_path: Path, name: str, line: str, relpath: str = "fixture.py"
) -> list[dict[str, object]]:
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        pytest.skip("gitleaks is not installed")
    target = tmp_path / name
    sample = target / relpath
    sample.parent.mkdir(parents=True)
    sample.write_text(line + "\n", encoding="utf-8")
    # CI runs `gitleaks detect --source .` on a git checkout, where findings carry
    # paths relative to the repository root; a scratch repository reproduces that
    # (the `dir` mode reports absolute paths, which no path allowlist matches).
    git_env = {
        "GIT_AUTHOR_NAME": "kew",
        "GIT_AUTHOR_EMAIL": "kew@invalid.local",
        "GIT_COMMITTER_NAME": "kew",
        "GIT_COMMITTER_EMAIL": "kew@invalid.local",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": "/usr/bin:/bin",
    }
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "sample"],
    ):
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv, cwd=target, env=git_env, check=True, capture_output=True, timeout=60
        )
    report = tmp_path / f"{name}.json"
    subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            gitleaks,
            "detect",
            "--source",
            str(target),
            "--config",
            str(CONFIG),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--exit-code",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if not report.exists():
        return []
    body = report.read_text(encoding="utf-8").strip()
    return json.loads(body) if body else []


def test_the_fixture_alone_is_allowlisted(tmp_path: Path) -> None:
    findings = _scan(tmp_path, "fixture", 'issued_key="bounded-secret-1944"')
    assert findings == []


def test_a_real_token_on_the_fixture_line_is_still_reported(tmp_path: Path) -> None:
    line = f'issued_key="bounded-secret-1944"; github_token = "{_FAKE_PAT}"'
    findings = _scan(tmp_path, "shared-line", line)
    assert any(item["Secret"] == _FAKE_PAT for item in findings), findings


def test_a_secret_that_contains_the_phrase_is_still_reported(tmp_path: Path) -> None:
    # Assembled at runtime from fragments so this source file never carries a
    # token-shaped literal that the repository's own secrets gate would flag.
    value = "".join(("xK9", "bounded-secret-", "1944", "Qz7pL2mN4vR8"))
    line = f'api_key = "{value}"'
    findings = _scan(tmp_path, "containing", line)
    assert findings, "an unanchored allowlist would have hidden this secret"


def test_this_module_is_not_a_blind_spot(tmp_path: Path) -> None:
    """The allowlist for this file is an AND of its exact path and one historical
    sample value: a real token committed here is still reported."""
    token_line = f'github_token = "{_FAKE_PAT}"'
    findings = _scan(tmp_path, "token-here", token_line, THIS_MODULE)
    assert any(item["Secret"] == _FAKE_PAT for item in findings), findings


def test_only_the_historical_sample_in_this_module_is_exempt(tmp_path: Path) -> None:
    """The literal an earlier revision of this file carried is exempt here and
    only here; the same value under a similar path is reported."""
    historical = "".join(("xK9", "bounded-secret-", "1944", "Qz7pL2mN4vR8"))
    line = f'api_key = "{historical}"'
    assert _scan(tmp_path, "historical", line, THIS_MODULE) == []
    for relpath in (
        THIS_MODULE + ".bak",
        "old/" + THIS_MODULE,
        "kew/tests/test_certification_conformance.py.orig",
    ):
        findings = _scan(tmp_path, relpath.replace("/", "_"), line, relpath)
        assert findings, relpath


CONFORMANCE_MODULE = "kew/tests/test_certification_conformance.py"


def test_conformance_module_is_not_a_blind_spot(tmp_path: Path) -> None:
    """#1962: the sanitizer test was allowlisted by path alone, which skipped
    every rule for the file. A real token committed there is now reported."""
    token_line = f'github_token = "{_FAKE_PAT}"'
    findings = _scan(tmp_path, "conformance-token", token_line, CONFORMANCE_MODULE)
    assert any(item["Secret"] == _FAKE_PAT for item in findings), findings


def test_only_the_sanitizer_fixture_in_the_conformance_module_is_exempt(
    tmp_path: Path,
) -> None:
    """The sequential fake the sanitizer test needs is exempt at that exact path
    and nowhere else; another generic key at that path is still reported."""
    fixture = "".join(("abcdef", "0123456789"))
    line = f'{{"where": "api_key={fixture}"}},'
    assert _scan(tmp_path, "conformance-fixture", line, CONFORMANCE_MODULE) == []
    elsewhere = _scan(tmp_path, "fixture-elsewhere", line, "kew/tests/test_other.py")
    assert any(item["Secret"] == fixture for item in elsewhere), elsewhere
    other = "".join(("q7Zp", "L2mN4vR8", "xK9w"))
    other_line = f'{{"where": "api_key={other}"}},'
    findings = _scan(tmp_path, "conformance-other", other_line, CONFORMANCE_MODULE)
    assert any(item["Secret"] == other for item in findings), findings


SCAN_TEST_MODULES = (
    "kew/tests/test_ci_fix.py",
    "kew/tests/test_publication_worker.py",
)


@pytest.mark.parametrize("module", SCAN_TEST_MODULES)
def test_only_the_synthetic_provider_placeholder_is_exempt(
    tmp_path: Path, module: str
) -> None:
    """#1969: the fake SYNTHETIC_PROVIDER_KEY is exempt in the two publication-
    scan test files only; elsewhere it is reported, and another generic key in
    those files is still reported."""
    fixture = "-".join(("kew", "placeholder", "synthetic", "key", "0123"))
    line = f'    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "{fixture}")'
    stem = module.rsplit("/", 1)[-1].removesuffix(".py")
    assert _scan(tmp_path, f"{stem}-fixture", line, module) == []
    elsewhere = _scan(tmp_path, f"{stem}-elsewhere", line, "kew/tests/test_other.py")
    assert any(item["Secret"] == fixture for item in elsewhere), elsewhere
    other = "".join(("q7Zp", "L2mN4vR8", "xK9w", "T3yU"))
    other_line = f'    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "{other}")'
    findings = _scan(tmp_path, f"{stem}-other", other_line, module)
    assert any(item["Secret"] == other for item in findings), findings


LAUNCHER_MODULE = "kew/tests/test_openshell_launcher.py"


def _launcher_samples() -> dict[str, str]:
    """The launcher test's fake credential-shape samples, read from its source.

    Read rather than restated, so this module carries no token-shaped literal
    and the two cannot drift apart: the allowlist must cover exactly what the
    launcher test commits.
    """
    source = (REPO_ROOT / LAUNCHER_MODULE).read_text(encoding="utf-8")
    block = source.split("_FORWARDED_CREDENTIAL_SAMPLES = {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r'"([a-z-]+)": "([^"]+)"', block))


@pytest.mark.parametrize(
    "shape", ["stripe-secret-key", "aws-access-key-id", "slack-bot-token"]
)
def test_only_the_launcher_samples_are_exempt(tmp_path: Path, shape: str) -> None:
    """#2317: the launcher test's fake samples are exempt in that one file only;
    the same value elsewhere is reported, and another token in that file is too."""
    sample = _launcher_samples()[shape]
    line = f'    "{shape}": "{sample}",'
    assert _scan(tmp_path, f"{shape}-fixture", line, LAUNCHER_MODULE) == []
    elsewhere = _scan(tmp_path, f"{shape}-elsewhere", line, "kew/tests/test_other.py")
    assert elsewhere, elsewhere
    token_line = f'github_token = "{_FAKE_PAT}"'
    findings = _scan(tmp_path, f"{shape}-other", token_line, LAUNCHER_MODULE)
    assert any(item["Secret"] == _FAKE_PAT for item in findings), findings


RUN_CREDENTIALS_MODULE = "kew/tests/test_run_credentials.py"


def test_run_credentials_module_is_not_a_blind_spot(tmp_path: Path) -> None:
    """#2318: the run-credentials test's fake value is exempt by exact value, so
    any other token committed to that file is still reported."""
    token_line = f'github_token = "{_FAKE_PAT}"'
    findings = _scan(
        tmp_path, "run-credentials-other", token_line, RUN_CREDENTIALS_MODULE
    )
    assert any(item["Secret"] == _FAKE_PAT for item in findings), findings


REDACTION_TEST_MODULE = "kew/tests/test_credential_redaction.py"


@pytest.mark.parametrize(
    ("name", "fixture"),
    [
        ("long", "".join(("sk-", "placeholder", "0123456789"))),
        ("short", "".join(("placeholder", "123"))),
    ],
    ids=["long", "short"],
)
def test_only_the_redaction_test_placeholders_are_exempt(
    tmp_path: Path, name: str, fixture: str
) -> None:
    """#3345: the fake known secrets the wrapped-secret redaction tests trace are
    exempt in that one file only; elsewhere they are reported, and another
    generic key in that file is still reported."""
    line = f'    secret = "{fixture}"'
    assert _scan(tmp_path, f"redaction-{name}", line, REDACTION_TEST_MODULE) == []
    elsewhere = _scan(
        tmp_path, f"redaction-{name}-elsewhere", line, "kew/tests/test_other.py"
    )
    assert any(item["Secret"] == fixture for item in elsewhere), elsewhere
    other = "".join(("q7Zp", "L2mN4vR8", "xK9w", "T3yU"))
    other_line = f'    secret = "{other}"'
    findings = _scan(
        tmp_path, f"redaction-{name}-other", other_line, REDACTION_TEST_MODULE
    )
    assert any(item["Secret"] == other for item in findings), findings
