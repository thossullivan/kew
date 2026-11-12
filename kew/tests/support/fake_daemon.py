"""A container runtime answering ``subprocess.run`` the way a daemon would.

It holds containers by id, refuses a duplicate name, and answers ``create``,
``inspect``, ``rm``, ``kill``, ``wait``, ``info`` and ``image`` (#2973).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from support.image_resolution import inspect_config


class FakeDaemon:
    """A container runtime holding containers by id, refusing duplicate names."""

    def __init__(self, absence: str = "No such container") -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.created = 0
        # Docker says "No such container"; Podman "no such object".
        self.absence = absence
        # Another remover takes the container between our read and our ``rm``.
        self.rm_raced = False
        # The daemon refuses ``rm`` without saying the container is gone.
        self.rm_refused = False
        # Which daemon this is; a context switch or a new host changes it.
        self.engine = "engine-1"
        # The daemon commits a create, then its client exits 1 without a cidfile.
        self.create_response_lost = False
        # The daemon makes containers without the labels kew asked for.
        self.unlabelled = False
        # The daemon answers an inspect with labels that are not JSON.
        self.labels_unreadable = False
        # The daemon refuses every create, with these words, making nothing.
        self.create_refusal: str | None = None
        # The daemon cannot answer any inspect, saying these words (it is down).
        self.inspect_refusal: str | None = None
        # The daemon will not say who it is (``info`` fails).
        self.info_refused = False
        # Called with the container id just before an ``rm`` takes effect: what
        # another actor does, or what the ledger holds, at that moment.
        self.on_rm: Callable[[str], None] | None = None
        self.calls: list[list[str]] = []

    def add(
        self,
        cid: str,
        name: str,
        status: str,
        labels: dict[str, str] | None = None,
        daemon: str = "docker-engine-1",
    ) -> None:
        self.containers[cid] = {
            "name": name,
            "status": status,
            "labels": labels,
            "daemon": daemon,
        }

    def _find(self, target: str, runtime: str) -> str | None:
        """Only the daemon that holds a container can see it."""
        for cid, container in self.containers.items():
            if container["daemon"] == f"{runtime}-{self.engine}" and target in (
                cid,
                container["name"],
            ):
                return cid
        return None

    def run(self, command: list[str], **_: object) -> subprocess.CompletedProcess:
        verb, target = command[1], command[-1]
        self.calls.append(list(command))
        if verb == "create":
            return self._create(command)
        if verb == "info" and self.info_refused:
            return subprocess.CompletedProcess(command, 1, "", "permission denied")
        if verb == "info":
            fields = len(command[command.index("--format") + 1].split())
            answer = " ".join([f"{command[0]}-{self.engine}"] * fields)
            return subprocess.CompletedProcess(command, 0, f"{answer}\n", "")
        if verb == "inspect" and self.inspect_refusal is not None:
            return subprocess.CompletedProcess(command, 1, "", self.inspect_refusal)
        if verb == "image":
            # An image kew built carries the inputs label its pin tag names.
            config = inspect_config(target)
            image = json.dumps([{"Id": "sha256:" + "f" * 64, "Config": config}])
            return subprocess.CompletedProcess(command, 0, image, "")
        cid = self._find(target, command[0])
        if cid is None:
            return subprocess.CompletedProcess(
                command, 1, "", f"Error: {self.absence}: {target}\n"
            )
        container = self.containers[cid]
        if verb == "inspect":
            fmt = command[command.index("--format") + 1]
            if fmt == "{{.Id}}":
                return subprocess.CompletedProcess(command, 0, f"{cid}\n", "")
            if fmt == "{{.Id}} {{.Name}} {{.State.Running}}":
                running = str(container["status"] == "running").lower()
                return subprocess.CompletedProcess(
                    command, 0, f"{cid} /{container['name']} {running}\n", ""
                )
            pid = "0" if container["status"] != "running" else "4242"
            labels = json.dumps(container["labels"])
            if self.labels_unreadable:
                labels = labels[:-1]
            return subprocess.CompletedProcess(
                command, 0, f"{cid} {container['status']} {pid} {labels}\n", ""
            )
        if verb == "rm" and self.rm_raced:
            del self.containers[cid]
            return subprocess.CompletedProcess(
                command, 1, "", f"Error: {self.absence}: {target}\n"
            )
        if verb == "rm" and self.rm_refused:
            return subprocess.CompletedProcess(command, 1, "", "daemon busy")
        if verb == "rm":
            if container["status"] == "running" and "-f" not in command:
                return subprocess.CompletedProcess(command, 1, "", "is running")
            if self.on_rm is not None:
                self.on_rm(cid)
            del self.containers[cid]
            return subprocess.CompletedProcess(command, 0, cid, "")
        if verb == "kill":
            if container["status"] != "running":
                return subprocess.CompletedProcess(command, 1, "", "is not running")
            container["status"] = "exited"
            return subprocess.CompletedProcess(command, 0, cid, "")
        if verb == "wait":
            return subprocess.CompletedProcess(command, 0, "0\n", "")
        raise AssertionError(f"unexpected runtime call {command}")

    def _create(self, command: list[str]) -> subprocess.CompletedProcess:
        if self.create_refusal is not None:
            return subprocess.CompletedProcess(command, 125, "", self.create_refusal)
        name = command[command.index("--name") + 1]
        holder = self._find(name, command[0])
        if holder is not None:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                f'Error response from daemon: Conflict. The container name "/{name}" '
                f'is already in use by container "{holder}".',
            )
        self.created += 1
        cid = f"{self.created:064x}"
        labels = dict(
            command[i + 1].split("=", 1)
            for i, arg in enumerate(command)
            if arg == "--label"
        )
        if self.unlabelled:
            labels = {}
        self.add(cid, name, "created", labels or None, f"{command[0]}-{self.engine}")
        if self.create_response_lost:
            return subprocess.CompletedProcess(command, 1, "", "unexpected EOF")
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text(cid, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, cid, "")
