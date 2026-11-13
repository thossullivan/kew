"""A PR's merge verify runs the vitest and ui-check its change owns, behind
``[verify] frontend`` (#3213).

Two kinds of fixture. The selection's rules are read off a console-shaped
tree handed to :func:`kew.verify_frontend.select`: routes that import shared
files, and ui-check modules whose ``ui-check-owns:`` lines own those routes.
The replays of #2965, #3001, #2991, #2985, #3062 and #3085 change the paths
those PRs changed, against that fixture and again against this checkout's own
tree (:class:`TestReplaysOnTheRealTree`). The verify itself runs against a
fixture repository with an ``origin``, its host's job execution faked at the
process boundary (:meth:`LocalProvider._execute`), so what is checked is the
job script the host was handed, the ``verify_completed`` row and the CLI's
output.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from support.fake_gcloud import launchable
from support.held_out_log import opened
from support.verify_switches import SUITE_JOB_SWITCHES, environ_without_switches
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, load_config
from kew.drive_merge import browser_checked
from kew.hosts import Job, LocalProvider
from kew.main_health import NIGHTLY_KIND, read_main_health
from kew.merge_verify import (
    FRONTEND_ENV,
    PREFLIGHT_PASSED_MARK,
    STAGE_ENV,
    STAGE_SUITE,
    UI_CHECK_COMMAND,
    VERIFY_EVENT,
    run_verify,
    summary_line,
)
from kew.run_store import SqliteRunStore
from kew.ui_check_modules import glob_matches, owned_globs
from kew.verify_frontend import (
    FRONTEND_SCOPED,
    SHARED_LIB_READING_EXPORTS,
    Change,
    FrontendSelection,
    FrontendTree,
    select,
)

runner = CliRunner()

KEW_DIR = Path(__file__).resolve().parents[1]
VERIFY_SH = KEW_DIR / "scripts" / "verify.sh"

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)
_SOURCE = re.compile(r"\.(?:ts|js|mjs|svelte)$")


def _owns(*globs: str) -> str:
    lines = [f"// ui-check-owns: {glob}" for glob in globs]
    return "\n".join([*lines, "export async function checks() {}", ""])


def _unimported(path: str) -> str:
    """The full run's reason for a file no owned file reaches by import."""
    return (
        f"no ui-check module with ownership lines owns {path} or a file that imports it"
    )


def _unnamed(path: str) -> str:
    """The full run's reason for a static, fixture or demo data file no
    ownership line names."""
    return f"no ui-check module with ownership lines names {path}"


# --- the replays ---------------------------------------------------------------

#: The console, as far as the replayed PRs reach into it: each route imports
#: the shared files its surface renders.
_CONSOLE: dict[str, str] = {
    "frontend/src/routes/(factory)/work/+page.svelte": (
        "<script>\nimport RunButton from '$lib/RunButton.svelte';\n"
        "import Launchpad from '$lib/work/DispatchLaunchpad.svelte';\n"
        "import NeedsYou from '$lib/needs-you/NeedsYouBlock.svelte';\n"
        "import { rows } from '$lib/work/list';\n"
        "import { VERB } from '$lib/verbs';\n</script>\n"
    ),
    "frontend/src/lib/RunButton.svelte": (
        "<script>\nimport { request } from './run-request';\n</script>\n"
    ),
    "frontend/src/lib/run-request.ts": "import { NAMES } from './factory/names';\n",
    "frontend/src/lib/verbs.ts": "export const VERB = 'run';\n",
    "frontend/src/lib/work/DispatchLaunchpad.svelte": (
        "<script>\nimport { request } from '$lib/run-request.js';\n</script>\n"
    ),
    "frontend/src/lib/work/list.ts": "export const rows = [];\n",
    "frontend/src/lib/needs-you/NeedsYouBlock.svelte": "<p>needs you</p>\n",
    "frontend/src/lib/factory/names.ts": "export const NAMES = {};\n",
    "frontend/src/lib/factory/classes.ts": "export const ID = 'id';\n",
    "frontend/src/routes/(factory)/monitor/+page.svelte": (
        "<script>\nimport { line } from '$lib/monitor/event-line';\n"
        "import { open } from '$lib/monitor/stream';\n"
        "import { holds } from '$lib/monitor/holds';\n</script>\n"
    ),
    "frontend/src/lib/monitor/event-line.ts": (
        "import { NAMES } from '../factory/names';\n"
    ),
    "frontend/src/lib/monitor/stream.ts": "export const open = 1;\n",
    "frontend/src/lib/monitor/holds.ts": "export const holds = 1;\n",
    "frontend/src/routes/(factory)/health/+page.svelte": (
        "<script>\nimport { ID } from '$lib/factory/classes';\n</script>\n"
    ),
    "frontend/src/routes/(factory)/epics/+page.svelte": (
        "<script>\nimport { rail } from '$lib/epic/rail';\n</script>\n"
    ),
    "frontend/src/lib/epic/rail.ts": "export const rail = 1;\n",
    "frontend/src/routes/(factory)/prompts/+page.svelte": (
        "<script>\nimport Outcomes from '$lib/prompts/ProfileOutcomes.svelte';\n"
        "import { get } from '$lib/api';\n</script>\n"
    ),
    "frontend/src/lib/prompts/ProfileOutcomes.svelte": (
        "<script>\nimport { outcomes } from './outcomes';\n</script>\n"
    ),
    "frontend/src/lib/prompts/outcomes.ts": "export const outcomes = 1;\n",
    "frontend/src/lib/prompts/sections.ts": "import { outcomes } from './outcomes';\n",
    "frontend/src/lib/api.ts": "export const get = 1;\n",
    "frontend/src/lib/time.ts": "export const now = 1;\n",
    # vitest files: the ones #3085 touched or reached, and one it did not.
    "frontend/src/lib/prompts/outcomes.test.ts": (
        "import { outcomes } from './outcomes';\n"
    ),
    "frontend/src/lib/prompts/sections.test.ts": "import './sections';\n",
    "frontend/src/lib/fixtures.test.ts": (
        "import data from '../../fixtures/factory/prompts-ci_fix-outcomes.json';\n"
        "import { get } from '$lib/api';\n"
    ),
    "frontend/src/lib/time.test.ts": "import { now } from './time';\n",
    "frontend/fixtures/factory/prompts-ci_fix-outcomes.json": "{}\n",
    "frontend/fixtures/scenarios/dashboard-host-restart-repeated.json": "{}\n",
    "frontend/fixtures/README.md": "fixtures\n",
}

_ROUTES = "src/routes/(factory)"

#: The ui-check modules, each owning its surface. Ten more own routes the
#: replays never touch, as most of the real modules do.
_MODULES: dict[str, str] = {
    "work-rows": _owns(f"{_ROUTES}/work/**"),
    "launchpad-run-held": _owns(f"{_ROUTES}/work/**"),
    "run-queued-label": _owns(f"{_ROUTES}/work/**"),
    "needs-you-copy-held": _owns("src/lib/needs-you/**"),
    "monitor-takeover-danger": _owns(f"{_ROUTES}/monitor/**"),
    "health-runner-ids": _owns(f"{_ROUTES}/health/**"),
    "epics-list": _owns(f"{_ROUTES}/epics/**"),
    "host-restart-repeated": _owns("fixtures/scenarios/*host-restart-repeated*.json"),
    "host-restart-repeated-surfaces": _owns(
        "fixtures/scenarios/*host-restart-repeated*.json"
    ),
    "prompts-outcomes": _owns(f"{_ROUTES}/prompts/**", "fixtures/factory/prompts-*"),
    "status-words": _owns(f"{_ROUTES}/prompts/**"),
    **{
        f"{route}-page": _owns(f"{_ROUTES}/{route}/**")
        for route in (
            "budget",
            "config",
            "audit",
            "runs",
            "loops",
            "scorecard",
            "console",
            "recorder",
            "inbox",
            "dashboard",
        )
    },
}

_CHECKS = "frontend/scripts/ui-checks"


def _tree(
    files: Mapping[str, str], modules: Mapping[str, str] = _MODULES
) -> FrontendTree:
    every = {**files, **{f"{_CHECKS}/{n}.mjs": s for n, s in modules.items()}}
    return FrontendTree(
        files=frozenset(every),
        sources={p: t for p, t in every.items() if _SOURCE.search(p)},
        modules=dict(modules),
    )


def _changes(*paths: str, status: str = "M") -> list[Change]:
    return [Change(status, path) for path in paths]


def _select(changes: list[Change], tree: FrontendTree) -> FrontendSelection:
    """A change whose base and head agree on what the rules read off the
    base: the ownership map, the file-reading tests, the imports out of
    ``frontend/``."""
    return select(changes, tree, tree)


#: Each replayed PR's frontend change, and the ui-check module that failed on
#: its own surface.
_UI_REPLAYS = {
    "3001": (
        [
            f"{_CHECKS}/monitor-takeover-danger.mjs",
            "frontend/src/lib/factory/names.ts",
            "frontend/src/lib/monitor/event-line.test.ts",
            "frontend/src/lib/monitor/event-line.ts",
            "frontend/src/lib/monitor/stream.test.ts",
            "frontend/src/lib/monitor/stream.ts",
        ],
        "monitor-takeover-danger",
    ),
    "2991": (
        [
            f"{_CHECKS}/launchpad-run-held.mjs",
            f"{_CHECKS}/needs-you-copy-held.mjs",
            f"{_CHECKS}/run-queued-label.mjs",
            "frontend/src/lib/RunButton.svelte",
            "frontend/src/lib/factory/names.ts",
            "frontend/src/lib/needs-you/NeedsYouBlock.svelte",
            "frontend/src/lib/run-request.test.ts",
            "frontend/src/lib/run-request.ts",
            "frontend/src/lib/verbs.ts",
            "frontend/src/lib/work/DispatchLaunchpad.svelte",
            f"frontend/{_ROUTES}/work/+page.svelte",
        ],
        "launchpad-run-held",
    ),
    "2985": (
        [
            "frontend/fixtures/README.md",
            "frontend/fixtures/scenarios/dashboard-host-restart-repeated.json",
            f"{_CHECKS}/host-restart-repeated-surfaces.mjs",
            f"{_CHECKS}/host-restart-repeated.mjs",
            "frontend/src/lib/epic/rail.ts",
            "frontend/src/lib/monitor/holds.ts",
            "frontend/src/lib/work/list.ts",
        ],
        "host-restart-repeated-surfaces",
    ),
    "3062": (
        [
            f"{_CHECKS}/health-runner-ids.mjs",
            "frontend/src/lib/factory/classes.ts",
            f"frontend/{_ROUTES}/health/+page.svelte",
        ],
        "health-runner-ids",
    ),
}


#: #2965's frontend change: the runner, whose run-queued-label module failed.
_REPLAY_2965 = (
    "frontend/scripts/ui-check-lib.mjs",
    f"{_CHECKS}/run-queued-label.mjs",
    "frontend/src/lib/RunButton.svelte",
)
#: #3085's frontend change, whose prompts-outcomes module failed.
_REPLAY_3085 = (
    "frontend/fixtures/factory/prompts-ci_fix-outcomes.json",
    f"{_CHECKS}/prompts-outcomes.mjs",
    f"{_CHECKS}/status-words.mjs",
    "frontend/src/lib/api.ts",
    "frontend/src/lib/fixtures.test.ts",
    "frontend/src/lib/prompts/ProfileOutcomes.svelte",
    "frontend/src/lib/prompts/outcomes.test.ts",
    "frontend/src/lib/prompts/outcomes.ts",
    f"frontend/{_ROUTES}/prompts/+page.svelte",
)


