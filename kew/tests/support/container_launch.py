"""Launch doubles implement the complete ``sandbox.launch_container`` signature."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kew.credential_issuer import BoundedRunKey
from kew.dispatch_admission import PendingAdmission
from kew.sandbox import ContainedExecution


@dataclass
class ContainerLaunchCapture:
    """Capture argv and admission without starting a runtime client.

    Keep the keyword arguments identical to ``sandbox.launch_container``.
    Admission carries cancellation ownership; callers must always pass it.
    Registration and preparation callbacks must reach the same launch boundary.
    """

    handle: object = field(default_factory=object)
    order: list[str] | None = None
    argvs: list[list[str]] = field(default_factory=list)
    admissions: list[PendingAdmission] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)
    credentials: list[BoundedRunKey | None] = field(default_factory=list)
    registrations: list[Callable[[subprocess.Popen[str]], None]] = field(
        default_factory=list
    )
    preparations: list[Callable[[list[str]], ContainedExecution]] = field(
        default_factory=list
    )
    separated_stderr: list[bool] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        credential: BoundedRunKey | None = None,
        container_name: str | None = None,
        container_id: str | None = None,
        checkout_dir: Path | None = None,
        admission: PendingAdmission | None = None,
        on_spawn: Callable[[subprocess.Popen[str]], None] | None = None,
        prepare: Callable[[list[str]], ContainedExecution] | None = None,
        separate_stderr: bool = False,
    ) -> object:
        assert admission is not None
        assert callable(on_spawn)
        self.registrations.append(on_spawn)
        if container_id is None:
            assert callable(prepare)
            self.preparations.append(prepare)
        self.argvs.append(list(argv))
        self.admissions.append(admission)
        self.envs.append(env)
        self.credentials.append(credential)
        self.separated_stderr.append(separate_stderr)
        if self.order is not None:
            self.order.append("launch")
        return self.handle
