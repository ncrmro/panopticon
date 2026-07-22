"""The execution-backend interface (ADR 0006/0008): spawn and stop task containers.

A *runner* turns a task into a running task container that connects back to the task service
(liveness) and a host tmux session the terminal controller can attach to. The container runs
the agent and decides its own slug; the runner only manages the container/tmux lifecycle and
injects the repo's secrets — it stays **LLM-free** (the determinism invariant).

Concrete adapters implement this behind the same contract:
* :class:`~panopticon.sessionservice.stub_runner.StubRunner` — runs the entrypoint in-process,
  no Docker (the walking skeleton);
* the local Docker+tmux runner (Slice 2) — a real host process.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol

from panopticon.core.models import LifecyclePhase


class Runner(ABC):
    """Spawns task containers and owns their tmux sessions (ADR 0008)."""

    @abstractmethod
    def spawn(self, task_id: str) -> str:
        """Start a container working on ``task_id``; return its container id.

        The container self-registers with the task service for liveness and chooses its slug
        in-container — the runner passes neither work nor slug in.
        """

    @abstractmethod
    def stop(self, container_id: str) -> None:
        """Stop the container and tear down its tmux session. Idempotent."""


class ContainerRunner(Protocol):
    """The concrete surface the host spawn loop drives — wider than the bare :class:`Runner` ABC.

    :class:`~panopticon.sessionservice.local_runner.LocalRunner` and
    :class:`~panopticon.sessionservice.kubernetes_runner.KubernetesRunner` both satisfy this, so the
    :class:`~panopticon.sessionservice.spawner.Spawner` can treat either as a drop-in (ADR 0008 —
    new backends implement the interface, callers don't change).

    ``host_prepared`` tells the spawner whether the **host** readies the task's workspace + image
    before spawning (``True`` — the local Docker path clones ``/workspace`` on the host and composes
    the image) or the **spawned unit** does it itself (``False`` — a Kubernetes pod clones its own
    checkout and runs the agent's pre-resolved image). When ``False`` the spawner skips
    ``prepare_workspace`` / image composition and passes ``workspace=None``.
    """

    host_prepared: bool

    def spawn(
        self,
        task_id: str,
        *,
        env_file: str | None = None,
        workspace: str | None = None,
        image: str | None = None,
        docker_in_docker: bool = False,
        initial_prompt: str | None = None,
        turn: str | None = None,
        starting_model: str | None = None,
        git_url: str | None = None,
        progress: Callable[[LifecyclePhase], None] | None = None,
    ) -> str: ...

    def stop(self, container_id: str) -> None: ...

    def is_running(self, task_id: str) -> bool: ...

    def has_session(self, task_id: str) -> bool: ...

    def delete_workspace_contents(self, path: str) -> None: ...
