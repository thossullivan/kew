"""A stub ``docker`` whose containers are real processes (#2876).

A job's ``docker run -d`` leaves a container behind that the docker daemon,
not the job, is the parent of, so it is outside the job's scope. The stand-in
is the same: ``run -d IMAGE ARGV...`` starts *ARGV* detached, in a session
of its own and holding none of the job's descriptors, so not its stand-in
cgroup (:func:`~support.fake_systemd.start_in_cgroup`), and prints a
container id. ``ps -q`` (running) and ``ps -aq`` (every
container not removed), ``inspect --format`` over ``{{.State.Status}}``,
``{{.Created}}`` and ``{{.State.StartedAt}}``, ``kill`` and ``rm -f`` answer
from those processes.

:func:`add_container` records one started at a given time, for a container
that was running before the job; :func:`add_created_container` one created
and never started, as a ``docker run`` not yet through leaves it.
:func:`add_pending_container` one the daemon has accepted but lists only
a moment after the next ``ps`` has returned (#2980), as a ``docker run`` in
flight leaves it; :data:`LISTINGS` records when each ``ps`` ran.
:func:`add_image` gives it a tagged image for ``image ls``, ``image inspect``
and ``image rm``, and :func:`add_image_container` a stopped container that
``container ls`` names by its image.
``KILL_IGNORED`` in the state directory makes ``kill`` and ``rm -f`` no-ops,
for the container that survives them; ``INSPECT_FAILS`` makes ``inspect``
fail, as a daemon that lists a container it then cannot answer for does.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from support.process_facts import stub_prelude

#: A file in the state directory that makes ``docker kill`` and ``rm -f`` do
#: nothing.
KILL_IGNORED = "kill-ignored"

#: A file in the state directory that makes ``docker inspect`` fail.
INSPECT_FAILS = "inspect-fails"

#: A file in the state directory with one ``[began, ended]`` line of wall-clock
#: seconds per ``docker ps``, in the order they ran.
LISTINGS = "listings"

_DOCKER = """
import json, os, signal, subprocess, sys, time, uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from support.process_facts import alive
state = Path(sys.argv[0]).resolve().parent.parent / "docker"
containers = state / "containers"
containers.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]

NEVER = "0001-01-01T00:00:00Z"

def status(found):
    if found.get("pid") is None:
        return "created"
    return "running" if alive(found["pid"]) else "exited"

def listed():
    for record in sorted(containers.iterdir()):
        yield record.name, json.loads(record.read_text())

def images():
    path = state / "images.json"
    return json.loads(path.read_text()) if path.exists() else []

def reference(row):
    return f"{row['repository']}:{row['tag']}"

def running():
    for cid, found in listed():
        if status(found) == "running":
            yield cid, found

