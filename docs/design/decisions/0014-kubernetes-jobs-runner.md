# 0014 — Execution backend: local panopticon spawns task Jobs into a Kubernetes namespace

- Status: Accepted
- Date: 2026-07-21
- Deciders: Nicholas Romero
- Related: ADR 0008 (execution backends / the `Runner` seam), ADR 0009 (remote execution),
  ADR 0011 (per-task-clone provisioning),
  [RFC #347](https://github.com/Unsupervisedcom/panopticon/issues/347),
  [`link-operator`](https://github.com/ncrmro/link-operator) OPR-003/004/005

## Context

The local runner (ADR 0008) ties a task to one host's Docker daemon and tmux server. The goal here
is **a local panopticon (task service + host daemon on the operator's machine) that spawns each
task as a Kubernetes `batch/v1` Job in a cluster** — the control plane stays local; only the task
pods run in the cluster, and each calls back to the local task service for liveness/slug/branch.
This is a drop-in behind the `Runner` seam (ADR 0008): `core` and the determinism invariant are
untouched (the control plane makes no LLM calls; the agent runs in the spawned pod).

### What link-operator actually provides (verified against its Go, not just its docs)

The Jobs land in a link-operator Agent's namespace `agent-<name>`. link-operator's controller
(`agent_resources.go`) genuinely provisions, per Agent:

- the namespace `agent-<name>`;
- a ServiceAccount `agent-runtime` bound to the built-in **`admin`** ClusterRole scoped to that
  namespace — **the one load-bearing primitive**: admin in the namespace is what lets a local
  panopticon `kubectl apply` a Job there at all;
- a `ResourceQuota` `agent-workspace` + a `LimitRange` (a hard budget panopticon can't widen).

Three things the RFC's framing over-credited, and which panopticon therefore does itself:

1. **The image is panopticon's, not the agent's.** OPR-003's "resolved runtime image" is
   link-operator's harness image; it has no panopticon package. The pod runs a **panopticon** image
   we supply (`--k8s-image`).
2. **The workspace is an `emptyDir`, not the agent PVC.** `agent-workspace` is RWO and already
   mounted by the always-on agent Deployment, so the task pod clones its own checkout into an
   `emptyDir`.
3. **Credentials are a Secret we/the operator create in the namespace.** OPR-004 is a *wait-for-it*
   contract; the operator doesn't project a Secret onto a foreign Job. panopticon references a named
   Secret via `envFrom`.

`link-operator`'s own subagent-Job launch (OPR-005) is **design-only — no Job code exists in the
operator**, so panopticon creates **raw** Jobs (permitted by the admin SA). The net dependency on
link-operator is therefore precisely *a namespace with an admin-scoped SA and a quota*; the Agent
CR is how that namespace comes to exist (bet on link-operator's fleet/org model + a future OPR-005),
not a launch API panopticon calls.

## Decision

Add a `KubernetesRunner` (`sessionservice/kubernetes_runner.py`) as a drop-in `ContainerRunner`,
selected per host via `--runner-type kubernetes` (`PANOPTICON_RUNNER_TYPE`). No per-task placement
routing yet (deferred — RFC #347): a host runs one backend.

1. **kubectl behind an injectable command-runner.** Same convention the local runner uses for
   `docker`/`tmux` (`kubectl exec` is the interactive attach surface anyway). Job manifests are
   emitted as **JSON** (`kubectl apply` accepts JSON) to stay dependency-free; the command runner is
   injectable so argv + manifest are unit-tested without a cluster.

2. **Spawn a raw Job into the pre-declared namespace.** `serviceAccountName: agent-runtime`,
   `restartPolicy: Never`, `backoffLimit: 0` (respawn is deliberate — the host daemon's self-heal —
   not silent Job-controller retries), labeled so the agent/operator can recognize + GC it
   (OPR-005.3). Deterministic Job name (`panopticon-<task_id>`) so a respawn replaces rather than
   duplicates and it matches the shared `session_name` convention. `stop` deletes the Job — never
   the namespace (OPR-005.6). `imagePullPolicy` defaults to `IfNotPresent` so a locally-imported
   image (`k3s ctr images import` / `kind load`) is used as-is.

3. **The pod prepares itself (`host_prepared = False`).** The spawner (`_spawn_remote_container`)
   skips host-side workspace clone + image composition and passes `workspace=None`. The pod
   (`container/pod.py`) clones its own `/workspace` (an `emptyDir`) from the repo's git URL, runs the
   panopticon image, and starts the agent in a `tmux` session so
   `kubectl exec -it <pod> -- tmux attach` keeps the interactive surface.

4. **Two URLs — the control plane is local.** The pod calls back on `--container-service-url` (the
   in-container view — a host-reachable address for the local task service), while the daemon itself
   reaches the service at `--service-url` (its own local view). Nothing panopticon runs in the
   cluster.

5. **Provisioning moves in-container (RFC open question 3: yes).** The host-side `Provisioner`
   (ADR 0011) is not wired for this backend (`run_host` passes `provisioner=None` when
   `host_prepared` is False). The pod branches `/workspace` and records `(branch, clone)` over REST
   once the agent sets the slug (`pod.provision_once`). The task service still only records the
   result.

6. **Credentials via a namespace Secret (OPR-004 mechanism).** `envFrom` a named in-namespace
   Secret (`--k8s-credentials-secret`) carrying `CLAUDE_CODE_OAUTH_TOKEN` etc.

## Consequences

- The interactive surface survives (`kubectl exec … tmux attach`), but the terminal supervisor's
  `attach_command` (`terminal/attach.py`) still builds a plain/`ssh`-wrapped `tmux` attach —
  emitting the `kubectl exec` form is a **follow-up**; until then a k8s task is attached manually.
- Real isolation + budget: the namespace `ResourceQuota`/`LimitRange` are hard guardrails — shared,
  though, with link-operator's own always-on agent Deployment in that namespace.
- The dependency on link-operator is thin (namespace + admin SA + quota). If the Agent/Organization
  ceremony proves not worth it, the same runner works against any namespace that offers an
  admin-scoped SA + quota — a decision to revisit when OPR-005 lands (or doesn't).
- Deferred (RFC #347): per-task **placement** routing; panopticon emitting the `Agent` CR (kept
  pre-declared, out of band).

## Convergence on a shared "Agent Job" (RFC 0001)

The raw Job this runner creates is an interim form of a broader shared primitive
(`docs/design/rfcs/0001-agent-job-shared-primitive.md`): one headless run of a composed agent, bound
to a task, that link-operator's main agent (on a channel event) and panopticon both want to launch.
To stay forward-compatible, the emitted Job now carries the **OPR-005.3 environment-run identity** —
labels `link.aioutfitter.com/{organization,project,environment,agent}`, `panopticon.parent-run`,
`panopticon.task` — and the **OPR-006 typed inputs** as env (`PANOPTICON_INPUT_{TASK_ID,REPO,
CALLBACK_URL,AGENT}` — trusted identifiers only, never body text). The identity is configured via
`--k8s-organization/project/environment/agent-slug` (all optional).

The convergence target is link-operator's proposed **`Run` launch CRD** (OPR-006): panopticon
switches from `kubectl apply` of the Job to `kubectl apply` of a `Run` — the same fields — and the
operator owns materialization + run-history + concurrency. Until then panopticon creates the Job
directly (as "an operator on the agent's behalf," which OPR-005 sanctions).

## Validation

Against link-operator's microVM k3s (its `devenv tasks run cluster:up` + `operator:install`, with
an `Agent` applied so the namespace is genuinely operator-provisioned): run panopticon's task
service + host daemon **locally**, and `dev/k8s-demo.sh` ships the image into the VM (the shared
`images/` dir the VM auto-imports) and starts the daemon with `--runner-type kubernetes`. Because
the microVM uses QEMU user-mode networking, the pod reaches the local task service at the slirp
gateway `http://10.0.2.2:8000` (passed as `--container-service-url`). Observe: the Job spawn in
`agent-<name>` → the pod register liveness back to the local service → set slug + record
provisioning → `kubectl exec` attach → `stop` delete the Job (namespace untouched).