class TestReplays:
    @pytest.mark.parametrize("pr", sorted(_UI_REPLAYS))
    def test_a_replay_selects_the_module_that_failed(self, pr: str) -> None:
        paths, failed = _UI_REPLAYS[pr]

        selection = _select(_changes(*paths), _tree(_CONSOLE))

        assert selection.scoped, selection.reason
        assert failed in selection.modules
        assert len(selection.modules) < len(_MODULES)

    def test_a_shared_file_selects_every_module_whose_surface_imports_it(
        self,
    ) -> None:
        # names.ts reaches the work route through run-request.ts and the
        # monitor route through event-line.ts; nothing else imports it.
        selection = _select(
            _changes("frontend/src/lib/factory/names.ts"), _tree(_CONSOLE)
        )

        assert selection.modules == (
            "launchpad-run-held",
            "monitor-takeover-danger",
            "run-queued-label",
            "work-rows",
        )

    def test_2965_changed_the_runner_so_it_runs_every_module(self) -> None:
        # #2965 changed ui-check-lib.mjs, which every module imports: the
        # failing run-queued-label runs because they all do.
        selection = _select(_changes(*_REPLAY_2965), _tree(_CONSOLE))

        assert not selection.scoped
        assert selection.reason == (
            "the ui-check runner: frontend/scripts/ui-check-lib.mjs"
        )

    def test_3085_selects_its_own_vitest_files_and_modules(self) -> None:
        selection = _select(_changes(*_REPLAY_3085), _tree(_CONSOLE))

        assert selection.scoped, selection.reason
        assert selection.modules == ("prompts-outcomes", "status-words")
        # What the job hands `vitest related`: every changed file there is.
        assert "src/lib/prompts/outcomes.ts" in selection.related
        assert "src/lib/fixtures.test.ts" in selection.related
        # The import graph's own reading of the vitest files that run.
        assert selection.tests == (
            "frontend/src/lib/fixtures.test.ts",
            "frontend/src/lib/prompts/outcomes.test.ts",
            "frontend/src/lib/prompts/sections.test.ts",
        )


class TestTheFullFallback:
    @pytest.mark.parametrize(
        ("path", "why"),
        [
            ("frontend/src/routes/+layout.svelte", "the app shell"),
            (f"frontend/{_ROUTES}/+layout.svelte", "the app shell"),
            ("frontend/src/lib/shell/FactoryHeader.svelte", "the app shell"),
            ("frontend/src/lib/ui/index.ts", "lib/ui"),
            ("frontend/src/app.css", "the design tokens"),
            ("frontend/fixtures/plugin.ts", "the fixtures loader"),
            ("frontend/package.json", "package.json or a lockfile"),
            ("frontend/package-lock.json", "package.json or a lockfile"),
            ("frontend/vite.config.ts", "the build config"),
        ],
    )
    def test_a_shared_root_runs_everything_and_says_why(
        self, path: str, why: str
    ) -> None:
        selection = _select(
            _changes("frontend/src/lib/verbs.ts", path), _tree(_CONSOLE)
        )

        assert not selection.scoped
        assert selection.reason == f"{why}: {path}"

    def test_a_surface_no_module_owns_or_imports_runs_everything(self) -> None:
        files = {**_CONSOLE, "frontend/src/lib/orphan.ts": "export const X = 1;\n"}

        selection = _select(_changes("frontend/src/lib/orphan.ts"), _tree(files))

        assert not selection.scoped
        assert selection.reason == _unimported("frontend/src/lib/orphan.ts")

    def test_a_module_with_no_ownership_line_runs_for_every_change(self) -> None:
        modules = {**_MODULES, "legacy-surface": "export async function checks() {}\n"}

        selection = _select(
            _changes("frontend/src/lib/factory/classes.ts"), _tree(_CONSOLE, modules)
        )

        assert selection.scoped, selection.reason
        assert selection.modules == ("health-runner-ids", "legacy-surface")

    def test_an_unmapped_module_never_covers_a_file_no_mapped_module_owns(
        self,
    ) -> None:
        # monitor.mjs runs for every change, yet never visits the epic page.
        page = f"frontend/{_ROUTES}/epics/[ref]/+page.ts"
        files = {**_CONSOLE, page: "export const load = () => ({});\n"}
        modules = {
            **{n: s for n, s in _MODULES.items() if n != "epics-list"},
            "monitor": "export async function checks() {}\n",
        }

        selection = _select(_changes(page), _tree(files, modules))

        assert not selection.scoped
        assert selection.reason == _unimported(page)

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/fixtures/factory/epics-go-live-2026-09.json",
            "frontend/demo/world/epics.json",
            "frontend/static/favicon.svg",
            "frontend/fixtures/x/EPICS.JSON",
            "frontend/fixtures/x/rows.jsonl",
            "frontend/demo/x/data.json5",
        ],
    )
    def test_data_no_module_names_runs_everything_though_an_owned_file_imports_it(
        self, path: str
    ) -> None:
        # The work page imports it, yet a page fetches its data over HTTP:
        # only an ownership line naming the file proves a module loads it.
        route = f"frontend/{_ROUTES}/work/+page.svelte"
        files = {
            **_CONSOLE,
            path: "{}\n",
            route: _CONSOLE[route]
            + f"<script>import d from '../../../../{path.removeprefix('frontend/')}';"
            "</script>\n",
        }
        modules = {**_MODULES, "monitor": "export async function checks() {}\n"}

        selection = _select(_changes(path), _tree(files, modules))

        assert not selection.scoped
        assert selection.reason == _unnamed(path)

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/static/app.test.js",
            "frontend/static/README.md",
            "frontend/fixtures/x/epics.test.json",
            "frontend/demo/world/epics.spec.json",
        ],
    )
    def test_a_test_or_prose_name_never_spares_served_data_a_naming_module(
        self, path: str
    ) -> None:
        # A unit test or prose under src/ renders nothing; a file under
        # static/ is served whatever its name, and data is fetched by path.
        selection = _select(_changes(path), _tree({**_CONSOLE, path: "{}\n"}))

        assert not selection.scoped
        assert selection.reason == _unnamed(path)

    @pytest.mark.parametrize(
        "path",
        [
            "frontend/static/app.test.js",
            "frontend/fixtures/x/epics.test.json",
            "frontend/demo/world/epics.spec.json",
            "frontend/fixtures/x/EPICS.JSON",
        ],
    )
    def test_served_data_with_a_test_name_selects_the_module_that_names_it(
        self, path: str
    ) -> None:
        modules = {**_MODULES, "data-reader": _owns(path.removeprefix("frontend/"))}
        tree = _tree({**_CONSOLE, path: "{}\n"}, modules)

        selection = _select(_changes(path), tree)

        assert selection.scoped, selection.reason
        assert selection.modules == ("data-reader",)

    def test_a_fixture_selects_the_modules_that_name_it_beside_an_unmapped_one(
        self,
    ) -> None:
        fixture = "frontend/fixtures/scenarios/dashboard-host-restart-repeated.json"
        modules = {**_MODULES, "monitor": "export async function checks() {}\n"}

        selection = _select(_changes(fixture), _tree(_CONSOLE, modules))

        assert selection.scoped, selection.reason
        assert selection.modules == (
            "host-restart-repeated",
            "host-restart-repeated-surfaces",
            "monitor",
        )

    def test_a_selection_of_70_percent_or_more_runs_everything(self) -> None:
        modules = {
            name: _MODULES[name]
            for name in ("work-rows", "run-queued-label", "health-runner-ids")
        }
        modules["monitor"] = _owns(f"{_ROUTES}/monitor/**")

        selection = _select(
            _changes("frontend/src/lib/factory/names.ts"), _tree(_CONSOLE, modules)
        )

        assert not selection.scoped
        assert selection.reason == "the selection is 3 of 4 modules, 70% or more"

    def test_a_deleted_source_file_runs_everything(self) -> None:
        selection = _select(
            _changes("frontend/src/lib/verbs.ts", status="D"), _tree(_CONSOLE)
        )

        assert not selection.scoped
        assert "frontend/src/lib/verbs.ts" in selection.reason

    def test_a_change_with_no_frontend_file_runs_vitest_whole(self) -> None:
        selection = _select(_changes("kew/src/kew/cli.py"), _tree(_CONSOLE))

        assert not selection.scoped
        assert "touches nothing under frontend/" in selection.reason

    def test_a_test_only_change_runs_no_module(self) -> None:
        selection = _select(_changes("frontend/src/lib/time.test.ts"), _tree(_CONSOLE))

        assert selection.scoped
        assert selection.modules == ()
        assert selection.related == ("src/lib/time.test.ts",)


# --- the verify ------------------------------------------------------------------

_ROUTE_WORK = f"frontend/{_ROUTES}/work/+page.svelte"
_ROUTE_BUDGET = f"frontend/{_ROUTES}/budget/+page.svelte"
_SOURCE_READ_TEST = "frontend/src/lib/source-read.test.ts"

_PROJECT = {
    "kew/scripts/verify.sh": 'echo "fixture verify.sh ran"\n',
    "kew/src/kew/__init__.py": "",
    # Read by frontend code from outside frontend/, as the model catalog is.
    "kew/src/kew/models.json": "{}\n",
    "frontend/package.json": "{}\n",
    "frontend/src/routes/+layout.svelte": "<slot />\n",
    "frontend/src/lib/names.ts": (
        "import { models } from '../../../kew/src/kew/models.json';\n"
        "export const NAMES = {};\n"
    ),
    "frontend/src/lib/names.test.ts": "import { NAMES } from './names';\n",
    # Reads the budget page as text: no import reaches it.
    _SOURCE_READ_TEST: (
        "import { readFileSync } from 'node:fs';\n"
        f"const page = readFileSync('{_ROUTES}/budget/+page.svelte', 'utf8');\n"
    ),
    "frontend/scripts/helper.mjs": "export const HELP = 1;\n",
    _ROUTE_WORK: "<script>\nimport { NAMES } from '$lib/names';\n</script>\n",
    f"frontend/{_ROUTES}/health/+page.svelte": "<p>health</p>\n",
    _ROUTE_BUDGET: "<p>budget</p>\n",
    f"{_CHECKS}/work-rows.mjs": _owns(f"{_ROUTES}/work/**"),
    f"{_CHECKS}/health.mjs": _owns(f"{_ROUTES}/health/**"),
    f"{_CHECKS}/budget.mjs": _owns(f"{_ROUTES}/budget/**"),
}

