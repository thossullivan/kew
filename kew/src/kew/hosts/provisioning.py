"""One checked-in recipe, run on a host, and the versions it reported back.

The verify VM was built by hand from an operator script, so a second host, a
sharded set of them or a CI runner could not be built the same way. The recipe
now lives in the repository, next to the provider that runs it
(``provision/ubuntu-24.04.sh``), and this module is the plumbing in between:

* :func:`recipe_path` resolves a recipe **shipped with kew** by name. A caller
  does not get to point this at an arbitrary file on the operator's disk — the
  whole point of the node is that every host is built from a recipe under
  review.
* :func:`provision_host` claims the host, brings it up, runs the recipe through
  the provider, and releases it. The four job verbs are unchanged; copying and
  running a recipe is a provider *capability* (:class:`RecipeHost`), and the
  ``local`` host deliberately does not have it — kew does not apt-install on
  the operator's own machine.
* :func:`parse_tool_versions` reads the recipe's self-check lines back as
  **data**, and :func:`write_record` keeps them. Nothing the host prints is
  executed or trusted as an instruction; a version string is text kew stores
  and shows.

A record is written only when the recipe exits zero. A record is the claim
"this host was built from this recipe"; a half-finished run has no business
leaving one behind, and the self-check in the recipe is what makes the exit
code mean that.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from kew.hosts.errors import HostError
from kew.hosts.lock import lock_path
from kew.hosts.provider import HostProvider

#: Where the recipes shipped with kew live.
RECIPE_DIR = Path(__file__).resolve().parent / "provision"

#: The recipe for the distribution the verify host runs.
DEFAULT_RECIPE = "ubuntu-24.04.sh"

#: The tool pins every recipe in :data:`RECIPE_DIR` reads, as data rather than
#: as a literal in each of them (:func:`pin_path`).
PIN_FILE = "trivy.pin"

#: The pinned scanner's key in that file.
TRIVY_PIN_KEY = "TRIVY_VERSION"

#: One ``KEY=value`` line of the pin file. Comments and blank lines are not
#: assignments and are skipped; a value never contains whitespace.
_PIN_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(\S+)[ \t]*$", re.MULTILINE)

#: What the recipe's self-check prints one line per tool as.
TOOL_MARKER = "KEW_HOST_TOOL"
#: ``\r`` is allowed at the end on purpose: a host reached over SSH can send
#: CRLF, and a version line is not worth losing to a carriage return.
_TOOL_LINE = re.compile(
    rf"^{TOOL_MARKER} ([A-Za-z0-9][A-Za-z0-9_.+-]*)=(\S+)[ \t\r]*$", re.MULTILINE
)

#: Suffix of the per-host provisioning record, written beside the host's lock.
RECORD_SUFFIX = ".provision.json"

#: Ceiling for a whole provisioning run. An apt-driven recipe that fetches a
#: Docker repository, Node, uv and a browser is minutes of work on a cold
#: instance, so the job ceiling a verification run uses is far too tight.
PROVISION_TIMEOUT = 1800.0


@runtime_checkable
class RecipeHost(Protocol):
    """A host kew can copy a provisioning recipe to and run as root.

    Not a fifth job verb: a recipe is not a shipped bundle and its output is
    not a :class:`~kew.hosts.provider.JobResult`. It is a capability a provider
    either has or does not, which is why this is a protocol rather than another
    abstract method — :class:`~kew.hosts.local.LocalProvider` is *this machine*,
    and kew installing system packages on the operator's laptop is not a
    feature. ``provision_host`` says so by name instead of discovering it.
    """

    def run_recipe(
        self, recipe: Path, *, timeout_seconds: float | None
    ) -> tuple[int, str]:
        """Run *recipe* on the host and return its exit code and log text."""


@dataclass(frozen=True)
class ProvisionReport:
    """What one provisioning run produced: data, never an instruction."""

    #: The provider that ran the recipe (``"gcp"``).
    host: str

    #: The exact host it ran on, as the per-host lock names it.
    lock_key: str

    #: The recipe's file name, as shipped.
    recipe: str

    #: SHA-256 of the recipe's bytes, so a record names *which* recipe built
    #: the host, not just its file name.
    recipe_sha256: str

    #: The recipe's exit status. The recipe's own self-check is what makes a
    #: zero mean "every tool is present".
    exit_code: int

    #: Wall-clock seconds the run took, measured by kew around the call.
    seconds: float

    #: The versions the self-check reported, tool name to version string.
    versions: Mapping[str, str]

    #: Combined stdout and stderr of the run, as text.
    log: str

    @property
    def ok(self) -> bool:
        """True iff the recipe, self-check included, exited zero."""
        return self.exit_code == 0


def pin_path() -> Path:
    """The tool pins the recipes read, as one shipped file.

    Shipped beside the recipes rather than written into them, and copied up
    beside the recipe by :meth:`~kew.hosts.gcp.GcpProvider.run_recipe`: the
    version a host's trivy is installed at and the version
    ``scripts/scan-agent-image.sh`` demands are the same value from the same
    file, because a host whose scanner is off that pin passed its own
    provisioning and then failed every verification at the last step of the
    run — after the whole suite had already passed (#2462).
    """
    return RECIPE_DIR / PIN_FILE


def read_pins() -> Mapping[str, str]:
    """Every ``KEY=value`` line of :func:`pin_path`, as a mapping.

    The same shape the recipe's own builtin reader understands: comments and
    plain assignments, no expansion. Read as data — nothing in the file is
    executed here or there.
    """
    text = pin_path().read_text(encoding="utf-8")
    return {match.group(1): match.group(2) for match in _PIN_LINE.finditer(text)}


def pinned_trivy_version() -> str:
    """The one scanner version the recipe installs and the gate accepts."""
    version = read_pins().get(TRIVY_PIN_KEY, "")
    if not version:
        raise HostError(
            f"{pin_path()} defines no {TRIVY_PIN_KEY}: the recipe installs the "
            "scanner the agent-image gate pins, and both read that version here"
        )
    return version


def recipe_path(name: str = DEFAULT_RECIPE) -> Path:
    """The shipped recipe called *name*.

    Only a bare file name from :data:`RECIPE_DIR` resolves: a path, a parent
    reference or an absolute name is refused rather than read. A host built
    from a file nobody reviewed is the thing this node exists to end.
    """
    if name != Path(name).name or name in ("", ".", ".."):
        raise HostError(
            f"{name!r} is not a shipped recipe name: kew runs recipes from its "
            "own provision directory, so that every host is built from one "
            "under review"
        )
    path = RECIPE_DIR / name
    if not path.is_file():
        shipped = ", ".join(sorted(p.name for p in RECIPE_DIR.glob("*.sh"))) or "none"
        raise HostError(f"no recipe named {name!r} ships with kew (shipped: {shipped})")
    return path


def parse_tool_versions(log: str) -> dict[str, str]:
    """The ``KEW_HOST_TOOL name=version`` lines of *log*, as a mapping.

    Read as data: the names and versions are stored and shown, never executed.
    A tool reported twice keeps the last line, which is the one the run ended
    with.
    """
    return {match.group(1): match.group(2) for match in _TOOL_LINE.finditer(log)}


def record_path(directory: Path, lock_key: str) -> Path:
    """Where the provisioning record for host *lock_key* is kept.

    Beside that host's lock file and named from the same key, so the record and
    the lock cannot disagree about which host they describe.
    """
    return lock_path(Path(directory), lock_key).with_suffix(RECORD_SUFFIX)


def write_record(directory: Path, report: ProvisionReport) -> Path:
    """Record the versions *report* came back with, and return the record path.

    Written atomically: a reader of this file is deciding whether a host is
    provisioned, and half a JSON document is worse than none.
    """
    path = record_path(directory, report.lock_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(
        {
            "host": report.host,
            "lock_key": report.lock_key,
            "recipe": report.recipe,
            "recipe_sha256": report.recipe_sha256,
            "provisioned_at": datetime.now(UTC).isoformat(),
            "seconds": round(report.seconds, 3),
            "versions": dict(report.versions),
        },
        indent=2,
        sort_keys=True,
    )
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def read_record(directory: Path, lock_key: str) -> dict[str, object] | None:
    """The recorded provisioning of host *lock_key*, or ``None`` if there is none.

    An unreadable record is reported as an error rather than as "never
    provisioned": the two are different facts.
    """
    path = record_path(directory, lock_key)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostError(
            f"the provisioning record {path} cannot be read: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise HostError(f"the provisioning record {path} is not a JSON object")
    return record


def _run_recipe(
    provider: RecipeHost, recipe: Path, timeout_seconds: float | None
) -> tuple[int, str, float]:
    """Run the recipe and time it, whatever it reports."""
    started = time.monotonic()
    exit_code, log = provider.run_recipe(recipe, timeout_seconds=timeout_seconds)
    return exit_code, log, time.monotonic() - started


def provision_host(
    provider: HostProvider,
    *,
    record_dir: Path,
    recipe: Path | None = None,
    timeout_seconds: float | None = PROVISION_TIMEOUT,
) -> ProvisionReport:
    """Build *provider*'s host from a recipe and record what it reports.

    The host is claimed for the whole run — a provisioning run that installs
    Docker underneath a verification job would break it — brought up as a job
    brings it up, and released afterwards, which arms its idle stop. A failed
    run releases the host too and leaves **no** record.

    The bring-up is inside the released block on purpose: ``ensure_up()`` is
    what *starts* a stopped instance, and it can fail after that — sshd on a
    just-started instance refuses connections for a while, so cancelling the
    pending idle stop is the step most likely to fail on a cold host. Raising
    from there without releasing would leave exactly what the idle stop exists
    to prevent: a running VM, billing, with no stop armed.
    """
    if not isinstance(provider, RecipeHost):
        raise HostError(
            f"the {provider.name} host cannot be provisioned by kew: it is this "
            "machine, and kew does not install system packages on it. Run the "
            f"recipe yourself ({recipe_path().name}) or provision a gcp host"
        )
    script = recipe_path() if recipe is None else Path(recipe)
    if not script.is_file():
        raise HostError(f"provisioning recipe not found: {script}")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    with provider.claim():
        try:
            provider.ensure_up()
            exit_code, log, seconds = _run_recipe(provider, script, timeout_seconds)
        finally:
            provider.release()
    report = ProvisionReport(
        host=provider.name,
        lock_key=provider.lock_key,
        recipe=script.name,
        recipe_sha256=digest,
        exit_code=exit_code,
        seconds=seconds,
        versions=parse_tool_versions(log),
        log=log,
    )
    if report.ok:
        write_record(Path(record_dir), report)
    return report
