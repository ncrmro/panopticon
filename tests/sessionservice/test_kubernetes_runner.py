"""KubernetesRunner: unit tests pin the emitted kubectl commands + the Job manifest it applies.

No cluster — the command runner records argv + the piped manifest instead of running ``kubectl``
(the same pattern ``test_local_runner`` uses for ``docker``/``tmux``). A live-cluster path is a
``skipif`` integration test (RFC #347's first-step validation); it is not run in CI."""

from __future__ import annotations

import json
from collections.abc import Sequence

from panopticon.core.models import LifecyclePhase
from panopticon.sessionservice.kubernetes_runner import KubernetesRunner
from panopticon.sessionservice.runner import ContainerRunner, Runner


class _Recorder:
    """An injectable kubectl CommandRunner that records calls (argv + piped manifest)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool, str | None]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True, stdin: str | None = None) -> str:
        self.calls.append((list(args), check, stdin))
        return ""


def _runner(rec: _Recorder, **kwargs: object) -> KubernetesRunner:
    return KubernetesRunner(
        "http://panopticon.panopticon.svc:8000",
        agent="researcher",
        runner_id="k8s-1",
        image="registry/panopticon:1",
        credentials_secret="panopticon-task-secrets",
        run=rec,
        **kwargs,  # type: ignore[arg-type]
    )


def test_kubernetes_runner_conforms_to_the_container_runner_protocol() -> None:
    assert issubclass(KubernetesRunner, Runner)
    runner: ContainerRunner = _runner(_Recorder())  # structural: satisfies the wider surface
    assert runner.host_prepared is False  # the pod preps its own workspace + image


def test_spawn_deletes_any_stale_job_then_applies_the_manifest() -> None:
    rec = _Recorder()
    container_id = _runner(rec).spawn("t1", git_url="https://forge/r1.git")

    assert container_id == "panopticon-t1"  # the shared name; also the tmux session + heal probe
    (delete, delete_check, _), (apply, _, manifest_json) = rec.calls
    # a stale Job of the same name is cleared first so `apply` isn't rejected (idempotent respawn)
    assert delete[-4:] == ["delete", "job", "panopticon-t1", "--ignore-not-found"]
    assert delete_check is False
    assert apply[-3:] == ["apply", "--filename", "-"]
    # both are scoped to the pre-declared agent namespace
    assert delete[delete.index("--namespace") + 1] == "agent-researcher"
    assert apply[apply.index("--namespace") + 1] == "agent-researcher"
    assert manifest_json is not None


def test_spawn_manifest_targets_the_agent_namespace_sa_secret_and_workspace() -> None:
    rec = _Recorder()
    _runner(rec).spawn(
        "t1",
        git_url="https://forge/r1.git",
        initial_prompt="do the thing",
        turn="agent",
        starting_model="opus",
    )
    manifest = json.loads(rec.calls[1][2])

    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["namespace"] == "agent-researcher"
    assert manifest["metadata"]["name"] == "panopticon-t1"
    # labels let the agent/operator recognize + GC workspace-scoped Jobs (OPR-005.3)
    labels = manifest["metadata"]["labels"]
    assert labels["panopticon.task"] == "t1"
    assert labels["link.aioutfitter.com/agent"] == "researcher"
    assert labels["app.kubernetes.io/managed-by"] == "panopticon"

    spec = manifest["spec"]
    assert spec["backoffLimit"] == 0  # respawn is deliberate (host self-heal), not Job-controller
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "agent-runtime"  # link-operator's namespace-admin SA
    assert pod["volumes"] == [{"name": "workspace", "emptyDir": {}}]

    container = pod["containers"][0]
    assert container["image"] == "registry/panopticon:1"
    assert container["command"] == ["python", "-m", "panopticon.container.pod"]
    assert container["workingDir"] == "/workspace"
    assert container["volumeMounts"] == [{"name": "workspace", "mountPath": "/workspace"}]
    # OPR-004 credential projection — the Secret carries CLAUDE_CODE_OAUTH_TOKEN etc.
    assert container["envFrom"] == [{"secretRef": {"name": "panopticon-task-secrets"}}]

    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["PANOPTICON_SERVICE_URL"] == "http://panopticon.panopticon.svc:8000"
    assert env["PANOPTICON_TASK_ID"] == "t1"
    assert env["PANOPTICON_CONTAINER_ID"] == "panopticon-t1"
    assert env["PANOPTICON_RUNNER_ID"] == "k8s-1"
    assert env["PANOPTICON_GIT_URL"] == "https://forge/r1.git"  # the pod clones its own workspace
    assert env["PANOPTICON_INITIAL_PROMPT"] == "do the thing"
    assert env["PANOPTICON_TASK_TURN"] == "agent"
    assert env["PANOPTICON_STARTING_MODEL"] == "opus"


def test_image_pull_policy_defaults_to_if_not_present_for_locally_imported_images() -> None:
    rec = _Recorder()
    _runner(rec).spawn("t1")  # default policy
    container = json.loads(rec.calls[1][2])["spec"]["template"]["spec"]["containers"][0]
    # IfNotPresent so a `k3s ctr import` / `kind load` image is used as-is (Always would try to pull)
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_image_pull_policy_is_configurable() -> None:
    rec = _Recorder()
    KubernetesRunner("http://svc:8000", agent="a", image_pull_policy="Always", run=rec).spawn("t1")
    container = json.loads(rec.calls[1][2])["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == "Always"  # registry-backed image


def test_typed_inputs_are_projected_as_env_trusted_identifiers_only() -> None:
    rec = _Recorder()
    _runner(rec).spawn("t1", git_url="https://forge/r1.git")
    container = json.loads(rec.calls[1][2])["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    # OPR-006 typed inputs: identifiers + callback, forward-compatible with a `Run` reconciler
    assert env["PANOPTICON_INPUT_TASK_ID"] == "t1"
    assert env["PANOPTICON_INPUT_REPO"] == "https://forge/r1.git"
    assert env["PANOPTICON_INPUT_CALLBACK_URL"] == "http://panopticon.panopticon.svc:8000"


def test_environment_identity_is_stamped_as_labels_when_configured() -> None:
    rec = _Recorder()
    KubernetesRunner(
        "http://svc:8000",
        agent="researcher-ns",
        organization="acme",
        project="widgets",
        environment="engineer",
        agent_slug="engineer",
        runner_id="k8s-1",
        run=rec,
    ).spawn("t1")
    manifest = json.loads(rec.calls[1][2])
    labels = manifest["metadata"]["labels"]
    # OPR-005.3 environment-run identity (org/project/environment/agent) + parent-run + task
    assert labels["link.aioutfitter.com/organization"] == "acme"
    assert labels["link.aioutfitter.com/project"] == "widgets"
    assert labels["link.aioutfitter.com/environment"] == "engineer"
    assert labels["link.aioutfitter.com/agent"] == "researcher-ns"
    assert labels["panopticon.parent-run"] == "k8s-1"
    assert labels["panopticon.task"] == "t1"
    # the Dotagents agent the headless runtime should run (OPR-006.6)
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["PANOPTICON_INPUT_AGENT"] == "engineer"


def test_environment_identity_labels_omitted_when_unset() -> None:
    rec = _Recorder()
    _runner(rec).spawn("t1")  # no org/project/environment configured
    labels = json.loads(rec.calls[1][2])["metadata"]["labels"]
    assert "link.aioutfitter.com/organization" not in labels
    assert "link.aioutfitter.com/project" not in labels
    assert "link.aioutfitter.com/environment" not in labels
    assert labels["panopticon.parent-run"] == "k8s-1"  # always present


def test_spawn_reports_starting_then_awaiting_via_the_progress_callback() -> None:
    phases: list[LifecyclePhase] = []
    _runner(_Recorder()).spawn("t1", progress=phases.append)
    assert phases == [LifecyclePhase.STARTING, LifecyclePhase.AWAITING]


def test_no_credentials_secret_omits_envfrom() -> None:
    rec = _Recorder()
    KubernetesRunner("http://svc:8000", agent="a", run=rec).spawn("t1")
    container = json.loads(rec.calls[1][2])["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container  # auth then comes from the namespace's own agent env


def test_context_and_namespace_override() -> None:
    rec = _Recorder()
    KubernetesRunner(
        "http://svc:8000", agent="a", namespace="custom-ns", context="prod", run=rec
    ).spawn("t1")
    apply = rec.calls[1][0]
    assert apply[:3] == ["kubectl", "--context", "prod"]
    assert apply[apply.index("--namespace") + 1] == "custom-ns"


def test_active_deadline_and_ttl_are_set_when_configured() -> None:
    rec = _Recorder()
    KubernetesRunner(
        "http://svc:8000",
        agent="a",
        active_deadline_seconds=1800,
        ttl_seconds_after_finished=600,
        run=rec,
    ).spawn("t1")
    spec = json.loads(rec.calls[1][2])["spec"]
    assert spec["activeDeadlineSeconds"] == 1800  # OPR-005: a Job must enforce a timeout
    assert spec["ttlSecondsAfterFinished"] == 600


def test_stop_deletes_the_job_idempotently_never_the_namespace() -> None:
    rec = _Recorder()
    _runner(rec).stop("panopticon-t1")
    ((delete, check, _),) = rec.calls
    assert delete[-4:] == ["delete", "job", "panopticon-t1", "--ignore-not-found"]
    assert check is False  # tolerate an already-gone Job
    assert "delete" in delete and "namespace" not in delete[delete.index("delete") + 1 :]


def test_is_running_reads_the_jobs_active_pod_count() -> None:
    class _Active(_Recorder):
        def __call__(
            self, args: Sequence[str], *, check: bool = True, stdin: str | None = None
        ) -> str:
            super().__call__(args, check=check, stdin=stdin)
            return "1"  # one active pod

    rec = _Active()
    runner = _runner(rec)
    assert runner.is_running("t1") is True
    get = rec.calls[0][0]
    assert get[-6:] == [
        "get",
        "job",
        "panopticon-t1",
        "--ignore-not-found",
        "-o",
        "jsonpath={.status.active}",
    ]


def test_is_running_false_when_no_active_pods() -> None:
    # default recorder returns "" → a missing/finished Job → down (respawn)
    assert _runner(_Recorder()).is_running("t1") is False


def test_delete_workspace_contents_is_a_noop() -> None:
    rec = _Recorder()
    _runner(rec).delete_workspace_contents("/anything")
    assert rec.calls == []  # the emptyDir dies with the pod; nothing to clean up host-side