_NAMES_CHANGE = {"frontend/src/lib/names.ts": "export const NAMES = { a: 1 };\n"}
_SHELL_CHANGE = {"frontend/src/routes/+layout.svelte": "<main><slot /></main>\n"}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _write(repo: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


class Project:
    """The fixture project, its origin, and its branches."""

    def __init__(self, root: Path, origin: Path) -> None:
        self.root = root
        origin.mkdir(parents=True, exist_ok=True)
        _git(origin, "init", "--quiet", "--bare", "-b", "main", ".")
        _git(root, "init", "--quiet", "-b", "main", ".")
        (root / ".git" / "info" / "exclude").write_text(
            "logs/\nkew.toml\nhost-locks/\n", encoding="utf-8"
        )
        _write(root, _PROJECT)
        _git(root, "add", "--all")
        _git(root, "commit", "--quiet", "-m", "base")
        _git(root, "remote", "add", "origin", str(origin))
        self.push()

    def push(self) -> None:
        _git(self.root, "push", "--quiet", "origin", "main")
        _git(self.root, "fetch", "--quiet", "origin")

    def branch(
        self, name: str, files: Mapping[str, str], remove: tuple[str, ...] = ()
    ) -> str:
        _git(self.root, "checkout", "--quiet", "-b", name, "main")
        _write(self.root, files)
        for rel in remove:
            (self.root / rel).unlink()
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "--quiet", "-m", name)
        sha = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "--quiet", "main")
        return sha

    def land(self, files: Mapping[str, str], message: str) -> None:
        _write(self.root, files)
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "--quiet", "-m", message)
        self.push()

    def squash_land(self, sha: str) -> str:
        """*sha*'s tree, landed on main as a new commit, as a squash merge does."""
        tree = _git(self.root, "rev-parse", f"{sha}^{{tree}}")
        landed = _git(self.root, "commit-tree", tree, "-p", "main", "-m", "land")
        _git(self.root, "update-ref", "refs/heads/main", landed)
        _git(self.root, "checkout", "--quiet", "-f", "main")
        self.push()
        return landed


class FakeHost:
    """The verify host's job execution: every job passes."""

    def __init__(self) -> None:
        self.jobs: list[str] = []

    def __call__(self, provider: LocalProvider, job: Job) -> tuple[int, str]:
        self.jobs.append(job.command)
        return 0, f"{PREFLIGHT_PASSED_MARK}\ngate passed\n{_HELD_OUT_PASSED}"


