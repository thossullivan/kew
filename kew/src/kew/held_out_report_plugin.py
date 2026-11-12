"""kew's own pytest report for the held-out and contract runs (#3194).

Never imported by kew. The verifying kew writes this file's text outside the
candidate's checkout, as ``kew_held_out_<random hex>.py``, and runs it as the
run's entry point: it loads itself under that name, from that path, and hands
itself to ``pytest.main`` as the first plugin after pytest's own — before the
head's ``addopts``, any ``-p`` and any conftest. No entry point, no module on
``sys.path`` and nothing the candidate ships can stand in for it, and the
candidate cannot know its name before the run starts. xdist workers load it
by that name through ``PYTEST_PLUGINS``, only to put the environment back.

The run's verdict is read from the JSON file it writes, never from the run's
log, where any test can print a line shaped like a result. The report names
every test or collector that failed or errored, and — when asked — every
test whose call passed. It refuses the run (``refused`` in the report) unless
every plugin registered in it, every hook implementation it called and every
module of a pinned package it imported is code from:

- this file;
- the base's own test tree (``KEW_HELD_OUT_TRUSTED``); or
- a file listed in the ``RECORD`` of the one installed distribution allowed to
  carry that package, at the version the base's ``uv.lock`` pins, whose bytes
  still hash to what ``RECORD`` says.

Distributions are looked up only outside the candidate's checkout
(``KEW_HELD_OUT_CHECKOUT``), the interpreter's own site directories aside; a
distribution installing a file inside another's pinned package, a pinned
distribution installed twice, and a ``pytest11`` entry point named after a
pinned plugin that points anywhere else are refused too.

The checks run in the process that writes the report. Code the candidate ships
runs in that process as well, so they bind what the candidate declares —
entry points, plugins, conftests, packages — not what its running code does.

The variables it reads are taken out of the environment as it is imported, so
an xdist worker or a pytest the tests start themselves reports nothing and
never finds the report's path. The settings that keep autoload off are taken
out too, once the process that runs the tests has loaded its plugins — an
xdist worker, or a run that starts none — so a pytest a test starts of its
own runs as it would in the main phase. A controller that starts workers
keeps them: each worker it starts, or restarts, must load what it loaded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import site
import sys
from importlib import metadata, util
from types import ModuleType
from typing import Any

import pytest

#: Where to write the report; unset, the plugin reports nothing.
REPORT_ENV = "KEW_HELD_OUT_REPORT"
#: The pins, as :func:`kew.pinned_pytest_plugins.Pins.settings` writes them.
SETTINGS_ENV = "KEW_HELD_OUT_SETTINGS"
#: The candidate's checkout: no distribution is looked up inside it.
CHECKOUT_ENV = "KEW_HELD_OUT_CHECKOUT"
#: The base's test tree: its conftests and helpers may register plugins.
TRUSTED_ENV = "KEW_HELD_OUT_TRUSTED"
#: Set to ``1`` to name the tests that passed as well.
NAME_PASSES_ENV = "KEW_HELD_OUT_NAME_PASSES"
#: Set to ``1`` to list every test the run collected (the base's own pass).
LIST_COLLECTED_ENV = "KEW_HELD_OUT_LIST_COLLECTED"
#: The report of the base's own collection of this run, to hold it to.
EXPECTED_ENV = "KEW_HELD_OUT_EXPECTED"

_OWN = (
    REPORT_ENV,
    SETTINGS_ENV,
    CHECKOUT_ENV,
    TRUSTED_ENV,
    NAME_PASSES_ENV,
    LIST_COLLECTED_ENV,
    EXPECTED_ENV,
)
_env: dict[str, str | None] = (
    {}
    if __name__ == "__main__"
    else {name: os.environ.pop(name, None) for name in _OWN}
)
_report_path = _env.get(REPORT_ENV)
_name_passes = _env.get(NAME_PASSES_ENV) == "1"

#: The start-up hooks Python had imported when this plugin loaded, and from
#: where: taken now, before anything the run imports can replace them.
_startup_hooks: dict[str, str | None] = {
    name: getattr(sys.modules[name], "__file__", None)
    for name in ("sitecustomize", "usercustomize")
    if sys.modules.get(name) is not None
}
_here = os.path.realpath(__file__)

#: The statuses the base's own collection may end with: nothing wrong,
#: a base module that does not import, nothing selected.
_COLLECTED = (0, 1, 5)

_failed: dict[str, None] = {}
_passed: dict[str, None] = {}
_collected: dict[str, None] = {}
_registered: list[tuple[str, object]] = []

#: How many refusals the report spells out before it counts the rest.
_SHOWN = 5


def _node(nodeid: str) -> str:
    """*nodeid* on one line, whatever characters a test id carried."""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in nodeid)


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _inside(path: str, root: str | None) -> bool:
    if not root:
        return False
    # Both real paths are absolute, so commonpath has a common root to find.
    return os.path.commonpath([_real(path), _real(root)]) == _real(root)


class _Audit:
    """What the run loaded, held against the pins."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.plugins: list[list[str]] = settings.get("plugins", [])
        self.packages: dict[str, str] = settings.get("packages", {})
        #: ``None``: no lock, so any installed version of each.
        self.versions: dict[str, str] | None = settings.get("versions", {})
        self.refused: list[str] = [str(r) for r in settings.get("refused", [])]
        self.checkout = _env.get(CHECKOUT_ENV)
        self.trusted = _env.get(TRUSTED_ENV)
        #: Each file of a pinned distribution: its name, RECORD hash mode, hash.
        self.owned: dict[str, tuple[str, str | None, str | None]] = {}
        self._vouched: dict[str, str | None] = {}

    def refuse(self, reason: str) -> None:
        if reason not in self.refused:
            self.refused.append(reason)

    def paths(self) -> list[str]:
        """``sys.path`` without the checkout (its site directories aside)."""
        sites = {_real(p) for p in site.getsitepackages()}
        here = os.path.dirname(_here)
        kept = []
        for path in sys.path:
            if not path or not os.path.isdir(path) or _real(path) == here:
                continue
            if _inside(path, self.checkout) and _real(path) not in sites:
                continue
            kept.append(path)
        return list(dict.fromkeys(kept))

    def distributions(self) -> None:
        """Map every pinned distribution's files, and refuse the impostors."""
        found: dict[str, list[metadata.Distribution]] = {}
        for dist in metadata.distributions(path=self.paths()):
            name = _normal(dist.metadata["Name"] or "")
            found.setdefault(name, []).append(dist)
            self._foreign_files(name, dist)
        pinned: dict[str, str | None] = (
            dict.fromkeys(n for n in self.packages.values() if n in found)
            if self.versions is None
            else dict(self.versions)
        )
        for name, version in pinned.items():
            self._own(name, version, found.get(name, []))

    def _own(
        self, name: str, version: str | None, found: list[metadata.Distribution]
    ) -> None:
        """Map *name*'s files, if it is installed once, at *version*, from an
        index: a ``direct_url.json`` marks one installed from a path, a VCS or
        a URL, whose ``RECORD`` is whatever that source built."""
        copies = {_dist_path(d): d for d in found}
        if len(copies) != 1:
            self.refuse(f"{name} is installed {len(copies)} times, not once")
            return
        dist = next(iter(copies.values()))
        if version is not None and dist.version != version:
            self.refuse(f"{name} {dist.version} is installed; the base pins {version}")
            return
        if dist.read_text("direct_url.json") is not None:
            self.refuse(f"{name} is installed from a path, VCS or URL, not an index")
            return
        for record in dist.files or ():
            digest = record.hash
            self.owned[_real(str(record.locate()))] = (
                name,
                None if digest is None else digest.mode,
                None if digest is None else digest.value,
            )

    def _foreign_files(self, name: str, dist: metadata.Distribution) -> None:
        for record in dist.files or ():
            parts = record.parts
            owner = self.packages.get(parts[0]) if parts else None
            if owner is None and parts and parts[0].endswith(".py"):
                owner = self.packages.get(parts[0][:-3])
            if owner is not None and owner != name:
                self.refuse(f"{name} installs {record} inside {owner}'s package")

    def vouch(self, path: str | None, what: str) -> None:
        """Refuse unless *path* is this file, the base's tests, or a pinned file."""
        if path is None:
            self.refuse(f"{what} has no source file")
            return
        real = _real(path)
        if real not in self._vouched:
            self._vouched[real] = self._why_not(real)
        why = self._vouched[real]
        if why is not None:
            self.refuse(f"{what} ({path}) {why}")

    def _why_not(self, real: str) -> str | None:
        if real == _here or _inside(real, self.trusted):
            return None
        if real not in self.owned:
            return "is not a file of any distribution kew pins"
        name, mode, value = self.owned[real]
        if mode != "sha256":
            return f"has no sha256 in {name}'s RECORD"
        try:
            with open(real, "rb") as handle:
                digest = hashlib.sha256(handle.read()).digest()
        except OSError as exc:
            return f"cannot be read: {exc}"
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != value:
            return f"does not hash to what {name}'s RECORD says"
        return None

    def entry_points(self) -> None:
        """Each ``pytest11`` entry point named after a pinned plugin is it.

        Every copy of every distribution on ``sys.path`` is read, the
        checkout's included: ``entry_points()`` would keep only the first of
        two distributions sharing a name, and pytest's loader reads them all.
        """
        expected = {name: (module, dist) for name, module, dist in self.plugins}
        for dist in metadata.distributions():
            owner = _normal(dist.metadata["Name"] or "")
            for point in dist.entry_points:
                if point.group != "pytest11" or point.name not in expected:
                    continue
                if (point.value, owner) == expected[point.name]:
                    continue
                self.refuse(
                    f"the pytest11 entry point {point.name} = {point.value} "
                    f"from {owner} answers to a pinned plugin's name"
                )

    def registered(self, manager: Any) -> None:
        """Every plugin registered in the run, and each hook it implements."""
        for name, plugin in _registered:
            self.vouch(_source_of(plugin), f"the pytest plugin {name}")
        for name, module, _dist in self.plugins:
            loaded = manager.get_plugin(name)
            if loaded is not None and loaded is not sys.modules.get(module):
                self.refuse(f"the pytest plugin {name} is not {module}")
        for caller in vars(manager.hook).values():
            for impl in getattr(caller, "get_hookimpls", lambda: [])():
                code = getattr(impl.function, "__code__", None)
                self.vouch(
                    None if code is None else code.co_filename,
                    f"the {caller.name} hook of {impl.plugin_name}",
                )

    def modules(self) -> None:
        """Every imported module of a pinned package is a pinned file."""
        for name, module in list(sys.modules.items()):
            if name.partition(".")[0] in self.packages and module is not None:
                self.vouch(getattr(module, "__file__", None), f"the module {name}")

    def startup(self) -> None:
        """No ``sitecustomize`` or ``usercustomize`` the checkout ships ran.

        Python imports either at start-up from anywhere on ``sys.path`` — the
        checkout's ``src`` included, which an editable install puts there — so
        it runs before this plugin does.
        """
        for name, path in _startup_hooks.items():
            if path is None or _inside(path, self.checkout):
                self.refuse(f"{name} ran at start-up from {path or 'no file'}")

    def reason(self) -> str | None:
        if not self.refused:
            return None
        shown = self.refused[:_SHOWN]
        more = len(self.refused) - len(shown)
        return "; ".join(shown) + (f"; and {more} more" if more else "")


