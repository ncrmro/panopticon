"""Kubernetes Jobs runner (RFC #347 / ADR 0008): spawn each task as a Job in a link-operator
Agent's namespace.

A drop-in :class:`~panopticon.sessionservice.runner.ContainerRunner` alongside the local
Docker+tmux runner. Instead of a host container, ``spawn`` creates a ``batch/v1`` **Job** in a
**pre-declared** [link-operator](https://github.com/ncrmro/link-operator) Agent namespace
(``agent-<name>``), reusing everything that agent already gives it (OPR-003/004/005): the
``agent-runtime`` service account with namespace ``admin``, the ``agent-workspace``
``ResourceQuota``/``LimitRange``, and OPR-004 credential projection (a named ``Secret`` mounted as
``envFrom``). panopticon only *references* the namespace — it creates/deletes Jobs and never owns
the ``Agent`` CR or the namespace.

We shell out to ``kubectl`` behind an injectable command-runner — the same convention the local
runner uses for ``docker``/``tmux`` (``kubectl exec`` is the interactive attach surface anyway),
so the emitted argv + Job manifest are unit-testable without a cluster. Manifests are emitted as
**JSON** (``kubectl apply`` accepts JSON) to stay dependency-free. LLM-free — the agent runs in the
spawned pod (the determinism invariant).

Unlike the local path, the host does **not** prepare a workspace or build an image
(``host_prepared = False``): the pod clones its own ``/workspace`` (an ``emptyDir``) from the repo's
git URL and runs the agent's pre-resolved image (see :mod:`panopticon.container.pod`).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.local_runner import DEFAULT_IMAGE, session_name
from panopticon.sessionservice.runner import Runner

#: The service account link-operator provisions in every agent namespace, bound to the built-in
#: ``admin`` ClusterRole scoped to that namespace (OPR-003.3). Task Jobs run as it.
DEFAULT_SERVICE_ACCOUNT = "agent-runtime"

#: The in-pod command that clones the workspace, holds liveness, and runs the agent in a tmux
#: session (see :mod:`panopticon.container.pod`). Set as the container ``command`` so it overrides
#: the image's root uid-remap entrypoint — a Kubernetes pod runs unprivileged as its own user and
#: mounts no host path, so there's nothing to remap.
POD_COMMAND: tuple[str, ...] = ("python", "-m", "panopticon.container.pod")

#: Reap a finished Job (and its pod) this long after completion, so ``stop`` on the happy path is
#: automatic and the namespace doesn't fill with finished Jobs (a bounded ``agent-workspace`` quota).
DEFAULT_TTL_SECONDS = 3600


class CommandRunner(Protocol):
    """Runs a ``kubectl`` command and returns its stdout; ``check`` raises on non-zero exit.

    ``stdin`` feeds a manifest to ``kubectl apply --filename -`` (the local runner's
    :class:`~panopticon.sessionservice.local_runner.CommandRunner` has no stdin — this backend
    needs one). Injectable so tests record argv + piped manifest instead of hitting a cluster."""

    def __call__(
        self, args: Sequence[str], *, check: bool = True, stdin: str | None = None
    ) -> str: ...


def _subprocess_run(args: Sequence[str], *, check: bool = True, stdin: str | None = None) -> str:
    return subprocess.run(
        list(args), check=check, input=stdin, capture_output=True, text=True
    ).stdout


class KubernetesRunner(Runner):
    """Runs task Jobs in a pre-declared link-operator Agent namespace (RFC #347)."""

    #: The pod prepares its own workspace + uses the agent's resolved image, so the spawner skips
    #: the host-side clone/image-build path (see
    #: :class:`~panopticon.sessionservice.runner.ContainerRunner`).
    host_prepared = False

    def __init__(
        self,
        service_url: str,
        *,
        agent: str,
        namespace: str | None = None,
        runner_id: str = "kubernetes",
        image: str = DEFAULT_IMAGE,
        image_pull_policy: str = "IfNotPresent",
        credentials_secret: str | None = None,
        service_account: str = DEFAULT_SERVICE_ACCOUNT,
        kubectl: Sequence[str] = ("kubectl",),
        context: str | None = None,
        active_deadline_seconds: int | None = None,
        ttl_seconds_after_finished: int | None = DEFAULT_TTL_SECONDS,
        pod_command: Sequence[str] = POD_COMMAND,
        extra_env: Mapping[str, str] | None = None,
        run: CommandRunner = _subprocess_run,
    ) -> None:
        self._service_url = service_url
        self._agent = agent
        #: The pre-declared agent namespace (``agent-<name>``); panopticon spawns Jobs into it.
        self._namespace = namespace or f"agent-{agent}"
        self._runner_id = runner_id
        self._image = image
        #: ``imagePullPolicy`` for the pod. Defaults to ``IfNotPresent`` so a **locally-imported**
        #: image (``k3s ctr images import`` / ``kind load`` — the microVM dev path) is used as-is
        #: instead of pulled: a ``:latest`` tag otherwise defaults to ``Always`` and fails on a
        #: cluster with no registry for it. Set ``Always`` for a real registry-backed image.
        self._image_pull_policy = image_pull_policy
        #: The in-namespace Secret carrying the container's credentials (OPR-004): projected as
        #: ``envFrom`` so it reaches the agent (``CLAUDE_CODE_OAUTH_TOKEN`` etc.). ``None`` = the
        #: agent's own env already carries auth (link-operator projected it onto the namespace).
        self._credentials_secret = credentials_secret
        self._service_account = service_account
        self._kubectl = list(kubectl)
        self._context = context
        self._active_deadline_seconds = active_deadline_seconds
        self._ttl_seconds_after_finished = ttl_seconds_after_finished
        self._pod_command = list(pod_command)
        self._extra_env = dict(extra_env or {})
        self._run = run

    def _kubectl_cmd(self, *args: str) -> list[str]:
        """A ``kubectl`` argv scoped to this runner's context + namespace."""
        prefix = [*self._kubectl, *(["--context", self._context] if self._context else [])]
        return [*prefix, "--namespace", self._namespace, *args]

    def _labels(self, task_id: str) -> dict[str, str]:
        """Labels every Job (and its pod template) carries so the agent/operator can recognize +
        GC workspace-scoped Jobs (OPR-005.3), and ``kubectl`` selectors can find a task's Job."""
        return {
            "app.kubernetes.io/managed-by": "panopticon",
            "link.aioutfitter.com/agent": self._agent,
            "panopticon.task": task_id,
        }

    def _job_manifest(
        self, task_id: str, job: str, image: str, env: Mapping[str, str]
    ) -> dict[str, Any]:
        """The ``batch/v1`` Job manifest (a plain dict, serialized to JSON for ``kubectl apply``).

        ``backoffLimit: 0`` + ``restartPolicy: Never`` — a task is respawned deliberately (the host
        daemon's self-heal), not silently retried by the Job controller."""
        container: dict[str, Any] = {
            "name": "agent",
            "image": image,
            "imagePullPolicy": self._image_pull_policy,
            "command": list(self._pod_command),
            "env": [{"name": k, "value": v} for k, v in env.items()],
            "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
            "workingDir": "/workspace",
        }
        if self._credentials_secret:  # OPR-004: project the credential Secret as env
            container["envFrom"] = [{"secretRef": {"name": self._credentials_secret}}]
        spec: dict[str, Any] = {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": self._labels(task_id)},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": self._service_account,
                    "containers": [container],
                    "volumes": [{"name": "workspace", "emptyDir": {}}],
                },
            },
        }
        if self._ttl_seconds_after_finished is not None:
            spec["ttlSecondsAfterFinished"] = self._ttl_seconds_after_finished
        if self._active_deadline_seconds is not None:  # OPR-005: enforce a timeout
            spec["activeDeadlineSeconds"] = self._active_deadline_seconds
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job,
                "namespace": self._namespace,
                "labels": self._labels(task_id),
            },
            "spec": spec,
        }

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
    ) -> str:
        """Spawn the task as a Job in the agent namespace; return the Job name (the container id).

        ``git_url`` is the repo's forge — passed to the pod (``PANOPTICON_GIT_URL``) which clones its
        own ``/workspace`` from it (the host prepared nothing, unlike the local runner). ``image``
        overrides the runner's default (the agent's resolved runtime image). ``env_file`` /
        ``workspace`` / ``docker_in_docker`` are accepted for
        :class:`~panopticon.sessionservice.runner.ContainerRunner` parity but not used here:
        credentials come from the namespace's Secret (OPR-004), the workspace is an in-pod
        ``emptyDir``, and privilege is bounded by the namespace quota. ``initial_prompt`` / ``turn``
        / ``starting_model`` flow through as the same ``PANOPTICON_*`` env the local runner sets, so
        the in-pod agent launcher behaves identically. ``progress`` reports ``STARTING`` (before the
        apply) then ``AWAITING`` (once applied), mirroring the local runner."""

        def _report(phase: LifecyclePhase) -> None:
            if progress is not None:
                progress(phase)

        job = session_name(task_id)  # shared `panopticon-<id>` convention (attach/heal probes)
        env = {
            "PANOPTICON_SERVICE_URL": self._service_url,
            "PANOPTICON_TASK_ID": task_id,
            "PANOPTICON_CONTAINER_ID": job,
            "PANOPTICON_RUNNER_ID": self._runner_id,
            **self._extra_env,
        }
        if git_url:  # the pod clones its own checkout (host-side prepare_workspace is skipped)
            env["PANOPTICON_GIT_URL"] = git_url
        if initial_prompt:
            env["PANOPTICON_INITIAL_PROMPT"] = initial_prompt
        if turn:
            env["PANOPTICON_TASK_TURN"] = turn
        if starting_model:
            env["PANOPTICON_STARTING_MODEL"] = starting_model
        manifest = self._job_manifest(task_id, job, image or self._image, env)
        # Idempotent respawn: a deterministic name (task_id is already unique) means a stale Job is
        # replaced, not duplicated. `apply` creates or updates; a prior finished Job with the same
        # name is deleted first so `apply` isn't rejected for an immutable-field change.
        self._run(self._kubectl_cmd("delete", "job", job, "--ignore-not-found"), check=False)
        _report(LifecyclePhase.STARTING)
        self._run(self._kubectl_cmd("apply", "--filename", "-"), stdin=json.dumps(manifest))
        _report(LifecyclePhase.AWAITING)  # Job applied; waiting for the pod's /live registration
        return job

    def is_running(self, task_id: str) -> bool:
        """Whether the task's Job is active (a pod running) on the cluster.

        ``.status.active`` is the count of running pods; empty/``0`` (or a missing Job) means the
        task is **down** and should be respawned. Feeds the host daemon's ``reconcile``
        down-detection, exactly like the local runner's ``docker ps`` probe."""
        job = session_name(task_id)
        active = self._run(
            self._kubectl_cmd(
                "get", "job", job, "--ignore-not-found", "-o", "jsonpath={.status.active}"
            ),
            check=False,
        )
        return active.strip() not in ("", "0")

    def has_session(self, task_id: str) -> bool:
        """Whether the task's in-pod tmux session is up — feeds the host daemon's self-heal.

        A Kubernetes pod has no host tmux server to probe; the pod's own liveness is what matters,
        so this tracks :meth:`is_running` (an active pod is running the agent's tmux session). A
        finer ``kubectl exec … tmux has-session`` probe is a follow-up."""
        return self.is_running(task_id)

    def delete_workspace_contents(self, path: str) -> None:
        """No-op: the pod's ``/workspace`` is an ``emptyDir`` that dies with the pod — there is no
        host-side checkout to clean up (unlike the local runner's bind-mounted clone)."""

    def stop(self, container_id: str) -> None:
        """Delete the task's Job (idempotent). OPR-005.6: cancellation deletes the **Job**, never
        the namespace."""
        self._run(
            self._kubectl_cmd("delete", "job", container_id, "--ignore-not-found"), check=False
        )
