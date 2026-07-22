# 0014 — Execution backend: Kubernetes Jobs over a link-operator Agent namespace

- Status: Accepted
- Date: 2026-07-21
- Deciders: Nicholas Romero
- Related: ADR 0008 (execution backends / the `Runner` seam), ADR 0005 (composed images),
  ADR 0007 (per-repo secrets), ADR 0011 (per-task-clone provisioning),
  [RFC #347](https://github.com/Unsupervisedcom/panopticon/issues/347),
  [`link-operator`](https://github.com/ncrmro/link-operator) OPR-003/004/005

## Context

The local runner (ADR 0008) ties a task to one host's Docker daemon and tmux server. We want to
run tasks on a cluster with a real, quota-bounded workspace and a per-agent identity. Execution
placement is not a control-plane concern — the `Runner` interface lives in `sessionservice` (not
`core`) precisely so new backends are drop-ins (ADR 0008), and the determinism invariant holds
unchanged (the control plane makes no LLM calls; the agent runs in the spawned unit).

[`link-operator`](https://github.com/ncrmro/link-operator) already provisions, per agent, a
namespace `agent-<name>` with a runtime service account (`agent-runtime`) bound to the built-in
`admin` ClusterRole scoped to that namespace, an operator-owned `ResourceQuota`/`LimitRange` the
agent can't widen, a durable `/workspace` volume, credential projection (OPR-004), and a resolved
runtime image (OPR-003). OPR-005 specifies subagent **Jobs** launched into that namespace — a seam
link-operator has not yet implemented in code, so panopticon defines this part of the contract.

## Decision

Add a `KubernetesRunner` (`sessionservice/kubernetes_runner.py`) as a drop-in
`ContainerRunner`, alongside the local Docker+tmux runner. It is selected per host via
`--runner-type kubernetes` (`PANOPTICON_RUNNER_TYPE`); this deployment defaults to it. There is no
per-task placement routing yet (deferred — RFC #347 "task placement"): a host runs exactly one
backend.

1. **kubectl behind an injectable command-runner.** The runner shells out to `kubectl`, the same
   convention the local runner uses for `docker`/`tmux` (`kubectl exec` is the interactive attach
   surface anyway). Job manifests are emitted as **JSON** (`kubectl apply` accepts JSON) to stay
   dependency-free, and the command runner is injectable so the emitted argv + manifest are
   unit-tested without a cluster.

2. **Pre-declared, shared agent namespace; panopticon references it.** `spawn` creates a
   `batch/v1` Job (`serviceAccountName: agent-runtime`, `restartPolicy: Never`, `backoffLimit: 0`)
   in the pre-declared `agent-<name>` namespace, labeled so the agent/operator can recognize + GC
   workspace-scoped Jobs (OPR-005.3). panopticon does **not** own the `Agent` CR or the namespace;
   it only creates/deletes Jobs. `stop` deletes the Job — never the namespace (OPR-005.6). The Job
   name is deterministic (`panopticon-<task_id>`), so a respawn replaces rather than duplicates,
   and it matches the shared `session_name` convention the terminal supervisor and self-heal probes
   use.

3. **The spawned unit prepares itself (`host_prepared = False`).** The host does not clone a
   workspace or compose an image for this backend: the spawner (`_spawn_remote_container`) skips
   `prepare_workspace` + image composition and passes `workspace=None`. The pod
   (`container/pod.py`) clones its own `/workspace` (an `emptyDir`) from the repo's git URL, runs
   the agent's pre-resolved image, and starts the agent in a `tmux` session so
   `kubectl exec -it <pod> -- tmux attach` keeps the interactive surface.

4. **Provisioning moves in-container (RFC open question 3: yes).** Because the host is not the
   cluster, the host-side `Provisioner` (ADR 0011) is not wired for this backend
   (`run_host` passes `provisioner=None` when `host_prepared` is False). The pod branches
   `/workspace` and records `(branch, clone)` over REST once the agent sets the slug
   (`pod.provision_once`, mirroring `Provisioner`). The task service still only **records** the
   result.

5. **Credentials via OPR-004.** The container's credentials (`CLAUDE_CODE_OAUTH_TOKEN` etc.) come
   from a named in-namespace `Secret` projected as `envFrom` (`--k8s-credentials-secret`), not a
   host `--env-file` — secrets live in the cluster, and the reference (a name) is host-agnostic
   (ADR 0007's principle, on a different mechanism).

## Consequences

- The interactive surface survives (`kubectl exec … tmux attach`), but the terminal supervisor's
  `attach_command` (`terminal/attach.py`) still builds a plain/`ssh`-wrapped `tmux` attach —
  emitting the `kubectl exec` form is a **follow-up**; until then a k8s task is attached manually.
- Isolation + budget are real: the namespace `ResourceQuota`/`LimitRange` are hard guardrails the
  agent (and any in-namespace subagent swarm it fans out to, OPR-005) can't widen.
- The task container may itself launch a swarm of subagent Jobs in the same namespace — opaque to
  panopticon (one task → one registered container → one liveness/slug/branch).
- Deferred (RFC #347): per-task **placement** routing so several backends coexist and each task
  picks where it runs; panopticon emitting the `Agent` CR (kept pre-declared, out of band).

## Validation (first step)

Against a cluster with a hand-created `agent-demo` namespace mimicking link-operator (SA
`agent-runtime` + `admin` RoleBinding, a `panopticon-task-secrets` Secret): run the host daemon
with `--runner-type kubernetes --k8s-agent demo`, create a task, and observe the Job spawn → the
pod register liveness over in-cluster REST → set slug + record provisioning → `kubectl exec` attach
→ `stop` delete the Job (namespace untouched).