def _dist_path(dist: metadata.Distribution) -> str:
    """Where *dist*'s own metadata directory is: one per installed copy."""
    path = getattr(dist, "_path", None)
    return _real(str(path if path is not None else dist.locate_file("")))


def _source_of(plugin: object) -> str | None:
    """The file a plugin's code comes from: a module's own, or its class's."""
    if isinstance(plugin, ModuleType):
        return getattr(plugin, "__file__", None)
    module = sys.modules.get(getattr(plugin, "__module__", None) or "")
    return None if module is None else getattr(module, "__file__", None)


def _settings() -> dict[str, Any]:
    try:
        data = json.loads(_env.get(SETTINGS_ENV) or "")
    except ValueError:
        return {"refused": ["the run carried no pins kew can read"]}
    return data if isinstance(data, dict) else {"refused": ["the pins are not a map"]}


def _starts_workers(config: Any) -> bool:
    if hasattr(config, "workerinput"):
        return False
    return bool(getattr(config.option, "numprocesses", None)) or bool(
        getattr(config.option, "tx", None)
    )


def _restore_environment() -> None:
    """Take the held-out run's own settings out of this process's environment."""
    os.environ.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    if os.environ.get("PYTEST_PLUGINS") == __name__:
        del os.environ["PYTEST_PLUGINS"]
    here = os.path.dirname(_here)
    paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    kept = [path for path in paths if path and _real(path) != here]
    if kept:
        os.environ["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        os.environ.pop("PYTHONPATH", None)


def pytest_plugin_registered(plugin: object, manager: Any) -> None:
    """Note every plugin the run registers, as it registers it.

    The hook is historic: registered first, it also hears of pytest's own
    plugins, registered before it.
    """
    if _report_path is not None:
        _registered.append((manager.get_name(plugin) or repr(plugin), plugin))


def pytest_configure(config: Any) -> None:
    if not _starts_workers(config):
        _restore_environment()


def pytest_collectreport(report: Any) -> None:
    if report.failed:
        _failed[_node(report.nodeid)] = None


def pytest_collection_finish(session: Any) -> None:
    """What a run without xdist workers collected, its selection applied."""
    for item in session.items:
        _collected[_node(item.nodeid)] = None


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    """What an xdist worker collected; every worker collects the whole run."""
    for nodeid in ids:
        _collected[_node(nodeid)] = None


def _uncollected() -> tuple[list[str], str | None]:
    """The tests the base collects for this run and this run did not.

    Read from the base's own collection of the same selection, under the
    same configuration (``KEW_HELD_OUT_EXPECTED``). A test in a file whose
    collection failed here is named too, beside that file's failure, so a
    declaration of the file alone excuses none of its tests.
    """
    path = _env.get(EXPECTED_ENV)
    if path is None:
        return [], None
    expected, why = _expected(path)
    if why is not None:
        return [], why
    return [node for node in expected if node not in _collected], None


def _expected(path: str) -> tuple[list[str], str | None]:
    """What the base's own collection at *path* collected, or why it cannot say."""
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return [], "kew has no collection of the base to hold the run to"
    collected = report.get("collected") if isinstance(report, dict) else None
    if not isinstance(collected, list):
        return [], "kew cannot read the base's collection of the run"
    if report.get("refused"):
        return [], f"the base's own collection was refused: {report['refused']}"
    if report.get("exitstatus") not in _COLLECTED:
        return [], f"the base's own collection exited {report.get('exitstatus')}"
    return [str(node) for node in collected], None


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        _failed[_node(report.nodeid)] = None
    elif _name_passes and report.when == "call" and report.passed:
        _passed[_node(report.nodeid)] = None


def _refusal(manager: Any) -> str | None:
    audit = _Audit(_settings())
    audit.distributions()
    audit.entry_points()
    audit.registered(manager)
    audit.modules()
    audit.startup()
    return audit.reason()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write the report, once, from the process that started the run."""
    if _report_path is None:
        return
    uncollected, unheld = _uncollected()
    refused = "; ".join(
        r for r in (_refusal(session.config.pluginmanager), unheld) if r
    )
    report: dict[str, object] = {
        "failed": list(_failed),
        "passed": [node for node in _passed if node not in _failed],
        "uncollected": uncollected,
        "refused": refused or None,
        "exitstatus": int(exitstatus),
    }
    # The base's own collection lists what it collected; so does the
    # contracts' run, each of whose collected contract tests must pass.
    if _name_passes or _env.get(LIST_COLLECTED_ENV) == "1":
        report["collected"] = list(_collected)
    with open(_report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle)


def _main() -> int:
    """Load this file as the plugin it is, under its own name, and run pytest."""
    name = os.path.splitext(os.path.basename(_here))[0]
    spec = util.spec_from_file_location(name, _here)
    if spec is None or spec.loader is None:  # pragma: no cover - a plain file
        raise SystemExit(f"cannot load {_here}")
    plugin = util.module_from_spec(spec)
    sys.modules[name] = plugin
    spec.loader.exec_module(plugin)
    return int(pytest.main(sys.argv[1:], plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(_main())