@pytest.fixture
def project(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Project:
    return Project(tmp_path, tmp_path_factory.mktemp("origin") / "origin.git")


def _config(
    project: Project, monkeypatch: pytest.MonkeyPatch, frontend: str | None
) -> DispatchConfig:
    toml = project.root / "kew.toml"
    line = "" if frontend is None else f'frontend = "{frontend}"\n'
    toml.write_text(
        '[project]\nrepo = "o/r"\n\n'
        f'[logging]\ndir = "{project.root / "logs"}"\n\n'
        f'[verify]\nhost = "local"\n{line}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(project.root))
    return load_config(toml)


@pytest.fixture
def scoped(project: Project, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    return _config(project, monkeypatch, FRONTEND_SCOPED)


@pytest.fixture
def default(project: Project, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    return _config(project, monkeypatch, None)


@pytest.fixture
def host(project: Project, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    fake = FakeHost()

    def _execute(provider: LocalProvider, job: Job) -> tuple[int, str]:
        return fake(provider, job)

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    monkeypatch.chdir(project.root)
    return fake


@pytest.fixture
def store(project: Project) -> Iterator[SqliteRunStore]:
    log_dir = project.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as opened_store:
        yield opened_store


def _recorded(store: SqliteRunStore) -> dict[str, Any]:
    """The newest ``verify_completed`` payload's frontend selection."""
    events = sorted(
        store.list_audit_events(None, event=VERIFY_EVENT), key=lambda e: e.id
    )
    payload = events[-1].payload
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    return payload["frontend_selection"]


def _nightly(config: DispatchConfig, store: SqliteRunStore, project: Project) -> None:
    """A green full verify of main, as the nightly records one."""
    run_verify(
        config,
        store=store,
        ref="origin/main",
        repo_root=project.root,
        kind=NIGHTLY_KIND,
    )


class TestTheVerify:
    def test_a_scoped_verify_runs_the_owned_module_and_vitest_related(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("names", _NAMES_CHANGE)

        candidate, result = run_verify(
            scoped, store=store, ref=sha, repo_root=project.root
        )

        assert result.outcome == "passed", result.log
        lines = host.jobs[-1].splitlines()
        assert (
            "npx --no-install vitest related --run --passWithNoTests src/lib/names.ts "
            "src/lib/source-read.test.ts" in lines
        )
        assert f"{UI_CHECK_COMMAND} -- --builds work-rows" in lines
        assert " frontend=scoped modules=1" in summary_line(candidate, result)
        recorded = _recorded(store)
        assert recorded["mode"] == "scoped"
        assert recorded["modules"] == ["work-rows"]
        assert recorded["related"] == ["src/lib/names.ts"]
        assert recorded["always"] == ["src/lib/source-read.test.ts"]
        assert recorded["reason"] == "1 of 3 ui-check modules own the change"
        assert re.fullmatch(r"[0-9a-f]{64}", recorded["digest"])

    def test_an_app_shell_change_runs_the_full_ui_check_and_says_why(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("shell", _SHELL_CHANGE)
        store.close()

        result = runner.invoke(app, ["verify", sha])

        assert result.exit_code == 0, result.output
        assert (
            "kew verify: frontend: full (the app shell: "
            "frontend/src/routes/+layout.svelte)" in result.output
        )
        assert "frontend=full" in result.output
        lines = host.jobs[-1].splitlines()
        assert UI_CHECK_COMMAND in lines
        assert FRONTEND_ENV not in host.jobs[-1]
        assert "vitest related" not in host.jobs[-1]

    def test_with_the_setting_off_the_verify_runs_what_it_runs_today(
        self,
        project: Project,
        default: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(default, store, project)
        sha = project.branch("names", _NAMES_CHANGE)

        candidate, result = run_verify(
            default, store=store, ref=sha, repo_root=project.root
        )

        command = host.jobs[-1]
        assert command.splitlines().count(UI_CHECK_COMMAND) == 1
        assert FRONTEND_ENV not in command
        assert "vitest" not in command
        assert "--builds" not in command
        assert "frontend=" not in summary_line(candidate, result)
        assert _recorded(store) == {
            "mode": "full",
            "reason": '[verify] frontend = "full"',
            "modules": [],
            "related": [],
            "always": [],
            "digest": None,
            "selector_version": None,
        }

    def test_with_no_nightly_on_record_the_frontend_runs_whole(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        sha = project.branch("names", _NAMES_CHANGE)

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        assert _recorded(store)["mode"] == "full"
        assert _recorded(store)["reason"].startswith("the nightly is missing")
        assert FRONTEND_ENV not in host.jobs[-1]

    def test_the_nightly_runs_the_full_vitest_and_ui_check(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)

        recorded = _recorded(store)
        assert recorded["mode"] == "full"
        assert recorded["reason"] == "the nightly verifies main in full"
        assert host.jobs[-1].splitlines().count(UI_CHECK_COMMAND) == 1

    def test_an_unreadable_ownership_map_runs_everything(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        _git(project.root, "checkout", "--quiet", "-b", "link", "main")
        module = project.root / _CHECKS / "budget.mjs"
        module.unlink()
        module.symlink_to("/etc/hostname")
        _write(project.root, _NAMES_CHANGE)
        _git(project.root, "add", "--all")
        _git(project.root, "commit", "--quiet", "-m", "link")
        sha = _git(project.root, "rev-parse", "HEAD")
        _git(project.root, "checkout", "--quiet", "main")

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        reason = _recorded(store)["reason"]
        assert reason.startswith("the ui-check ownership map cannot be read: ")
        assert "budget.mjs is no regular file" in reason


# --- what a scoped verify may never run too little of ---------------------------


def _payload(store: SqliteRunStore) -> dict[str, Any]:
    """The newest ``verify_completed`` payload."""
    events = sorted(
        store.list_audit_events(None, event=VERIFY_EVENT), key=lambda e: e.id
    )
    payload = events[-1].payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _vitest_line(host: FakeHost) -> str:
    return next(line for line in host.jobs[-1].splitlines() if "vitest" in line)


class TestTheFileReadingTests:
    """``vitest related`` follows imports only: a test that reads files runs
    in every scoped verify, as ``-m policy`` runs in every selected suite."""

    def test_a_test_reading_the_changed_page_runs_though_nothing_imports_it(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("budget", {_ROUTE_BUDGET: "<p>budget!</p>\n"})

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        recorded = _recorded(store)
        assert recorded["mode"] == "scoped", recorded["reason"]
        assert recorded["modules"] == ["budget"]
        assert recorded["always"] == ["src/lib/source-read.test.ts"]
        assert _vitest_line(host).endswith(" src/lib/source-read.test.ts")

    @pytest.mark.parametrize(
        "text",
        [
            "import { readFileSync } from 'fs';\n",
            "import { readFile } from 'node:fs/promises';\n",
            "const pages = import.meta.glob('../routes/**/*.svelte');\n",
            "import page from '../routes/+page.svelte?raw';\n",
            "const at = new URL('../routes/+page.svelte', import.meta.url);\n",
            "const fs = await import(`node:fs`);\n",
            "const fs = require(`fs`);\n",
            "import fse from 'fs-extra';\n",
            "import fs from 'graceful-fs';\n",
            "import fg from 'fast-glob';\n",
            "import { glob } from 'tinyglobby';\n",
            "import { spawnSync } from 'node:child_process';\n",
        ],
        ids=[
            "fs",
            "fs-promises",
            "glob",
            "raw",
            "url",
            "backtick-import",
            "backtick-require",
            "fs-extra",
            "graceful-fs",
            "fast-glob",
            "tinyglobby",
            "child-process",
        ],
    )
    def test_every_way_of_reading_a_file_runs_the_test(self, text: str) -> None:
        files = {**_CONSOLE, "frontend/src/lib/reads.test.ts": text}

        selection = _select(_changes("frontend/src/lib/time.test.ts"), _tree(files))

        assert selection.scoped, selection.reason
        assert selection.always == ("src/lib/reads.test.ts",)
        # The sizing estimate counts it: it runs.
        assert "frontend/src/lib/reads.test.ts" in selection.tests

    def test_a_test_whose_helper_reads_files_runs_too(self) -> None:
        files = {
            **_CONSOLE,
            "frontend/src/lib/support/source.ts": (
                "import { readFileSync } from 'node:fs';\n"
                "export const read = readFileSync;\n"
            ),
            "frontend/src/lib/uses-source.test.ts": (
                "import { read } from './support/source';\n"
            ),
        }

        selection = _select(_changes("frontend/src/lib/time.test.ts"), _tree(files))

        assert selection.always == ("src/lib/uses-source.test.ts",)

    def test_they_are_read_off_the_base_not_the_candidate(self) -> None:
        reads = "frontend/src/lib/reads.test.ts"
        base = _tree({**_CONSOLE, reads: "import { readFileSync } from 'fs';\n"})
        head = _tree({**_CONSOLE, reads: "export {};\n"})

        selection = select(_changes("frontend/src/lib/time.test.ts"), head, base)

        assert selection.always == ("src/lib/reads.test.ts",)


class TestModulesThatReachTheChangeThemselves:
    """A ui-check module runs when its own file reaches a changed file, and
    always when it reads files: its ownership lines say neither."""

    def test_a_changed_helper_runs_every_module_that_imports_it(self) -> None:
        files = {**_CONSOLE, "frontend/scripts/gallery.mjs": "export const X = 1;\n"}
        modules = {
            **_MODULES,
            "gallery-shots": (
                "import { X } from '../gallery.mjs';\n" + _owns(f"{_ROUTES}/x/**")
            ),
            "gallery-live": (
                "import { X } from '../gallery.mjs';\n" + _owns(f"{_ROUTES}/y/**")
            ),
        }

        selection = _select(
            _changes("frontend/scripts/gallery.mjs"), _tree(files, modules)
        )

        assert selection.scoped, selection.reason
        assert selection.modules == ("gallery-live", "gallery-shots")

    def test_a_changed_module_runs_every_module_that_imports_it(self) -> None:
        modules = {
            **_MODULES,
            "m4": "import { step } from './m5.mjs';\n" + _owns(f"{_ROUTES}/x/**"),
            "m5": "export const step = 1;\n" + _owns(f"{_ROUTES}/y/**"),
        }

        selection = _select(_changes(f"{_CHECKS}/m5.mjs"), _tree(_CONSOLE, modules))

        assert selection.scoped, selection.reason
        assert selection.modules == ("m4", "m5")

    def test_a_module_that_reads_a_changed_fixture_runs(self) -> None:
        dashboard = "frontend/fixtures/factory/dashboard.json"
        files = {**_CONSOLE, dashboard: "{}\n"}
        modules = {
            **_MODULES,
            "dashboard-json": _owns("fixtures/factory/dashboard.json"),
            # Owns the work route, yet reads the dashboard fixture as text.
            "work-rows": (
                "import { readFileSync } from 'node:fs';\n"
                "const d = readFileSync('fixtures/factory/dashboard.json');\n"
                + _owns(f"{_ROUTES}/work/**")
            ),
        }

        selection = _select(_changes(dashboard), _tree(files, modules))

        assert selection.scoped, selection.reason
        assert selection.modules == ("dashboard-json", "work-rows")

    def test_a_module_whose_helper_reads_files_runs_for_any_change(self) -> None:
        files = {
            **_CONSOLE,
            "frontend/scripts/fixture-reader.mjs": (
                "import { readFileSync } from 'node:fs';\nexport const r = 1;\n"
            ),
        }
        modules = {
            **_MODULES,
            "reads-fixtures": (
                "import { r } from '../fixture-reader.mjs';\n"
                + _owns(f"{_ROUTES}/x/**")
            ),
        }

        selection = _select(
            _changes("frontend/src/lib/factory/classes.ts"), _tree(files, modules)
        )

        assert selection.scoped, selection.reason
        assert selection.modules == ("health-runner-ids", "reads-fixtures")


class TestFileReaders:
    """The shared ui-check library reads files, yet a module importing it
    for a constant reads nothing itself: ownership lines decide its run."""

    _LIB = "frontend/scripts/ui-check-lib.mjs"
    _LIB_TEXT = (
        "import { readFileSync } from 'node:fs';\n"
        "const here = new URL('.', import.meta.url);\n"
        "export const SETTLE_MS = 50;\n"
    )

    def _modules(self) -> dict[str, str]:
        return {
            **_MODULES,
            "settles": (
                "import { SETTLE_MS } from '../ui-check-lib.mjs';\n"
                + _owns(f"{_ROUTES}/x/**")
            ),
        }

    def test_a_module_that_imports_the_shared_helper_is_not_a_reader(self) -> None:
        tree = _tree({**_CONSOLE, self._LIB: self._LIB_TEXT}, self._modules())

        selection = _select(_changes("frontend/src/lib/factory/classes.ts"), tree)

        assert selection.scoped, selection.reason
        assert selection.modules == ("health-runner-ids",)

    def test_a_module_reading_through_another_helper_still_runs(self) -> None:
        files = {
            **_CONSOLE,
            self._LIB: self._LIB_TEXT,
            "frontend/scripts/fixture-reader.mjs": (
                "import { SETTLE_MS } from './ui-check-lib.mjs';\n"
                "import { readFileSync } from 'node:fs';\nexport const r = 1;\n"
            ),
        }
        modules = {
            **self._modules(),
            "reads-fixtures": (
                "import { SETTLE_MS } from '../ui-check-lib.mjs';\n"
                "import { r } from '../fixture-reader.mjs';\n"
                + _owns(f"{_ROUTES}/y/**")
            ),
        }

        selection = _select(
            _changes("frontend/src/lib/factory/classes.ts"), _tree(files, modules)
        )

        assert selection.scoped, selection.reason
        assert selection.modules == ("health-runner-ids", "reads-fixtures")

    def test_a_change_to_the_shared_helper_still_runs_everything(self) -> None:
        tree = _tree({**_CONSOLE, self._LIB: self._LIB_TEXT}, self._modules())

        selection = _select(_changes(self._LIB), tree)

        assert not selection.scoped
        assert "the ui-check runner" in selection.reason

    def _runs_for_an_unrelated_change(self, imports: str) -> bool:
        """Whether a module importing the library as *imports* does, and so
        runs, when an unrelated change is selected."""
        modules = {
            **self._modules(),
            "tour-words": imports + _owns(f"{_ROUTES}/z/**"),
        }
        tree = _tree({**_CONSOLE, self._LIB: self._LIB_TEXT}, modules)

        selection = _select(_changes("frontend/src/lib/factory/classes.ts"), tree)

        assert selection.scoped, selection.reason
        return "tour-words" in selection.modules

    @pytest.mark.parametrize(
        "imports",
        [
            "import { docsPassage } from '../ui-check-lib.mjs';\n",
            "import { FRONTEND } from '../ui-check-lib.mjs';\n",
            "import { SETTLE_MS, buildLeaks as leaks } from '../ui-check-lib.mjs';\n",
            "import {\n  SETTLE_MS,\n  runChecks,\n} from '../ui-check-lib';\n",
            "import * as lib from '../ui-check-lib.mjs';\n",
            "const lib = await import('../ui-check-lib.mjs');\n",
            "export { SETTLE_MS } from '../ui-check-lib.mjs';\n",
            "import { SETTLE_MS } from '../ui-check-lib.mjs';\n"
            "import { docsPassage } from '../ui-check-lib.mjs';\n",
        ],
    )
    def test_a_module_using_what_the_library_reads_is_a_reader(
        self, imports: str
    ) -> None:
        assert self._runs_for_an_unrelated_change(imports)

    def test_a_module_importing_only_the_librarys_constants_is_not_a_reader(
        self,
    ) -> None:
        imports = "import { SETTLE_MS, widestBox } from '../ui-check-lib.mjs';\n"

        assert not self._runs_for_an_unrelated_change(imports)

    def test_a_helper_using_what_the_library_reads_makes_its_importers_readers(
        self,
    ) -> None:
        files = {
            **_CONSOLE,
            self._LIB: self._LIB_TEXT,
            "frontend/scripts/tour.mjs": (
                "import { docsPassage } from './ui-check-lib.mjs';\n"
                "export const words = 1;\n"
            ),
        }
        modules = {
            **self._modules(),
            "tour-words": (
                "import { words } from '../tour.mjs';\n" + _owns(f"{_ROUTES}/z/**")
            ),
        }

        selection = _select(
            _changes("frontend/src/lib/factory/classes.ts"), _tree(files, modules)
        )

        assert selection.scoped, selection.reason
        assert selection.modules == ("health-runner-ids", "tour-words")


#: A top-level declaration of an ES module, at column 0.
_JS_DECLARATION = re.compile(
    r"^(export\s+)?(?:async\s+)?(?:function\*?|const|let|var|class)\s+([\w$]+)",
    re.MULTILINE,
)
#: Comments that start a line: a docstring may name a reading export.
_JS_COMMENT = re.compile(r"^\s*/\*[\s\S]*?\*/|^\s*//[^\n]*", re.MULTILINE)
_FS_IMPORT = re.compile(
    r"""\bimport\s*\{([^}]*)\}\s*from\s*['"](?:node:)?"""
    r"""(?:fs|fs/promises|child_process)['"]"""
)
#: What reads a file or names one by itself: an ``fs`` binding, a path
#: resolved against the module's own URL, a dynamic import.
_JS_READS = (r"\bimport\.meta\.url\b", r"\bimport\s*\(")


def _reading_exports(source: str) -> set[str]:
    """The exports of an ES module that read files or build a path to one,
    themselves or through a top-level binding that does."""
    text = _JS_COMMENT.sub("", source)
    found = list(_JS_DECLARATION.finditer(text))
    bounds = [m.start() for m in found] + [len(text)]
    bodies = {
        m.group(2): text[m.end() : end]
        for m, end in zip(found, bounds[1:], strict=True)
    }
    exported = {m.group(2) for m in found if m.group(1)}
    fs_names = {
        part.split()[-1]
        for clause in _FS_IMPORT.findall(text)
        for part in clause.split(",")
        if part.strip()
    }
    seeds = [*_JS_READS, *(rf"\b{re.escape(name)}\b" for name in fs_names)]
    seed = re.compile("|".join(seeds))
    reading = {name for name, body in bodies.items() if seed.search(body)}
    grew = True
    while grew:
        more = {
            name
            for name, body in bodies.items()
            if name not in reading
            if any(re.search(rf"(?<![\w$.]){re.escape(r)}\b", body) for r in reading)
        }
        reading |= more
        grew = bool(more)
    return reading & exported


class TestTheSharedLibrarysReadingExports:
    _LIB = KEW_DIR.parent / "frontend" / "scripts" / "ui-check-lib.mjs"

    @pytest.mark.policy
    def test_every_export_of_the_library_that_reads_files_is_listed(self) -> None:
        source = self._LIB.read_text(encoding="utf-8")
        # Only declarations are read: an export list or re-export would hide.
        assert not re.search(
            r"^export\s*(?:\{|\*|default\b)|^export\s+(?:const|let|var)\s*[{\[]",
            source,
            re.MULTILINE,
        ), "teach _reading_exports this export form"

        reading = _reading_exports(source)

        assert {"FRONTEND", "docsPassage", "runChecks"} <= reading
        assert reading <= SHARED_LIB_READING_EXPORTS, sorted(
            reading - SHARED_LIB_READING_EXPORTS
        )

    def test_the_lint_finds_an_export_that_reads_through_a_local_binding(
        self,
    ) -> None:
        source = (
            "import { readFileSync as read } from 'node:fs';\n"
            "/** FRONTEND, in a comment, reads nothing. */\n"
            "export const SETTLE_MS = 1;\n"
            "const HERE = new URL('.', import.meta.url);\n"
            "function load(name) {\n  return read(name);\n}\n"
            "export function passage(page) {\n  return load(new URL(page, HERE));\n}\n"
            "export const pick = (x) => x.load;\n"
            "export async function every(dir) {\n  return import(dir);\n}\n"
        )

        assert _reading_exports(source) == {"passage", "every"}


class TestCoverageIsTheBasesGraph:
    """An import the change adds to an existing file covers nothing."""

    def test_an_import_added_to_an_owned_route_covers_nothing(self) -> None:
        route = f"frontend/{_ROUTES}/work/+page.svelte"
        base = _tree(_CONSOLE)
        head = _tree(
            {
                **_CONSOLE,
                route: _CONSOLE[route] + "<script>import '$lib/time';</script>\n",
                "frontend/src/lib/time.ts": "export const now = 2;\n",
            }
        )

        selection = select(_changes(route, "frontend/src/lib/time.ts"), head, base)

        assert not selection.scoped
        assert selection.reason == _unimported("frontend/src/lib/time.ts")

    def test_an_owned_file_the_change_adds_covers_what_it_imports(self) -> None:
        added = f"frontend/{_ROUTES}/work/new/+page.svelte"
        base = _tree(_CONSOLE)
        head = _tree(
            {
                **_CONSOLE,
                added: "<script>import { now } from '$lib/time';</script>\n",
                "frontend/src/lib/time.ts": "export const now = 2;\n",
            }
        )
        changes = [Change("A", added), Change("M", "frontend/src/lib/time.ts")]

        selection = select(changes, head, base)

        assert selection.scoped, selection.reason
        assert "work-rows" in selection.modules

    def test_a_fixture_only_an_owned_unit_test_imports_runs_everything(
        self,
    ) -> None:
        # work-rows owns the work route's folder, unit test included; the
        # test imports the fixture, but the modules that render it load the
        # page and get the fixture over HTTP, never by import.
        fixture = "frontend/fixtures/factory/dashboard-work.json"
        files = {
            **_CONSOLE,
            fixture: "{}\n",
            f"frontend/{_ROUTES}/work/page.load.test.ts": (
                "import data from '../../../../fixtures/factory/dashboard-work.json';\n"
            ),
        }
        tree = _tree(files)

        selection = _select(_changes(fixture), tree)

        assert not selection.scoped
        assert selection.reason == _unnamed(fixture)


class TestWhatAScopedVerifyAttests:
    def test_a_scoped_verify_that_lands_is_never_main_health_evidence(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        nightly = _git(project.root, "rev-parse", "origin/main")
        sha = project.branch("names", _NAMES_CHANGE)
        candidate, result = run_verify(
            scoped, store=store, ref=sha, repo_root=project.root
        )
        assert candidate.frontend.scoped and result.outcome == "passed"

        project.squash_land(sha)
        health = read_main_health(store, project.root, base_ref="origin/main")

        # Main still reads green from the nightly's commit, never the landing.
        assert (health.sha, health.via) == (nightly, NIGHTLY_KIND)

    def test_a_full_frontend_verify_that_lands_is_evidence(
        self,
        project: Project,
        default: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(default, store, project)
        sha = project.branch("names", _NAMES_CHANGE)
        run_verify(default, store=store, ref=sha, repo_root=project.root)

        landed = project.squash_land(sha)
        health = read_main_health(store, project.root, base_ref="origin/main")

        assert (health.sha, health.via) == (landed, "pr")

    def test_a_scoped_ui_check_of_no_module_is_no_browser_check(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch(
            "test-only",
            {"frontend/src/lib/names.test.ts": "import { NAMES } from './names';\n\n"},
        )

        candidate, result = run_verify(
            scoped, store=store, ref=sha, repo_root=project.root
        )

        assert candidate.frontend.scoped and candidate.frontend.modules == ()
        assert result.outcome == "passed"
        assert f"{UI_CHECK_COMMAND} -- --builds" in host.jobs[-1].splitlines()
        assert not browser_checked(store, sha)
        payload = _payload(store)
        assert (payload["ui_check"], payload["ui_check_modules"]) == (True, [])

    def test_a_scoped_ui_check_of_a_module_is_a_browser_check(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("names", _NAMES_CHANGE)

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        assert _payload(store)["ui_check_modules"] == ["work-rows"]
        assert browser_checked(store, sha)

    def test_a_full_ui_check_is_a_browser_check(
        self,
        project: Project,
        default: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
    ) -> None:
        _nightly(default, store, project)
        sha = project.branch("names", _NAMES_CHANGE)

        run_verify(default, store=store, ref=sha, repo_root=project.root)

        assert _payload(store)["ui_check_modules"] is None
        assert browser_checked(store, sha)


class TestChangesNoGraphTraces:
    @pytest.mark.parametrize(
        ("files", "remove", "why"),
        [
            (
                {"frontend/.npmrc": "engine-strict=true\n", **_NAMES_CHANGE},
                (),
                "a frontend root dotfile: frontend/.npmrc",
            ),
            (
                {"kew/src/kew/models.json": '{"m": 1}\n', **_NAMES_CHANGE},
                (),
                "frontend code imports or reads a changed file outside frontend/: "
                "kew/src/kew/models.json",
            ),
            (
                _NAMES_CHANGE,
                ("frontend/scripts/helper.mjs",),
                "a deletion under frontend/, whose importers and readers cannot "
                "be traced: frontend/scripts/helper.mjs",
            ),
        ],
        ids=["npmrc", "models-json", "deleted-script"],
    )
    def test_it_runs_the_full_frontend_and_says_why(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        files: Mapping[str, str],
        remove: tuple[str, ...],
        why: str,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("untraced", files, remove)

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        recorded = _recorded(store)
        assert (recorded["mode"], recorded["reason"]) == ("full", why)
        assert host.jobs[-1].splitlines().count(UI_CHECK_COMMAND) == 1
        assert FRONTEND_ENV not in host.jobs[-1]

    def test_a_file_named_against_import_meta_url_counts_as_imported(self) -> None:
        files = {
            **_CONSOLE,
            "frontend/src/lib/catalog.ts": (
                "export const AT = new URL('../../../kew/src/kew/models.json', "
                "import.meta.url);\n"
            ),
        }
        changes = _changes("kew/src/kew/models.json", "frontend/src/lib/time.test.ts")

        selection = _select(changes, _tree(files))

        assert not selection.scoped
        assert selection.reason.endswith("outside frontend/: kew/src/kew/models.json")

    def test_a_vitest_file_that_reads_one_runs_instead(self) -> None:
        # The test reads the catalog: it runs in every scoped verify, so the
        # catalog's change needs no full run on its account.
        files = {
            **_CONSOLE,
            "frontend/src/lib/catalog.test.ts": (
                "const at = new URL('../../../kew/src/kew/models.json', "
                "import.meta.url);\n"
            ),
        }
        changes = _changes("kew/src/kew/models.json", "frontend/src/lib/time.test.ts")

        selection = _select(changes, _tree(files))

        assert selection.scoped, selection.reason
        assert selection.always == ("src/lib/catalog.test.ts",)


class TestTheCandidatesOwnModules:
    """Ownership is the base's: a module the candidate adds runs, and covers
    nothing."""

    @pytest.mark.parametrize(
        "module",
        [_owns("src/**"), "export async function checks() {}\n"],
        ids=["owns-everything", "no-ownership-line"],
    )
    def test_an_added_module_never_covers_the_change_that_adds_it(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        module: str,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch(
            "zz",
            {
                f"{_CHECKS}/zz.mjs": module,
                "frontend/src/lib/orphan.ts": "export const X = 1;\n",
            },
        )

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        recorded = _recorded(store)
        assert recorded["mode"] == "full"
        assert recorded["reason"] == _unimported("frontend/src/lib/orphan.ts")

    @pytest.mark.parametrize(
        ("files", "modules"),
        [
            ({f"{_CHECKS}/zz.mjs": _owns("src/**")}, "work-rows zz"),
            (
                {f"{_CHECKS}/health.mjs": _owns(f"{_ROUTES}/health/**") + "// x\n"},
                "health work-rows",
            ),
        ],
        ids=["added", "edited"],
    )
    def test_a_module_the_change_adds_or_edits_runs(
        self,
        project: Project,
        scoped: DispatchConfig,
        host: FakeHost,
        store: SqliteRunStore,
        files: Mapping[str, str],
        modules: str,
    ) -> None:
        _nightly(scoped, store, project)
        sha = project.branch("module", {**files, **_NAMES_CHANGE})

        run_verify(scoped, store=store, ref=sha, repo_root=project.root)

        assert _recorded(store)["mode"] == "scoped", _recorded(store)["reason"]
        assert f"{UI_CHECK_COMMAND} -- --builds {modules}" in (
            host.jobs[-1].splitlines()
        )


def test_only_verify_sh_is_told_to_skip_its_vitest_run(
    project: Project,
    scoped: DispatchConfig,
    host: FakeHost,
    store: SqliteRunStore,
) -> None:
    _nightly(scoped, store, project)
    sha = project.branch("names", _NAMES_CHANGE)

    run_verify(scoped, store=store, ref=sha, repo_root=project.root)

    # Set on the suite job's verify.sh command line, beside the stage, never
    # exported: the phases after it (red-main) never see it, and the gate job,
    # which runs the static gates and held-out, never sets it.
    gate, suite = host.jobs[-2:]
    assert FRONTEND_ENV not in gate
    told = [line for line in suite.splitlines() if FRONTEND_ENV in line]
    assert told == [
        f"{STAGE_ENV}={STAGE_SUITE} {FRONTEND_ENV}=scoped bash scripts/verify.sh"
        ' || verify_status="$?"'
    ]


class TestTheSizingReport:
    def test_it_prints_the_median_and_p75(
        self, project: Project, default: DispatchConfig
    ) -> None:
        # Three frontend commits on main: one module of three, one again,
        # and the app shell, which runs everything; one backend commit between.
        project.land(_NAMES_CHANGE, "names")
        project.land({"kew/src/kew/leaf.py": "X = 1\n"}, "backend")
        project.land(
            {f"frontend/{_ROUTES}/health/+page.svelte": "<p>health!</p>\n"}, "health"
        )
        project.land(_SHELL_CHANGE, "shell")

        result = runner.invoke(app, ["verify", "--frontend-report", "--last", "3"])

        assert result.exit_code == 0, result.output
        assert "frontend sizing: 3 frontend commits on main" in result.output
        assert "full: the app shell: frontend/src/routes/+layout.svelte" in (
            result.output
        )
        # The file-reading test counts in every scoped estimate: it always runs.
        assert "scoped: ui-check 1/3 (health), vitest 1/2" in result.output
        assert "scoped: ui-check 1/3 (work-rows), vitest 2/2" in result.output
        # ui-check shares 1/3, 1/3, 1: median 33.3%, p75 66.7%.
        assert "ui-check modules: median 33.3%, p75 66.7%" in result.output
        # vitest shares 1, 1/2, 1 (the health page imports no test's file).
        assert "vitest files (import-graph estimate): median 100.0%, p75 100.0%" in (
            result.output
        )
        assert "full fallbacks: 1 of 3" in result.output

    def test_last_alone_is_refused(self) -> None:
        result = runner.invoke(app, ["verify", "--last", "3"])

        assert result.exit_code == 2
        assert "add one of them" in result.output

    def test_it_asks_a_different_question_from_the_test_report(self) -> None:
        result = runner.invoke(
            app, ["verify", "--frontend-report", "--selection-report"]
        )

        assert result.exit_code == 2
        assert "ask different questions; pick one" in result.output


class TestTheVerifyShSwitch:
    """verify.sh skips its whole vitest run for ``scoped``, and only then."""

    @staticmethod
    def _stub_bin(tmp_path: Path) -> Path:
        root = tmp_path if launchable(tmp_path) else Path(os.environ["HOME"])
        bin_dir = Path(root) / f"stub-{os.getpid()}-{id(tmp_path)}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for tool in ("uv", "uvx", "gitleaks", "npm"):
            (bin_dir / tool).write_text(
                f'#!/bin/sh\necho "{tool} $*" >> "$KEW_STUB_LOG"\nexit 0\n',
                encoding="utf-8",
            )
        # check-docs.sh refuses a docs build that leaves no site/index.html, so
        # the stubbed build writes one; a real `mkdocs build` cleans site/ first.
        (bin_dir / "uvx").write_text(
            '#!/bin/sh\necho "uvx $*" >> "$KEW_STUB_LOG"\n'
            'case " $* " in *" mkdocs build "*)\n'
            "    mkdir -p site\n"
            "    [ -e site/index.html ] || : > site/index.html ;;\n"
            "esac\nexit 0\n",
            encoding="utf-8",
        )
        for entry in bin_dir.iterdir():
            entry.chmod(0o755)
        return bin_dir

    def _run(self, tmp_path: Path, frontend: str) -> tuple[str, str]:
        bin_dir = self._stub_bin(tmp_path)
        log = tmp_path / "stub.log"
        log.write_text("", encoding="utf-8")
        environment = environ_without_switches()
        environment.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            KEW_STUB_LOG=str(log),
            KEW_VERIFY_IMAGE_SCAN="skip",
            KEW_VERIFY_SUITE="elsewhere",
        )
        environment[FRONTEND_ENV] = frontend
        completed = subprocess.run(
            ["bash", str(VERIFY_SH)],
            cwd=KEW_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        return log.read_text(encoding="utf-8"), completed.stderr

    @pytest.mark.parametrize(
        "ambient", [{}, SUITE_JOB_SWITCHES], ids=["clean", "inside-a-suite-job"]
    )
    def test_scoped_skips_only_the_whole_vitest_run_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ambient: dict[str, str],
    ) -> None:
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        seen, stderr = self._run(tmp_path, "scoped")

        assert "npm run check" in seen, seen
        assert "npm test -- --run" not in seen
        assert "npm run gates" in seen, "the www gates still ran"
        assert "uv run ruff check ." in seen, "the static gates still ran"
        assert "KEW_VERIFY_FRONTEND=scoped" in stderr

    @pytest.mark.parametrize(
        "ambient",
        [{}, {**SUITE_JOB_SWITCHES, FRONTEND_ENV: "scoped"}],
        ids=["clean", "inside-a-scoped-suite-job"],
    )
    def test_a_typo_runs_the_whole_vitest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ambient: dict[str, str],
    ) -> None:
        for name, value in ambient.items():
            monkeypatch.setenv(name, value)
        seen, _ = self._run(tmp_path, "scopd")

        assert "npm test -- --run" in seen, seen


def test_a_scoped_selection_describes_itself() -> None:
    selection = FrontendSelection(
        mode="scoped",
        reason="1 of 3 ui-check modules own the change",
        modules=("work-rows",),
        related=("src/lib/names.ts",),
        always=("src/lib/source-read.test.ts",),
        scoped_setting=True,
        total_modules=3,
    )

    assert selection.describe() == (
        "frontend: scoped, ui-check 1 of 3 modules, vitest related on 1 changed "
        "files and 1 file-reading tests (1 of 3 ui-check modules own the change); "
        "svelte-check and the builds run whole"
    )


# --- the real tree's ownership lines -------------------------------------------

#: For each ui-check module with ``ui-check-owns:`` lines, a real file (relative
#: to ``frontend/``) each of its lines owns. A change to any one of them alone
#: selects the module (#3409), so a module that loses an ownership line it still
#: needs fails here, even while another module owns the file. A new ownership
#: line records a file it owns here; a moved file is recorded again.
_OWNED_SAMPLES: dict[str, tuple[str, ...]] = {
    "audit-lines": (
        "src/routes/(factory)/audit/+page.svelte",
        "src/lib/audit-row.ts",
        "fixtures/factory/audit.json",
    ),
    "button-sm-type": (
        "src/lib/factory/classes.ts",
        "src/lib/ui/Button.svelte",
        "src/lib/prompts/ProfileEditor.svelte",
    ),
    "command-colours": (
        "src/lib/ui/Command.svelte",
        "src/app.css",
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "command-lines": (
        "src/lib/ui/Command.svelte",
        "src/lib/ui/command-markup.ts",
    ),
    "config-model-current": (
        "src/routes/(factory)/config/+page.svelte",
        "src/lib/ModelPicker.svelte",
    ),
    "config-model-picker": (
        "src/routes/(factory)/config/+page.svelte",
        "src/lib/ModelPicker.svelte",
        "src/lib/model-picker.ts",
        "src/lib/config-page.ts",
    ),
    "config-model-picker-edges": (
        "src/routes/(factory)/config/+page.svelte",
        "src/lib/ModelPicker.svelte",
    ),
    "declaration-hold": (
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "demo-follow-an-epic": (
        "demo/tours/follow-an-epic.json",
        "src/lib/epic/replay.ts",
    ),
    "demo-live-run": (
        "src/lib/demo/respond.ts",
        "fixtures/core.ts",
        "demo/world/runs-ab4656b0-audit.json",
    ),
    "demo-loops": (
        "src/routes/(factory)/loops/+page.svelte",
        "demo/world/loops.json",
    ),
    "demo-plex-arrows": (
        "src/lib/fonts/ibm-plex-mono-arrows-400-normal.woff2",
        "demo/vite.demo.config.ts",
    ),
    "demo-provenance": (
        "src/lib/demo/BuildHistory.svelte",
        "src/lib/demo/build-history.ts",
    ),
    "demo-replay": (
        "src/lib/epic/replay.ts",
        "src/lib/epic/EpicTimeline.svelte",
    ),
    "demo-run-events": (
        "src/lib/runs/RunEvents.svelte",
        "src/lib/runs/event-words.ts",
    ),
    "demo-run-page": (
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/lib/flight-recorder.ts",
    ),
    "demo-static": (
        "src/lib/demo/install.ts",
        "src/lib/demo/respond.ts",
        "src/lib/demo/writes.ts",
        "src/lib/demo/DemoBanner.svelte",
        "src/lib/demo/banner.js",
    ),
    "demo-tour-money": ("demo/tours/where-did-the-money-go.json",),
    "demo-tour-needs-you": (
        "demo/tours/needs-you.json",
        "src/routes/(factory)/monitor/+page.svelte",
    ),
    "demo-tour-refused-pr": ("demo/tours/refused-pr.json",),
    "demo-tours": (
        "src/lib/demo/tours/controller.ts",
        "demo/tours/needs-you.json",
    ),
    "demo-work-console-actions": (
        "src/lib/work/OrphanAckControl.svelte",
        "src/lib/work/QueuePicker.svelte",
        "demo/world/dashboard.json",
    ),
    "demo-world": (
        "demo/world/manifest.json",
        "demo/leak-scan.allow.json",
    ),
    "dialog-stack": (
        "src/lib/ui/contained.ts",
        "src/lib/keys/registry.ts",
        "src/lib/palette/Palette.svelte",
        "src/lib/shell/HelpOverlay.svelte",
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/routes/(factory)/config/+page.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "dispatch-plan": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "epic-arrows": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-canvas": (
        "src/lib/epic/DagView.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-done-groups": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
    ),
    "epic-fit": (
        "src/lib/epic/EpicTimeline.svelte",
        "src/lib/epic/canvas-view.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-graph": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
        "fixtures/factory/epics-sweep-2026-10-tick-strip.json",
    ),
    "epic-legend": (
        "src/lib/epic/DagView.svelte",
        "src/lib/epic/DagNode.svelte",
        "src/lib/epic/ReadyRing.svelte",
        "src/lib/epic/LineageBox.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-lineage": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-node-link": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-popover-more": (
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-popover-pinned": (
        "src/lib/epic/NodePopover.svelte",
        "src/lib/epic/node-popover.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "src/lib/epic/DagView.svelte",
    ),
    "epic-popover-title": (
        "src/lib/epic/NodePopover.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-rail": (
        "src/lib/epic/SelectedRail.svelte",
        "src/lib/epic/rail.ts",
        "src/lib/epic/live.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "src/lib/needs-you/NeedsYouBlock.svelte",
    ),
    "epic-replay-url": (
        "src/lib/epic/replay.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-tick-log": (
        "src/lib/epic/EpicTimeline.svelte",
        "src/lib/epic/timeline.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epic-tick-strip": (
        "src/lib/factory/tick-strip.ts",
        "src/lib/factory/TickTimeline.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epics-list": ("src/routes/(factory)/epics/+page.svelte",),
    "epics-list-readings": (
        "src/routes/(factory)/epics/+page.svelte",
        "src/lib/epic/roster.ts",
        "src/routes/(factory)/epics/+page.ts",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "src/routes/(factory)/epics/[ref]/+page.ts",
        "src/lib/factory/TickStrip.svelte",
        "fixtures/factory/epics.json",
        "fixtures/factory/epics-go-live-2026-09-nodes-SFD-229.json",
        "fixtures/factory/epics-launch-fixes-nodes-2055.json",
    ),
    "epics-roster-poll": (
        "src/routes/(factory)/epics/+page.svelte",
        "src/lib/epic/roster-poll.ts",
    ),
    "fixture-world": (
        "src/routes/(factory)/scorecard/+page.svelte",
        "src/routes/(factory)/loops/+page.svelte",
        "fixtures/factory/report-models.json",
        "fixtures/factory/report-outcomes.json",
        "fixtures/factory/loops.json",
    ),
    "gallery-live-linear": (
        "scripts/gallery.mjs",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
    ),
    "gallery-shots": (
        "scripts/gallery.mjs",
        "src/lib/palette/Palette.svelte",
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/routes/(factory)/config/+page.svelte",
        "src/lib/epic/DagView.svelte",
    ),
    "header-counts": (
        "src/lib/shell/FactoryHeader.svelte",
        "src/lib/health.ts",
        "src/routes/(factory)/health/+page.svelte",
    ),
    "health-controls": (
        "src/routes/(factory)/health/+page.svelte",
        "src/lib/health.ts",
        "src/routes/(factory)/health/+page.ts",
        "fixtures/factory/readiness.json",
    ),
    "health-runner-ids": ("src/routes/(factory)/health/+page.svelte",),
    "hidden-tab": (
        "src/lib/shell/visibility.svelte.ts",
        "src/lib/shell/pulse.svelte.ts",
    ),
    "host-restart-preserved-continue": (
        "fixtures/scenarios/host-restart-preserved-continue.json",
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/lib/monitor/Holds.svelte",
        "src/lib/epic/NodePopover.svelte",
        "src/lib/epic/SelectedRail.svelte",
    ),
    "host-restart-repeated": (
        "fixtures/scenarios/dashboard-host-restart-repeated.json",
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/list.ts",
        "src/lib/needs-you/NeedsYouBlock.svelte",
    ),
    "host-restart-repeated-surfaces": (
        "fixtures/scenarios/dashboard-launch-fixes-host-restart-repeated.json",
        "src/lib/needs-you/kind.ts",
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/monitor/Holds.svelte",
        "src/lib/epic/NodePopover.svelte",
        "src/lib/epic/SelectedRail.svelte",
    ),
    "launchpad": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/work/launchpad.ts",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "launchpad-drawer-type": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/ModelPicker.svelte",
        "src/routes/(factory)/config/+page.svelte",
        "src/routes/(factory)/health/+page.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "launchpad-model": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/ModelPicker.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "launchpad-model-runs": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/ModelPicker.svelte",
        "src/lib/model-picker.ts",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "launchpad-run-held": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/RunButton.svelte",
        "src/lib/run-request.ts",
        "src/routes/(factory)/work/+page.svelte",
        "src/routes/(factory)/monitor/+page.svelte",
    ),
    "launchpad-run-verb": (
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/lib/model-picker.ts",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "launchpad-where": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/DispatchLaunchpad.svelte",
    ),
    "merge-queue-stopped": (
        "src/lib/work/list.ts",
        "src/lib/shell/header-state.ts",
        "src/lib/monitor/holds.ts",
    ),
    "modal-keys": (
        "src/lib/palette/Palette.svelte",
        "src/lib/keys/registry.ts",
        "src/lib/shell/HelpOverlay.svelte",
        "src/lib/monitor/OverviewTrack.svelte",
        "src/routes/(factory)/monitor/+page.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "monitor": (
        "src/routes/(factory)/monitor/+page.svelte",
        "src/lib/monitor/EventStream.svelte",
        "src/lib/monitor/readouts.ts",
        "fixtures/factory/audit.json",
    ),
    "monitor-merge-queue-attention": (
        "src/routes/(factory)/monitor/+page.svelte",
        "src/lib/monitor/merge-queue-attention.ts",
        "src/lib/monitor/EventStream.svelte",
        "src/lib/monitor/Holds.svelte",
        "src/lib/shell/FactoryHeader.svelte",
        "src/lib/work/list.ts",
        "src/routes/(factory)/work/+page.svelte",
        "fixtures/factory/audit.json",
    ),
    "monitor-plain-lines": (
        "src/routes/(factory)/monitor/+page.svelte",
        "src/lib/monitor/EventStream.svelte",
        "src/lib/monitor/event-line.ts",
        "src/lib/monitor/Holds.svelte",
        "src/lib/monitor/readouts.ts",
        "fixtures/factory/audit.json",
    ),
    "monitor-readings": (
        "src/routes/(factory)/monitor/+page.svelte",
        "src/lib/monitor/readouts.ts",
        "src/lib/monitor/LiveRuns.svelte",
        "src/lib/monitor/live-runs.ts",
        "src/lib/monitor/stream.ts",
        "fixtures/factory/audit.json",
    ),
    "monitor-readout-fit": (
        "src/routes/(factory)/monitor/+page.svelte",
        "src/lib/monitor/readouts.ts",
        "src/lib/ui/Gauge.svelte",
        "src/lib/factory/gauge.ts",
        "src/routes/(factory)/budget/+page.svelte",
    ),
    "monitor-takeover-danger": (
        "src/lib/monitor/EventStream.svelte",
        "src/lib/monitor/stream.ts",
        "src/lib/factory/names.ts",
        "src/routes/(factory)/monitor/+page.svelte",
        "fixtures/factory/audit.json",
    ),
    "needs-you": (
        "src/lib/needs-you/summary.ts",
        "src/lib/needs-you/block.ts",
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/lib/epic/SelectedRail.svelte",
        "src/lib/epic/DagNode.svelte",
        "src/lib/monitor/Holds.svelte",
        "src/lib/work/entry-detail.ts",
    ),
    "needs-you-copy-held": (
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "needs-you-counts": (
        "src/lib/shell/header-state.ts",
        "src/lib/shell/FactoryHeader.svelte",
        "src/lib/epic/attention.ts",
        "src/routes/(factory)/epics/+page.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
    ),
    "needs-you-working": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/monitor/Holds.svelte",
        "src/lib/monitor/LiveRuns.svelte",
        "src/routes/(factory)/epics/+page.svelte",
        "fixtures/scenarios/dashboard-work-held-round-due.json",
    ),
    "palette": ("src/lib/palette/Palette.svelte",),
    "phone-chips-and-runner-ids": (
        "src/lib/runs/RunEvents.svelte",
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/routes/(factory)/health/+page.svelte",
    ),
    "phone-filters": (
        "src/lib/runs/RunEvents.svelte",
        "src/lib/palette/Palette.svelte",
    ),
    "phone-width": (
        "src/lib/needs-you/NeedsYouBlock.svelte",
        "src/lib/ui/Command.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "src/lib/epic/DagView.svelte",
        "src/lib/epic/NodePopover.svelte",
        "src/lib/runs/RunEvents.svelte",
    ),
    "plex-arrows": (
        "src/app.css",
        "src/lib/fonts/ibm-plex-mono-arrows-400-normal.woff2",
    ),
    "plain-names": (
        "src/lib/factory/names.ts",
        "src/lib/monitor/live-runs.ts",
        "src/lib/runs/RunEvents.svelte",
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/lib/monitor/LiveRuns.svelte",
        "src/routes/(factory)/monitor/+page.svelte",
    ),
    "prompts-display": (
        "src/routes/(factory)/prompts/+page.svelte",
        "src/lib/prompts/ProfileEditor.svelte",
        "src/lib/prompts/source-chip.ts",
        "fixtures/scenarios/prompts-versions.json",
    ),
    "prompts-outcomes": (
        "src/lib/prompts/ProfileOutcomes.svelte",
        "src/lib/prompts/outcomes.ts",
        "src/lib/prompts/ProfileEditor.svelte",
        "fixtures/scenarios/prompts-outcomes.json",
        "fixtures/factory/prompts-ci_fix-outcomes.json",
        "src/routes/(factory)/prompts/+page.svelte",
    ),
    "prompts-page": (
        "src/routes/(factory)/prompts/+page.svelte",
        "src/routes/(factory)/prompts/+page.ts",
        "src/lib/prompts/sections.ts",
    ),
    "prompts-profile-attention": (
        "src/routes/(factory)/prompts/+page.svelte",
        "src/lib/prompts/profile-state.ts",
        "src/lib/prompts/ProfileEditor.svelte",
    ),
    "prompts-write": (
        "src/routes/(factory)/prompts/+page.svelte",
        "src/lib/prompts/ProfileEditor.svelte",
        "src/lib/prompts/sections.ts",
    ),
    "routes": (
        "src/routes/(factory)/work/+page.svelte",
        "src/routes/(factory)/monitor/+page.svelte",
        "src/routes/(factory)/epics/+page.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/routes/+error.svelte",
        "src/lib/theme.svelte.ts",
    ),
    "run-events": (
        "src/lib/runs/RunEvents.svelte",
        "src/lib/runs/event-words.ts",
        "src/lib/runs/events.ts",
    ),
    "run-page": (
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/lib/runs/verdict.ts",
    ),
    "run-page-layout": (
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/lib/flight-recorder.ts",
        "src/lib/ui/CopyButton.svelte",
    ),
    "run-queued-label": (
        "src/lib/RunButton.svelte",
        "src/lib/work/DispatchLaunchpad.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "runs-budget-phone": (
        "src/routes/(factory)/runs/+page.svelte",
        "src/routes/(factory)/budget/+page.svelte",
        "src/routes/(factory)/scorecard/+page.svelte",
        "src/lib/ui/Readout.svelte",
    ),
    "runs-error-category": (
        "src/routes/(factory)/runs/+page.svelte",
        "src/lib/factory/names.ts",
    ),
    "shell-header": (
        "src/lib/shell/FactoryHeader.svelte",
        "src/lib/shell/header-state.ts",
    ),
    "shell-nav": (
        "src/lib/shell/FactoryHeader.svelte",
        "src/lib/shell/nav.ts",
    ),
    "signal-and-labels": (
        "src/app.css",
        "src/lib/factory/names.ts",
        "src/lib/monitor/EventStream.svelte",
        "src/routes/(factory)/work/+page.svelte",
        "src/routes/(factory)/health/+page.svelte",
        "src/routes/(factory)/epics/+page.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "src/routes/(factory)/runs/[id]/+page.svelte",
        "src/routes/(factory)/monitor/+page.svelte",
        "src/routes/(factory)/scorecard/+page.svelte",
        "fixtures/factory/audit.json",
    ),
    "status-words": ("src/lib/ui/StatusMark.svelte",),
    "work-console-actions": (
        "src/lib/work/OrphanAckControl.svelte",
        "src/lib/work/orphan-ack.ts",
        "src/lib/work/QueuePicker.svelte",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "work-first-run": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/FirstRunCard.svelte",
        "src/lib/work/first-run.ts",
    ),
    "work-items": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/Pipeline.svelte",
    ),
    "work-keys": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/shell/HelpOverlay.svelte",
        "src/lib/keys/registry.ts",
        "src/lib/work/keys.ts",
    ),
    "work-merge-reason": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/list.ts",
    ),
    "work-merge-verify-lead": (
        "src/lib/work/MergeVerify.svelte",
        "src/lib/work/entry-detail.ts",
        "src/lib/work/list.ts",
        "src/lib/monitor/holds.ts",
        "src/lib/epic/rail.ts",
        "src/lib/needs-you/moves.ts",
    ),
    "work-pipeline": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/Pipeline.svelte",
        "src/lib/ui/Swimlane.svelte",
        "src/lib/factory/swimlane.ts",
        "src/lib/factory/status.ts",
        "src/lib/ui/StatusMark.svelte",
        "src/lib/work/pipeline.ts",
    ),
    "work-read-failure": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/list.ts",
    ),
    "work-reread": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/reads.ts",
        "src/lib/work/Pipeline.svelte",
        "src/lib/work/EvidenceLedger.svelte",
    ),
    "work-reread-order": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/reads.ts",
        "src/lib/work/EvidenceLedger.svelte",
    ),
    "work-reread-single": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/reads.ts",
    ),
    "work-rows": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/work/entry-detail.ts",
        "src/lib/shell/FactoryHeader.svelte",
    ),
    "work-shell": (
        "src/routes/(factory)/work/+page.ts",
        "src/routes/(factory)/work/+page.svelte",
    ),
    "work-titles": (
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/ui/Title.svelte",
        "src/lib/factory/names.ts",
        "src/lib/epic/DagView.svelte",
        "src/routes/(factory)/epics/[ref]/+page.svelte",
        "src/lib/palette/Palette.svelte",
    ),
    "work-verify": (
        "src/lib/work/MergeVerify.svelte",
        "src/lib/work/merge-verify.ts",
        "src/routes/(factory)/work/+page.svelte",
        "src/lib/factory/names.ts",
    ),
}
#: Each recorded file, with the modules a change to it must select.
_SAMPLE_OWNERS: dict[str, frozenset[str]] = {
    sample: frozenset(n for n, files in _OWNED_SAMPLES.items() if sample in files)
    for sample in {f for files in _OWNED_SAMPLES.values() for f in files}
}
_REPO = KEW_DIR.parent
#: The sources the import graph reads, as :mod:`kew.verify_frontend` reads them.
_GRAPH_SOURCE = re.compile(r"\.(?:ts|js|mjs|cjs|svelte)$")


@pytest.fixture(scope="module")
def checkout() -> FrontendTree:
    """This checkout's ``frontend/`` as a commit of it would hold it: tracked
    and untracked files, the ignored ones aside, read off the disk."""
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "frontend",
        ],
        cwd=_REPO,
        check=True,
        capture_output=True,
    ).stdout.decode()
    files = sorted({p for p in listed.split("\0") if p and (_REPO / p).is_file()})
    sources = {
        path: (_REPO / path).read_text(encoding="utf-8", errors="replace")
        for path in files
        if _GRAPH_SOURCE.search(path)
    }
    modules = {
        posixpath.basename(path).removesuffix(".mjs"): sources[path]
        for path in files
        if posixpath.dirname(path) == _CHECKS and path.endswith(".mjs")
    }
    return FrontendTree(files=frozenset(files), sources=sources, modules=modules)


def _mapped_and_padded(tree: FrontendTree) -> dict[str, str]:
    """The modules with ownership lines, beside twice as many that own no file.

    A module with none runs for every change, and a selection of
    :data:`~kew.verify_frontend.WHOLE_SHARE` of the map or more runs them all:
    either would run a module whatever its lines say. The padding keeps every
    selection under that share, so only ownership lines and the import graph
    select a module here.
    """
    mapped = {name: text for name, text in tree.modules.items() if owned_globs(text)}
    padding = {f"padding-{i}": _owns("padding/none") for i in range(2 * len(mapped))}
    return {**mapped, **padding}


class TestEveryOwnershipLineSelectsItsModule:
    """The real modules' ownership lines, read off this checkout."""

    @pytest.mark.policy
    def test_every_ownership_line_has_a_recorded_file(
        self, checkout: FrontendTree
    ) -> None:
        unrecorded = [
            f"{name}: {glob}"
            for name, text in sorted(checkout.modules.items())
            for glob in owned_globs(text)
            if not any(glob_matches(glob, f) for f in _OWNED_SAMPLES.get(name, ()))
        ]
        gone = sorted(set(_OWNED_SAMPLES) - set(checkout.modules))

        assert not unrecorded, (
            f"record in _OWNED_SAMPLES a real file each line owns: {unrecorded}"
        )
        assert not gone, f"_OWNED_SAMPLES records modules that are gone: {gone}"

    @pytest.mark.policy
    @pytest.mark.parametrize("sample", sorted(_SAMPLE_OWNERS))
    def test_a_change_to_an_owned_file_alone_selects_its_modules(
        self, checkout: FrontendTree, sample: str
    ) -> None:
        path = f"frontend/{sample}"
        assert path in checkout.files, f"{sample} is gone: record another file"

        selection = select(
            [Change("M", path)],
            checkout,
            checkout,
            modules=_mapped_and_padded(checkout),
        )

        # A full run runs every module; a scoped one must hold every owner.
        picked = set(selection.modules) if selection.scoped else _SAMPLE_OWNERS[sample]
        missing = sorted(_SAMPLE_OWNERS[sample] - picked)
        assert not missing, f"a change to {sample} alone leaves out {missing}"


#: The six replays, each PR's frontend change beside the module that failed.
_REAL_REPLAYS: dict[str, tuple[tuple[str, ...], str]] = {
    **{pr: (tuple(paths), failed) for pr, (paths, failed) in _UI_REPLAYS.items()},
    "2965": (_REPLAY_2965, "run-queued-label"),
    "3085": (_REPLAY_3085, "prompts-outcomes"),
}
_RUNNER = "frontend/scripts/ui-check-lib.mjs"
#: The replays that leave the ui-check runner alone. #2965 changed it, and its
#: only other surface file, ``lib/RunButton.svelte``, is shared.
_REAL_TREE_REPLAYS_OFF_THE_RUNNER = sorted(
    pr for pr, (paths, _) in _REAL_REPLAYS.items() if _RUNNER not in paths
)
#: Each replayed PR's own source changes: what it changed of the surfaces, not
#: the failing module itself, a vitest file, prose, or the ui-check runner.
_REAL_REPLAY_SOURCES = sorted(
    (pr, path)
    for pr, (paths, _) in _REAL_REPLAYS.items()
    for path in paths
    if path.startswith("frontend/src/") or path.startswith("frontend/fixtures/")
    if not path.endswith((".test.ts", ".md"))
)
#: A replayed source the failing module never reads, with the replay's other
#: module that does. #2985's own fixture is the lone node's
#: (``host-restart-repeated``); the failing ``host-restart-repeated-surfaces``
#: reads the ``*-launch-fixes-*`` set, so no true ownership line of it names
#: this file, and it failed on the surfaces #2985 changed.
_REAL_TREE_OTHER_CHECKER = {
    (
        "2985",
        "frontend/fixtures/scenarios/dashboard-host-restart-repeated.json",
    ): "host-restart-repeated",
}
_SHARE_REASON = re.compile(r"the selection is \d+ of \d+ modules, 70% or more")


def _real_tree_checker(pr: str, path: str) -> str:
    """The module a change to *path* alone must reach: #*pr*'s failing one,
    unless :data:`_REAL_TREE_OTHER_CHECKER` names the one that reads it."""
    return _REAL_TREE_OTHER_CHECKER.get((pr, path), _REAL_REPLAYS[pr][1])


@pytest.fixture(scope="module")
def real_tree_shared(checkout: FrontendTree) -> frozenset[str]:
    """The replayed files, the runner and the ui-check modules aside, that
    alone run everything for :data:`~kew.verify_frontend.WHOLE_SHARE`: read
    off this checkout, so an import the console gains or loses moves a file
    in or out of the set without a hand list to keep."""
    return frozenset(
        path
        for paths, _ in _REAL_REPLAYS.values()
        for path in paths
        if path != _RUNNER and posixpath.dirname(path) != _CHECKS
        if _SHARE_REASON.fullmatch(
            select([Change("M", path)], checkout, checkout).reason
        )
    )


def _probed(tree: FrontendTree, failed: str) -> tuple[FrontendTree, str, str]:
    """*tree* with a copy of the module *failed* beside it, and the copy's name
    and text, for a map to hold.

    A module that reads files runs for every change, so finding *failed* in a
    selection proves nothing for such a module. The rule reads the tree's own
    modules, and the copy is no module of the tree, only a file of it, so a
    map that holds it selects it only through its ownership lines or an
    import, as it would *failed* if it read nothing.
    """
    text = tree.modules[failed]
    assert owned_globs(text), f"{failed} carries no ownership line"
    probe = f"probe-{failed}"
    path = f"{_CHECKS}/{probe}.mjs"
    probed = FrontendTree(
        files=tree.files | {path},
        sources={**tree.sources, path: text},
        modules=tree.modules,
    )
    return probed, probe, text


def _real_tree_select(
    tree: FrontendTree, paths: list[str], failed: str, *, padded: bool = False
) -> tuple[FrontendSelection, str]:
    """The selection of a change to *paths* over :func:`_probed`'s tree, its
    map the checkout's modules (or :func:`_mapped_and_padded`'s) and the copy
    of *failed*, and the copy's name."""
    probed, probe, text = _probed(tree, failed)
    modules = _mapped_and_padded(probed) if padded else dict(probed.modules)
    selection = select(
        _changes(*paths), probed, probed, modules={**modules, probe: text}
    )
    return selection, probe


class TestReplaysOnTheRealTree:
    """The six replays against this checkout's own modules and imports, where
    the ``_CONSOLE`` replays read a console-shaped fixture (#3298).

    No replay runs scoped whole on the real tree: #2965 changed the runner,
    and each of the others changed a shared file, one that alone selects
    :data:`~kew.verify_frontend.WHOLE_SHARE` of the modules or more
    (:func:`real_tree_shared`), as ``lib/api.ts`` and ``lib/factory/names.ts``
    do, which nearly every page imports. Without the runner and those files
    each replay runs scoped and reaches the failing module, and so does each
    of its other source files alone, but for one fixture that only another
    module of the replay reads (:data:`_REAL_TREE_OTHER_CHECKER`), which
    reaches that module. That a shared file alone still reaches the failing
    module is read with the map padded, as :func:`_mapped_and_padded` says
    why. Each module is reached through its ownership lines or an import,
    never only because it reads files (:func:`_probed`).

    A characterization of the real tree, deliberately: the whole runs are the
    sound answer, not a gap in the map. The runner is every module's, and a
    shared file reaches the modules that own the pages and parts importing
    it. For some, ``lib/api.ts`` among them, those modules alone make
    WHOLE_SHARE; for others, ``lib/monitor/holds.ts`` among them, they make
    it only beside the file-reading modules, which run for every change. So
    only a rule that skipped a module checking a changed page, or one that
    stopped running the file-reading modules, could make these replays run
    scoped.
    """

    @pytest.mark.policy
    @pytest.mark.parametrize("pr", sorted(_REAL_REPLAYS))
    def test_a_real_tree_replay_runs_whole_for_the_runner_or_a_shared_file(
        self, checkout: FrontendTree, real_tree_shared: frozenset[str], pr: str
    ) -> None:
        paths, _ = _REAL_REPLAYS[pr]
        gone = [path for path in paths if path not in checkout.files]
        assert not gone, f"#{pr} replays files that are gone: {gone}"

        selection = select(_changes(*paths), checkout, checkout)

        # Whole either way, and for no other reason: a replay that starts to
        # run scoped, or falls back on something else, fails here until this
        # class's account of the real tree is rewritten.
        assert not selection.scoped, f"#{pr} now runs scoped: {selection.reason}"
        if _RUNNER in paths:
            assert selection.reason == f"the ui-check runner: {_RUNNER}"
        else:
            assert _SHARE_REASON.fullmatch(selection.reason), selection.reason
            assert real_tree_shared.intersection(paths), (
                f"#{pr} runs whole with no shared file: {selection.reason}"
            )

    @pytest.mark.policy
    @pytest.mark.parametrize("pr", _REAL_TREE_REPLAYS_OFF_THE_RUNNER)
    def test_a_real_tree_replay_without_its_shared_files_runs_scoped(
        self, checkout: FrontendTree, real_tree_shared: frozenset[str], pr: str
    ) -> None:
        paths, failed = _REAL_REPLAYS[pr]
        # The ui-check modules go too: a module the change edits always runs.
        rest = [
            p
            for p in paths
            if p not in real_tree_shared and posixpath.dirname(p) != _CHECKS
        ]
        own = [p for p in rest if (pr, p) in _REAL_REPLAY_SOURCES]
        # Were every file shared, the checks below would read prose and tests.
        assert own, f"#{pr} keeps no source file once its shared files are out"

        selection, probe = _real_tree_select(checkout, rest, failed)

        assert selection.scoped, selection.reason
        assert probe in selection.modules, f"#{pr}: {rest} leaves out {failed}"

    @pytest.mark.policy
    @pytest.mark.parametrize("pr", _REAL_TREE_REPLAYS_OFF_THE_RUNNER)
    def test_each_unshared_real_tree_replay_source_alone_runs_the_failing_module(
        self, checkout: FrontendTree, real_tree_shared: frozenset[str], pr: str
    ) -> None:
        own = [p for q, p in _REAL_REPLAY_SOURCES if q == pr]
        unshared = [p for p in own if p not in real_tree_shared]
        assert unshared, f"#{pr}: every source file is shared: {own}"

        missed = {}
        for path in unshared:
            checker = _real_tree_checker(pr, path)
            selection, probe = _real_tree_select(checkout, [path], checker)
            if not selection.scoped or probe not in selection.modules:
                missed[path] = f"leaves out {checker}: {selection.reason}"

        assert not missed, f"#{pr}: alone, {missed}"

    @pytest.mark.policy
    @pytest.mark.parametrize(("pr", "path"), _REAL_REPLAY_SOURCES)
    def test_each_real_tree_replay_source_alone_selects_the_module_that_failed(
        self, checkout: FrontendTree, pr: str, path: str
    ) -> None:
        checker = _real_tree_checker(pr, path)

        selection, probe = _real_tree_select(checkout, [path], checker, padded=True)

        assert selection.scoped, selection.reason
        assert probe in selection.modules, f"#{pr}: {path} leaves out {checker}"
