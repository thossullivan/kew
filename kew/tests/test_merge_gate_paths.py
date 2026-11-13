"""#2192 gate — the shipped ``merge_gate_paths`` cover THIS repo's gate files.

The serial merge queue holds a PR whose diff touches a path in ``[epic]
merge_gate_paths``, because a PR that edits what the gates ARE is a PR that can
decide what proves it (#2184). The list shipped with that change was spelled
root-anchored — ``.semgrep/``, ``.xenon-excludes``, no lockfile at all — while
this repo keeps the pinned semgrep ruleset at ``kew/.semgrep/pinned.yml``, the
xenon exclude list at ``kew/.xenon-excludes`` and lockfiles at ``uv.lock`` and
``kew/uv.lock``. Every one of them was unmatched, so a PR that rewrote the rules
it is scanned by merged unheld.

Nothing caught it because the tests asserted the patterns against hand-written
paths, and the hand-written paths were spelled the way the patterns were. The
first repair reproduced that in a subtler form: it named a gate definition with
a hand list of basenames and directories that restated the pattern list, so it
stayed green while ``.gitleaks.toml``, ``.gitleaksignore`` and ``mkdocs.yml``
— the secrets gate's own allowlist and the docs gate's own exclude list — went
unmatched. A list that is checked against a restatement of itself cannot fail
for the omitted-tool-config class it was written to catch.

So this gate does not define a gate definition at all. It asks the GATE RUNNERS
— the CI workflows, the scripts they invoke, the ``scripts`` table of each
``package.json`` — which tracked files they name, and requires every answer to
be covered by the patterns or listed here as a subject/artifact with a reason.
Naming is not the only way a runner reads a file: `vite build` and
`svelte-check` are never pointed at `vite.config.ts` or `svelte.config.js`,
they discover them, so a second oracle credits `<tool>.config.<ext>` to any
runner that invokes `<tool>` — without it the whole JS/TS tool-config class is
invisible here and the gate cannot fail for it.

Both of those oracles read `git ls-files`, and that is a third blind spot, the
widest one (#2192): the gate config that matters most need not EXIST yet. `ruff`
reads a root `.ruff.toml` or `ruff.toml` in preference to `pyproject.toml`,
`pytest` reads `pytest.ini`, `tox.ini` or `setup.cfg`; none of them is in this
tree, so a scan of this tree says nothing about them — while a PR that ADDS one
carrying `select = []` turns the lint gate off and is then verified green by the
gate it just wrote. So the last oracle asserts nothing about the tree at all: for
every tool a runner invokes, `_CONVENTIONAL_CONFIGS` names the files that tool
would obey if they appeared, and every one of them must be matched at the repo
root and inside each project directory.

Naming the tools by hand made that oracle only as good as someone's memory, and
it had no `semgrep` key while `semgrep scan` was one of the gates (#2192):
pinning a scanner's RULES (`--config .semgrep/pinned.yml`, covered by
`**/.semgrep/`) does nothing about its TARGETS, and a one-line `.semgrepignore`
the branch adds is obeyed anyway — the gate then scans nothing, passes
everything, and the queue attests `result: pass`. So the tool roster is read off
the runners' own invocations too, and each tool must either name its
conventional configs or be recorded in `_NO_CONVENTIONAL_CONFIG` as reading
none.

The runners are authored by whoever adds a gate, not by whoever edits
``merge_gate_paths``, so adding a tool whose config the patterns miss fails this
test. It is deliberately fail-closed: a runner that starts naming some new
tracked config file, or invoking some new tool, breaks this until someone
decides which side it is on.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import PurePosixPath

import pytest
from support.repo_inventory import REPO_ROOT, tracked_paths

from kew.config import DispatchConfig
from kew.graph.driver.merge_queue import touched_gate_paths

pytestmark = pytest.mark.policy

#: A file that RUNS a gate: the workflows GitHub executes and the helpers they
#: shell out to, plus the local mirror (`kew/scripts/verify.sh`) and every other
#: script sitting in a `scripts/` directory. Whatever these name, they read.
_RUNNER_PATTERN = re.compile(
    r"^(\.github/(workflows|scripts)/[^/]+|([^/]+/)*scripts/[^/]+\.(sh|mjs|js|py))$"
)

#: Suffixes that make a basename configuration rather than prose or source. A
#: runner mentioning `pinned.yml` is naming a file it obeys; one mentioning
#: `pricing.py` is naming a module it merely tests.
_CONFIG_SUFFIXES = (".lock", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".json")

#: Config a tool READS BY CONVENTION when it is merely invoked — no flag points
#: at it, and it does not have to exist yet, which is what makes it the hole a
#: tree scan cannot see. Each one either OUTRANKS the `pyproject.toml` the same
#: tool would otherwise read or, for a scanner's ignore list, empties the set
#: of files it is allowed to look at — either way the file the PR under test
#: ADDS is the file that decides what proves it (#2192). This table is the
#: whole contract, so it must name a file for EVERY tool a runner invokes: it
#: had no `semgrep` key while `_invoked("semgrep")` was True, and the
#: `.semgrepignore` hole stayed open under a green test. Keyed by the token a
#: runner spells to invoke the tool; values are basenames, checked at the root
#: and under each project directory, because a config lives where its project
#: does — `semgrep` runs from `kew/`, `gitleaks` from the repo root.
_CONVENTIONAL_CONFIGS: dict[str, tuple[str, ...]] = {
    # `ruff check` / `ruff format`: either spelling outranks `pyproject.toml`.
    "ruff": (".ruff.toml", "ruff.toml"),
    "mypy": ("mypy.ini", ".mypy.ini", "setup.cfg"),
    # `pytest` also takes `setup.py` / `tox.ini` / `setup.cfg` as rootdir
    # anchors, which is what decides WHICH config file it then reads.
    "pytest": ("pytest.ini", "tox.ini", "setup.cfg", "setup.py"),
    # `pytest --cov`: coverage.py prefers `.coveragerc` over everything.
    "--cov": (".coveragerc", "tox.ini", "setup.cfg"),
    "lint-imports": (".importlinter", "setup.cfg"),
    "bandit": (".bandit",),
    # The scanners' ignore lists. These do not choose the RULES — `semgrep
    # scan --config .semgrep/pinned.yml` and the pinned `trivy image` call
    # keep those — they choose the TARGETS, and both are read from the working
    # directory whatever `--config` was passed. A gate that scans nothing
    # passes everything, so pinning the ruleset does not close this.
    "semgrep": (".semgrepignore",),
    "trivy": (".trivyignore", "trivy.yaml", "trivy.yml"),
    # `gitleaks detect --source .` is pointed at neither of these; it reads
    # them from the source root, so a PR may ADD the allowlist that clears it.
    "gitleaks": (".gitleaks.toml", ".gitleaksignore"),
    # `docker build` reads the `.dockerignore` beside its context, which is
    # what decides whether the file a scanned image gate would flag is in it.
    "docker": (".dockerignore",),
    # Not a linter config but the same power: these choose the interpreter and
    # the resolution every `uv run` gate then executes under.
    "uv": ("uv.toml", ".python-version"),
    "npm": (".npmrc", ".nvmrc"),
    "mkdocs": ("mkdocs.yml", "mkdocs.yaml"),
    "vite": ("vite.config.ts", "vite.config.js"),
    "vitest": ("vitest.config.ts", "vitest.config.js"),
    "svelte-check": ("svelte.config.js", "svelte.config.ts"),
    "mutmut": ("setup.cfg",),
}

#: The other side of the table: a tool a runner invokes that reads NO config
#: by convention, with the reason. Every tool a runner spells must be on one
#: side or the other — that is what turns the table above from a list someone
#: remembered to extend into a contract. Leaving `semgrep` off both sides is
#: exactly how `.semgrepignore` went unnoticed while the tests were green.
_NO_CONVENTIONAL_CONFIG = {
    # The interpreter and kew itself. Which interpreter is chosen is `uv`'s
    # `.python-version`, above; neither reads a config that gates anything.
    "python": "an interpreter, not a gate",
    "python3": "an interpreter, not a gate",
    "kew": "the subject of the gates, not one of them",
    "claude": "the agent a script drives; it gates nothing",
    "sha256sum": "a checksum utility, no config of any kind",
    "shasum": "a checksum utility, no config of any kind",
    # Pointed at, never discovered: the runner passes the file on the command
    # line, so the file-naming oracle above already covers it.
    "xenon": "its exclude list is passed as `-e $(paste -sd, .xenon-excludes)`",
    "pip-audit": "audits the resolved `pyproject.toml` / lockfile; no own config",
    "pip-licenses": "policy is entirely in the runner's `--fail-on` flag",
    "cyclonedx-py": "reads the resolved environment; no config file",
    "twine": "credentials come from `~/.pypirc`, outside the repo",
    # `frontend/scripts/plex-arrows.sh` regenerates committed font subsets; it
    # is a build helper, not a gate, and fontTools discovers no config file.
    "pyftsubset": "fontTools' subsetter; every option is on the command line",
}

#: How a runner spells the tool it is about to run. `uv run <tool>` / `uvx
#: [--with X] <tool>` is how every Python gate here starts, and `command -v
#: <tool>` is how the shell scripts refuse to gate with a tool that is not
#: installed. Reading the roster off the runners is what makes the table above
#: fail closed: adding a gate names its tool here whether or not anyone
#: remembers this file.
_INVOCATION = re.compile(
    r"(?:uv run|uvx)(?:\s+--[\w-]+(?:[= ]\S+)?)*\s+(?P<run>[A-Za-z][\w.-]*)"
    r"|command -v (?P<checked>[A-Za-z][\w.-]*)"
)

#: Where a project that a gate runs in lives in this repo, plus the root. A
#: conventional config is read from the directory the tool runs in, and the
#: runners `cd` into all of these.
_PROJECT_DIRS = ("", "kew/", "frontend/", "www/", "create-kew/")

#: A config the tool DISCOVERS instead of being pointed at. `vite build` reads
#: `vite.config.ts` from the working directory and `svelte-check` reads
#: `svelte.config.js`; neither name is ever spelled by a runner, so the
#: file-naming scan above is structurally blind to the whole JS/TS tool-config
#: class — it stayed green with `svelte.config.js` unmatched, and stayed green
#: with the `**/vite.config.*` pattern deleted (#2192). These get their own
#: oracle: `<tool>.config.<ext>` is read by `<tool>`, so it is a gate
#: definition whenever a runner invokes `<tool>`.
_DISCOVERED_CONFIG = re.compile(r"^(?P<tool>[A-Za-z0-9_-]+)\.config\.[A-Za-z]+$")

#: Trees a gate READS AS ITS SUBJECT: the source it lints, the tests it runs,
#: the docs it publishes, the skills and project templates it ships. A runner
#: naming one of these names what is being checked, not the rules it is checked
#: by — holding a PR for editing them would hold every PR.
_SUBJECT_TREES = (
    ".claude/",
    "create-kew/",
    "docs/",
    # The canned API payloads `ui-check` and the design gallery render: the
    # page data under test, like `kew/tests/`. A gallery shot names its
    # scenario file by path, which is not a rule it obeys (#2280).
    "frontend/fixtures/",
    "frontend/src/",
    "kew/src/",
    "kew/tests/",
    "www/src/",
)

#: Individual files a runner names that are evidence a gate COMPARES AGAINST,
#: or that run after the gates rather than as one. Editing them is caught by the
#: gate itself, so none of them lets a PR redefine what proves it.
_NOT_A_GATE = {
    # The audit-chain anchors `kew audit verify` checks a run against.
    "audit-anchors.jsonl",
    # The committed schema `scripts/export_openapi.py --check` diffs the app's
    # live one against — the artefact under test, not the test.
    "kew/openapi.json",
    # The local-model eval harness's model list. It shares only a basename with
    # the catalog `www/scripts/fetch-model-lifecycle.mjs` reads
    # (`kew/src/kew/models.json`); no gate reads it.
    "kew/evals/local_models/models.json",
    # Publishes the docs site after CI is green; it gates nothing.
    "www/deploy.sh",
    # The demo's committed sample build history. The demo build ships it and
    # a local demo check compares the served copy with it, so an edit moves
    # both sides together; the rules live in the checks, not in this data.
    "frontend/demo/provenance/provenance.sample.json",
    # The project's licence text. No gate reads it: the runners name it only
    # inside PIP_LICENSES_VERSION, the license gate's tool pin, and the
    # license gate checks dependencies' licences, not this file.
    "LICENSE",
}


@functools.cache
def _runner_text() -> str:
    """Everything the gate runners say, concatenated — read once a session.

    The `package.json` `scripts` table is included because it is where a JS
    project declares its gates (`svelte-check --tsconfig ./tsconfig.json`);
    the dependency lists are not, or every package name would read as a path.

    The sweeps below ask the same questions of this one string for every
    tracked path, so every answer derived from it — the invoked tools, the
    conventional configs, the gate definitions — is computed once too. Asking
    per path made the two sweeps 40 s each, which was the whole cost of this
    module (#2340).
    """
    chunks: list[str] = []
    for path in tracked_paths():
        if _RUNNER_PATTERN.match(path):
            chunks.append((REPO_ROOT / path).read_text(errors="ignore"))
        elif PurePosixPath(path).name == "package.json":
            manifest = json.loads((REPO_ROOT / path).read_text())
            chunks.append(json.dumps(manifest.get("scripts", {})))
    return "\n".join(chunks)


def _named_by_a_runner(path: str, runner_text: str) -> bool:
    """A runner names this tracked file — by full path, or by a configuration
    basename (a gate is usually pointed at `.gitleaks.toml`, not at
    `kew/.gitleaks.toml`)."""
    name = PurePosixPath(path).name
    config_shaped = name.startswith(".") or name.endswith(
        _CONFIG_SUFFIXES + ("Dockerfile",)
    )
    return path in runner_text or (config_shaped and name in runner_text)


def _invoked(tool: str, runner_text: str) -> bool:
    """A runner spells *tool* as a command, not as part of a longer word — so
    `vite` is not credited by `vitest run`, and `uv` is not by `uvx`."""
    return re.search(rf"(?<![\w.-]){re.escape(tool)}(?![\w-])", runner_text) is not None


@functools.cache
def _conventional_names() -> frozenset[str]:
    """Every basename some invoked tool would obey if the PR added it."""
    runner_text = _runner_text()
    return frozenset(
        name
        for tool, names in _CONVENTIONAL_CONFIGS.items()
        if _invoked(tool, runner_text)
        for name in names
    )


@functools.cache
def _invoked_tools() -> frozenset[str]:
    """Every tool the runners actually launch, read off the invocations."""
    return frozenset(
        match.group("run") or match.group("checked")
        for match in _INVOCATION.finditer(_runner_text())
    )


def _discovered_by_a_runner(path: str, runner_text: str) -> bool:
    """A runner invokes the tool that reads this file by convention — `vite
    build` for `vite.config.ts`, `svelte-check` for `svelte.config.js`, `npm ci`
    for the `.npmrc` that decides what it installs."""
    name = PurePosixPath(path).name
    match = _DISCOVERED_CONFIG.match(name)
    return (match is not None and match.group("tool") in runner_text) or (
        name in _conventional_names()
    )


@functools.cache
def _gate_definitions() -> tuple[str, ...]:
    runner_text = _runner_text()
    return tuple(
        path
        for path in tracked_paths()
        if (
            _named_by_a_runner(path, runner_text)
            or _discovered_by_a_runner(path, runner_text)
        )
        and not path.endswith(".md")
        and not path.startswith(_SUBJECT_TREES)
        and path not in _NOT_A_GATE
    )


def test_every_gate_definition_in_this_repo_is_matched_by_the_defaults() -> None:
    """The shipped list is only a gate if it names the files this repo gates
    itself with. One unmatched file is one file a PR may rewrite and then be
    verified by."""
    patterns = DispatchConfig().epic.merge_gate_paths
    gate_files = _gate_definitions()

    # The scan has to be reading the tree it thinks it is. `.gitleaks.toml`,
    # `.gitleaksignore` and `mkdocs.yml` are the omitted-tool-config class the
    # restated-list version of this test could not see; the four
    # `*.config.{js,ts}` files are the same class one layer down, invisible to
    # a scan that only believes a config a runner spells (#2192).
    assert {
        "kew/.semgrep/pinned.yml",
        "kew/.xenon-excludes",
        ".gitleaks.toml",
        ".gitleaksignore",
        "mkdocs.yml",
        "frontend/svelte.config.js",
        "frontend/vite.config.ts",
        "www/svelte.config.js",
        "www/vite.config.ts",
    } <= set(gate_files)

    unmatched = [
        path for path in gate_files if not touched_gate_paths([path], patterns)
    ]
    assert unmatched == [], (
        "these gate definitions are not covered by [epic] merge_gate_paths, so a "
        "PR that edits one is verified by a gate it authored — add a pattern, or "
        f"record it in _NOT_A_GATE with the reason it is not one: {unmatched}"
    )


def test_a_conventional_config_is_covered_before_any_pr_adds_one() -> None:
    """The class neither tree-reading oracle above can see (#2192).

    `ruff` prefers a root `.ruff.toml` or `ruff.toml` to `pyproject.toml`, and
    `pytest` prefers `pytest.ini`, `tox.ini` or `setup.cfg`. None exists here,
    so both scans are silent about them — yet the PR that ADDS one is exactly
    the PR that must be held: `select = []` in a file the branch introduced
    turns the lint gate off, and the queue then verifies that branch green with
    the gate it just wrote. This asserts on the tools the runners invoke, not on
    the tree, so it fails the day a gate starts using a tool whose conventional
    config the patterns miss.
    """
    patterns = DispatchConfig().epic.merge_gate_paths
    runner_text = _runner_text()

    invoked = {tool for tool in _CONVENTIONAL_CONFIGS if _invoked(tool, runner_text)}
    # The oracle has to be reading the runners it thinks it is: if the token
    # scan silently matched nothing, every assertion below would be vacuous.
    assert {
        "ruff",
        "mypy",
        "pytest",
        "uv",
        "npm",
        "vitest",
        "semgrep",
        "trivy",
        "gitleaks",
    } <= invoked

    unmatched = sorted(
        f"{directory}{name}"
        for name in _conventional_names()
        for directory in _PROJECT_DIRS
        if not touched_gate_paths([f"{directory}{name}"], patterns)
    )
    assert unmatched == [], (
        "a tool this repo's gates invoke reads these by convention, so a PR that "
        "ADDS one redefines the gate that is about to prove it and is not held: "
        f"{unmatched}"
    )


def test_every_tool_a_runner_invokes_is_on_one_side_of_the_table() -> None:
    """What makes the table above a contract rather than a memory (#2192).

    The test above asserts that the conventional configs `_CONVENTIONAL_CONFIGS`
    names are matched — it cannot notice a tool the table never mentions. That
    is the shape the `.semgrepignore` hole had: the SAST gate was invoked, the
    patterns covered its pinned ruleset (`**/.semgrep/`) and nothing covered
    the ignore file it obeys regardless, so a PR adding one scanned nothing,
    was not held and was attested `result: pass` — under a green suite.

    So the roster comes from the runners, not from this file: every tool they
    launch must either name its conventional configs or be recorded as having
    none, with the reason. Adding a gate therefore breaks this until someone
    decides which side its tool is on.
    """
    tools = _invoked_tools()

    # The extraction has to be reading the invocations it thinks it is.
    assert {"ruff", "mypy", "bandit", "semgrep", "gitleaks", "trivy"} <= tools

    unclassified = sorted(
        tools - set(_CONVENTIONAL_CONFIGS) - set(_NO_CONVENTIONAL_CONFIG)
    )
    assert unclassified == [], (
        "a gate runner invokes these and nothing here says what they read: name "
        "the files each obeys in _CONVENTIONAL_CONFIGS (and cover them in [epic] "
        "merge_gate_paths), or record it in _NO_CONVENTIONAL_CONFIG with the "
        f"reason it reads nothing a PR could add: {unclassified}"
    )


def test_the_ratchet_ledgers_are_gate_definitions() -> None:
    """A ratchet ledger is what its policy test compares the tree against
    (#2493): a PR that raises its own allowance there is verified green by
    the allowance it wrote. So every ledger is held — read off the directory
    on disk, so a ledger added later is covered without editing this list."""
    patterns = DispatchConfig().epic.merge_gate_paths
    ledgers = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "kew" / "ratchets").rglob("*")
        if path.is_file()
    )

    assert {
        "kew/ratchets/test_quality.json",
        "kew/ratchets/silence_on_failure_baseline.txt",
    } <= set(ledgers), "the sweep is not reading the ratchet directory"
    assert [path for path in ledgers if not touched_gate_paths([path], patterns)] == []


def test_the_lockfiles_no_runner_names_are_still_covered() -> None:
    """The runner scan is the oracle, not the whole contract: nothing in a
    workflow spells `uv.lock`, yet a lockfile pins the ruff / bandit / semgrep /
    pytest versions the gates ARE. Both copies stay named explicitly."""
    patterns = DispatchConfig().epic.merge_gate_paths
    tracked = set(tracked_paths())

    lockfiles = {"uv.lock", "kew/uv.lock"}
    assert lockfiles <= tracked, "the sweep is not reading the repo root"
    assert [
        path for path in sorted(lockfiles) if not touched_gate_paths([path], patterns)
    ] == []


def test_the_defaults_do_not_hold_ordinary_source() -> None:
    """The other half of the contract: a check that held every PR would be
    turned off on its first day, so ordinary source must stay unmatched."""
    patterns = DispatchConfig().epic.merge_gate_paths
    gate_files = set(_gate_definitions())
    ordinary = [
        path
        for path in tracked_paths()
        if path.startswith(("kew/src/kew/", "kew/tests/", "docs/"))
        and path not in gate_files
        and "/scripts/" not in path
        and "/docker/" not in path
        and PurePosixPath(path).name != "conftest.py"
    ]

    assert len(ordinary) > 500, "the sweep is not reading the source tree"
    assert [path for path in ordinary if touched_gate_paths([path], patterns)] == []
