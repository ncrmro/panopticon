"""The in-pod bootstrap for the Kubernetes runner (RFC #347) —
``python -m panopticon.container.pod``.

The local Docker runner spreads a task across two host processes: ``docker run`` starts the
liveness entrypoint, and a separate host ``tmux`` pane execs the agent into the container. A
Kubernetes pod can't be driven from outside the same way, so this one in-pod launcher does the
equivalent inside the pod:

1. **clone** ``/workspace`` from the repo's git URL if it's empty — the host prepared no checkout
   for this backend (``host_prepared = False``), so the pod provisions its own;
2. start the **agent** in a ``tmux`` session (so ``kubectl exec -it <pod> -- tmux attach`` reaches
   the live agent pane — the interactive surface the Kubernetes backend keeps over GitHub Actions);
3. **provision in-container** — once the agent sets the task's slug, branch ``/workspace`` and
   record it over REST (the host-side ``Provisioner`` doesn't run for this backend, ADR 0011);
4. hold the **liveness** connection in the foreground (``serve``) so the pod's lifetime is the
   task's liveness — a clean stop (the agent exiting signals PID 1) or a crash drops it.

The deterministic pieces (the clone decision, the tmux argv, one provision step) are unit-tested
with fakes; the real subprocess/thread wiring in :func:`main` is injectable and only runs in a live
pod. LLM-free — the agent (the only LLM) runs in the tmux pane via
:mod:`panopticon.container.agent`.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from panopticon.client import TaskServiceClient
from panopticon.container import entrypoint
from panopticon.core.git import GitClones, branch_name
from panopticon.sessionservice.local_runner import (
    TMUX_SOCKET,
    WORKSPACE_MOUNT,
    session_name,
)

#: What the pod's tmux pane runs — the same in-container agent launcher the local runner execs, so
#: ``tmux attach`` reaches the live agent on either backend.
AGENT_COMMAND: tuple[str, ...] = ("python", "-m", "panopticon.container.agent")

#: How often the in-pod provision loop re-checks whether the agent has set the task's slug yet.
PROVISION_POLL_SECONDS = 2.0


def _subprocess_run(args: Sequence[str], *, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def workspace_is_empty(workspace: str, *, exists: Callable[[str], bool] = os.path.exists) -> bool:
    """Whether ``/workspace`` still needs cloning — true unless it already holds a git checkout.

    Idempotent-clone gate: a respawned pod that somehow kept a populated workspace (or a durable
    volume in a later iteration) isn't re-cloned over."""
    return not exists(str(Path(workspace) / ".git"))


def clone_workspace(
    git_url: str,
    workspace: str,
    *,
    run: Callable[[Sequence[str]], object] = _subprocess_run,
    exists: Callable[[str], bool] = os.path.exists,
) -> bool:
    """Clone ``git_url`` into ``workspace`` if it's empty; return whether a clone happened.

    A plain ``git clone`` (not ``--local`` — there's no host cache in the pod) leaves ``origin``
    pointed at the forge, so the agent has a correct remote before it ever has a slug (as
    spawn-prep does host-side for the local runner)."""
    if not workspace_is_empty(workspace, exists=exists):
        return False
    run(["git", "clone", git_url, workspace])
    return True


def tmux_agent_command(
    session: str,
    *,
    socket: str = TMUX_SOCKET,
    agent_command: Sequence[str] = AGENT_COMMAND,
    workdir: str = WORKSPACE_MOUNT,
) -> list[str]:
    """The ``tmux new-session`` argv that runs the agent detached in ``workdir`` — the pane
    ``kubectl exec -it <pod> -- tmux -L panopticon attach`` reaches."""
    return [
        "tmux",
        "-L",
        socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        workdir,
        *agent_command,
    ]


def provision_once(
    client: TaskServiceClient,
    task_id: str,
    workspace: str,
    *,
    git: GitClones | None = None,
) -> str | None:
    """Branch ``/workspace`` + record it once the task has a slug (else no-op) — the in-container
    half of ADR 0011 provisioning for this backend.

    Idempotent, so the poll loop can call it every tick: skips a task with no slug yet or one
    already provisioned. Mirrors :class:`~panopticon.sessionservice.provisioner.Provisioner`, but
    branches the pod's own ``/workspace`` and records that path (the recorded clone path is just a
    fact for the task service; there's no host filesystem to match)."""
    git = git or GitClones()
    task = client.get_task(task_id)
    if not task.get("slug") or task.get("provisioned"):
        return None
    branch = branch_name(task["slug"])
    git.create_branch(repo_path=workspace, branch=branch)
    client.record_provisioning(task_id, branch, workspace)
    return branch


def _provision_loop(
    client: TaskServiceClient,
    task_id: str,
    workspace: str,
    *,
    running: Callable[[], bool],
    poll: float = PROVISION_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:  # pragma: no cover - thread body over a live task service
    """Poll until the agent sets the slug, provision once, then exit (the task branches only once)."""
    while running():
        try:
            if provision_once(client, task_id, workspace):
                return
        except httpx.HTTPError:
            pass  # transient REST blip — retry next tick
        sleep(poll)


def _default_client(service_url: str) -> TaskServiceClient:
    return TaskServiceClient(httpx.Client(base_url=service_url))


def main(
    *,
    client_factory: Callable[[str], TaskServiceClient] = _default_client,
    run: Callable[[Sequence[str]], object] = _subprocess_run,
    serve: Callable[..., None] = entrypoint.serve,
) -> None:  # pragma: no cover - real subprocess/thread wiring, live pod only
    """Pod entrypoint: clone the workspace, start the agent's tmux session + the in-pod provisioner,
    then hold the liveness connection until signalled (the agent exiting signals PID 1)."""
    env = os.environ
    service_url = env["PANOPTICON_SERVICE_URL"]
    task_id = env["PANOPTICON_TASK_ID"]
    container_id = env["PANOPTICON_CONTAINER_ID"]
    runner_id = env.get("PANOPTICON_RUNNER_ID")
    client = client_factory(service_url)

    if git_url := env.get("PANOPTICON_GIT_URL"):
        clone_workspace(git_url, WORKSPACE_MOUNT, run=run)
    run(tmux_agent_command(session_name(task_id)))

    running = entrypoint._until_signalled()
    threading.Thread(
        target=_provision_loop,
        args=(client, task_id, WORKSPACE_MOUNT),
        kwargs={"running": running},
        daemon=True,
    ).start()
    serve(
        client,
        task_id,
        container_id=container_id,
        runner_id=runner_id,
        running=running,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