if argv[0] == "run":
    command = argv[argv.index("-d") + 2:]
    # close_fds: the container holds none of the job's descriptors, so it is
    # outside the job's stand-in cgroup, as the daemon's child is.
    child = subprocess.Popen(
        command, close_fds=True, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cid = uuid.uuid4().hex * 2
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    record = {"pid": child.pid, "created": started, "started": started}
    (containers / cid).write_text(json.dumps(record))
    print(cid)
elif argv[0] == "ps":
    began = time.time()
    pending = state / "pending"
    reveal = state / "pending-reveal-at"
    if reveal.exists() and began >= float(reveal.read_text()):
        for record in sorted(pending.iterdir()):
            record.rename(containers / record.name)
        reveal.unlink()
    for cid, _ in listed() if "-aq" in argv else running():
        print(cid)
    sys.stdout.flush()
    ended = time.time()
    if not reveal.exists() and pending.exists() and any(pending.iterdir()):
        delay = float((state / "pending-delay").read_text())
        reveal.write_text(str(ended + delay))
    with (state / "@LISTINGS@").open("a") as log:
        log.write(json.dumps([began, ended]) + "\\n")
elif argv[0] == "inspect":
    record = containers / argv[-1]
    if not record.exists() or (state / "@INSPECT_FAILS@").exists():
        sys.exit(1)
    found = json.loads(record.read_text())
    template = argv[argv.index("--format") + 1]
    print(
        template.replace("{{.State.Status}}", status(found))
        .replace("{{.Created}}", found["created"])
        .replace("{{.State.StartedAt}}", found.get("started") or NEVER)
    )
elif argv[:2] == ["image", "ls"]:
    wanted = next(a.partition("=")[2] for a in argv if a.startswith("reference="))
    for row in images():
        if row["repository"] == wanted:
            fields = (row["repository"], row["tag"], row["id"], row["size"])
            print("\t".join(json.dumps(value) for value in fields))
elif argv[:2] == ["image", "inspect"]:
    found = [row for row in images() if reference(row) == argv[-1]]
    if not found:
        sys.exit(1)
    print(json.dumps(found[0]["id"]))
elif argv[:2] == ["image", "rm"]:
    kept = [row for row in images() if reference(row) != argv[-1]]
    if len(kept) == len(images()):
        print(f"Error: No such image: {argv[-1]}", file=sys.stderr)
        sys.exit(1)
    (state / "images.json").write_text(json.dumps(kept))
elif argv[:2] == ["container", "ls"]:
    for _, found in listed():
        if found.get("image"):
            print(json.dumps(found["image"]))
elif argv[0] in ("kill", "rm") and not (state / "@KILL_IGNORED@").exists():
    every = dict(listed())
    for cid in [arg for arg in argv[1:] if not arg.startswith("-")]:
        found = every.get(cid)
        if found is None:
            continue
        if status(found) == "running":
            os.kill(found["pid"], signal.SIGKILL)
        if argv[0] == "rm":
            (containers / cid).unlink()
"""


def install_fake_docker(bin_dir: Path) -> Path:
    """Write the stub into *bin_dir*; return its state directory beside it."""
    stub = bin_dir / "docker"
    body = (
        _DOCKER.replace("@KILL_IGNORED@", KILL_IGNORED)
        .replace("@INSPECT_FAILS@", INSPECT_FAILS)
        .replace("@LISTINGS@", LISTINGS)
    )
    stub.write_text(f"#!{sys.executable}\n{stub_prelude()}{body}", encoding="utf-8")
    stub.chmod(0o755)
    state = bin_dir.parent / "docker"
    (state / "containers").mkdir(parents=True, exist_ok=True)
    return state


def add_container(
    state: Path, started: datetime
) -> tuple[str, subprocess.Popen[bytes]]:
    """A container, started at *started*, that is running now; its id and process."""
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    cid = uuid.uuid4().hex * 2
    stamp = _stamp(started)
    record = {"pid": process.pid, "created": stamp, "started": stamp}
    (state / "containers" / cid).write_text(json.dumps(record), encoding="utf-8")
    return cid, process


def add_pending_container(
    state: Path, started: datetime, after: float
) -> tuple[str, subprocess.Popen[bytes]]:
    """A running container ``ps`` lists only *after* seconds once the next
    listing has returned, so a listing straight after that one still misses
    it; its id and process."""
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    cid = uuid.uuid4().hex * 2
    stamp = _stamp(started)
    record = {"pid": process.pid, "created": stamp, "started": stamp}
    (state / "pending").mkdir(exist_ok=True)
    (state / "pending-delay").write_text(str(after), encoding="utf-8")
    (state / "pending" / cid).write_text(json.dumps(record), encoding="utf-8")
    return cid, process


def add_created_container(state: Path, created: datetime) -> str:
    """A container created at *created* and never started; its id."""
    cid = uuid.uuid4().hex * 2
    record = {"pid": None, "created": _stamp(created), "started": None}
    (state / "containers" / cid).write_text(json.dumps(record), encoding="utf-8")
    return cid


def add_image(state: Path, reference: str, image_id: str, size: str = "1GB") -> None:
    """A tagged image ``image ls``, ``image inspect`` and ``image rm`` answer for."""
    repository, _, tag = reference.rpartition(":")
    rows = _image_rows(state)
    rows.append({"repository": repository, "tag": tag, "id": image_id, "size": size})
    (state / "images.json").write_text(json.dumps(rows), encoding="utf-8")


def add_image_container(state: Path, image: str) -> str:
    """A stopped container made from *image*, which ``container ls`` names."""
    cid = uuid.uuid4().hex * 2
    record = {"pid": None, "created": _stamp(datetime.now(UTC)), "image": image}
    record["started"] = None
    (state / "containers" / cid).write_text(json.dumps(record), encoding="utf-8")
    return cid


def image_references(state: Path) -> set[str]:
    """The tags the stub holds now."""
    return {f"{row['repository']}:{row['tag']}" for row in _image_rows(state)}


def _image_rows(state: Path) -> list[dict[str, str]]:
    path = state / "images.json"
    if not path.exists():
        return []
    rows: list[dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    return rows


def _stamp(moment: datetime) -> str:
    """*moment* as the daemon writes a container's times."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


#: A ``docker`` that keeps its containers as JSON state, for the launch path's
#: ``create``/``inspect``/``rm``/``kill`` calls (#3246). Each ``create`` takes
#: the next behaviour off ``plan``: ``"ok"``; ``"commit-then-fail"``, which
#: makes the container and still exits 1, as a client that lost the daemon's
#: answer does; ``"hang-commit-late"``, which hangs past kew's limit and makes
#: the container only once the next ``create`` reaches the daemon;
#: ``"commit-then-hang"``, which makes the container at once and then hangs
#: past kew's limit, as a daemon that never answers its client does;
#: ``"interrupt"``, which sends the caller SIGINT before the daemon sees the
#: create, as a Ctrl-C during ``docker create`` does, and
#: ``"commit-then-interrupt"``, which does so once the container is made and
#: its id written to the cidfile. With ``snapshot_db``, each ``create`` first
#: copies that SQLite database to ``snapshot-<call index>.db`` beside the
#: state: what a process that crashed then would leave for recovery.
#: ``rm_hangs`` makes every ``rm`` hang, ``ps_hangs`` every ``ps``;
#: ``info_fails`` leaves the daemon unnamed, as a loaded host's ``docker info``
#: can. Every call is appended to ``calls``.
_LAUNCH_DOCKER = """
import json, os, signal, sqlite3, sys, time
from pathlib import Path
from typing import Any

STATE = Path(@STATE@)
state = json.loads(STATE.read_text())
args = sys.argv[1:]
state["calls"].append(args)


def save():
    STATE.write_text(json.dumps(state))


def find(target):
    for cid, found in state["containers"].items():
        if target in (cid, found["name"]):
            return cid
    return None


def interrupt_caller():
    save()
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(60)


def snapshot():
    if state["snapshot_db"] is None:
        return
    index = len(state["calls"]) - 1
    source = sqlite3.connect(state["snapshot_db"], timeout=10)
    target = sqlite3.connect(str(STATE.parent / ("snapshot-%d.db" % index)))
    source.backup(target)
    target.close()
    source.close()


def missing(target):
    save()
    sys.stderr.write("Error: No such container: " + target + "\\n")
    sys.exit(1)


verb = args[0]
if verb == "create":
    snapshot()
    if state["plan"] and state["plan"][0] == "interrupt":
        state["plan"].pop(0)
        interrupt_caller()
    for cid, found in state.pop("late", {}).items():
        state["containers"][cid] = found
    name = args[args.index("--name") + 1]
    cidfile = Path(args[args.index("--cidfile") + 1])
    if cidfile.exists():
        save()
        sys.stderr.write("Container ID file found: " + str(cidfile) + "\\n")
        sys.exit(125)
    if find(name) is not None:
        save()
        sys.stderr.write(
            'Conflict. The container name "/' + name + '" is already in use '
            'by container "x"\\n'
        )
        sys.exit(1)
    plan = state["plan"]
    step = plan.pop(0) if plan else "ok"
    state["creates"] += 1
    labels = dict(
        args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label"
    )
    cid = format(state["creates"], "064x")
    record = {"name": name, "labels": labels, "status": "created"}
    if step == "hang-commit-late":
        state.setdefault("late", {})[cid] = record
        cidfile.write_text("")
        save()
        time.sleep(60)
    if step == "commit-then-interrupt":
        state["containers"][cid] = record
        cidfile.write_text(cid)
        interrupt_caller()
    if step == "commit-then-hang":
        state["containers"][cid] = record
        cidfile.write_text("")
        save()
        time.sleep(60)
    state["containers"][cid] = record
    if step == "commit-then-fail":
        save()
        sys.stderr.write("Error response from daemon: context deadline exceeded\\n")
        sys.exit(1)
    cidfile.write_text(cid)
    save()
    print(cid)
elif verb == "info":
    save()
    if state["info_fails"]:
        sys.exit(1)
    fields = len(args[args.index("--format") + 1].split())
    print(" ".join(["engine-1"] * fields))
elif verb == "ps":
    save()
    if state["ps_hangs"]:
        time.sleep(60)
    wanted = args[args.index("--filter") + 1].split("=", 1)[1]
    for found in state["containers"].values():
        if found["name"] == wanted:
            print(found["name"])
elif verb == "inspect":
    target = args[-1]
    cid = find(target)
    if cid is None:
        missing(target)
    found = state["containers"][cid]
    save()
    if args[args.index("--format") + 1] == "{{.Id}}":
        print(cid)
    else:
        pid = "0" if found["status"] == "created" else "4242"
        labels = json.dumps(found["labels"])
        print(cid + " " + found["status"] + " " + pid + " " + labels)
elif verb in ("rm", "kill"):
    if verb == "rm" and state["rm_hangs"]:
        save()
        time.sleep(60)
    target = args[-1]
    cid = find(target)
    if cid is None:
        missing(target)
    if verb == "rm":
        del state["containers"][cid]
    else:
        state["containers"][cid]["status"] = "exited"
    save()
    print(target)
else:
    save()
    sys.exit(2)
"""


class LaunchDocker:
    """The JSON-state ``docker`` above, written to *root* (#3246)."""

    def __init__(
        self,
        root: Path,
        *,
        plan: tuple[str, ...] = (),
        rm_hangs: bool = False,
        ps_hangs: bool = False,
        info_fails: bool = False,
        snapshot_db: Path | None = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.state = root / "docker-state.json"
        self.state.write_text(
            json.dumps(
                {
                    "plan": list(plan),
                    "rm_hangs": rm_hangs,
                    "ps_hangs": ps_hangs,
                    "info_fails": info_fails,
                    "snapshot_db": None if snapshot_db is None else str(snapshot_db),
                    "creates": 0,
                    "containers": {},
                    "calls": [],
                }
            ),
            encoding="utf-8",
        )
        self.path = root / "docker"
        body = _LAUNCH_DOCKER.replace("@STATE@", repr(str(self.state)))
        self.path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        self.path.chmod(0o755)

    def read(self) -> dict[str, Any]:
        return dict(json.loads(self.state.read_text(encoding="utf-8")))

    def add(self, name: str, labels: dict[str, str], status: str = "created") -> str:
        """A container already under *name*, with *labels*; its id."""
        state = self.read()
        cid = format(1000 + len(state["containers"]), "064x")
        state["containers"][cid] = {"name": name, "labels": labels, "status": status}
        self.state.write_text(json.dumps(state), encoding="utf-8")
        return cid

    def snapshot(self, index: int) -> Path:
        """The database as it stood when call *index* (a ``create``) began."""
        return self.state.parent / f"snapshot-{index}.db"

    def containers(self) -> dict[str, dict[str, Any]]:
        return dict(self.read()["containers"])

    def calls(self, verb: str) -> list[list[str]]:
        """Every call of *verb*, in order, as the argv after ``docker``."""
        return [call for call in self.read()["calls"] if call[0] == verb]
