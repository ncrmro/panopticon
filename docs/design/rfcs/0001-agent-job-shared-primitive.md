# RFC 0001 — "Agent Job": a shared launch primitive across link-operator, Outfitter, and panopticon

- Status: Draft — request for comments
- Date: 2026-07-21
- Related: panopticon [RFC #347](https://github.com/Unsupervisedcom/panopticon/issues/347),
  panopticon ADR 0014; link-operator OPR-002/003/004/005 and the proposed **OPR-006** (the `Run`
  launch surface); ai-outfitter (the `tasks/<id>` + `outfitter task bake` contract)

## Summary

Three systems each want to start the **same** unit of work — one headless run of a composed agent,
bound to a task, executed as a Kubernetes Job in an agent's namespace, with all state external.
They differ only in the *trigger* and *who records the run*. This RFC names that unit — the **Agent
Job** — as one contract with **two producers** and **one executor**, and lists the concrete change
each repo needs to converge on it.

Nothing here blocks today's work: panopticon already spawns the Job (ADR 0014), now shaped to this
contract; link-operator and Outfitter carry the proposals.

## The three systems, as they actually are (verified)

- **link-operator** — a k8s operator. Only `Organization` + `Agent` are real; an Agent is a
  persistent Deployment (the always-listening main loop) in a quota-bounded `agent-<name>`
  namespace with an `agent-runtime` SA bound to `admin`. Its canonical "agent job type" — the
  OPR-005 **environment** (`<org>/<project>/<environment>`) — is **design-only**. There is **no
  launch surface**: the only path is the in-namespace agent creating a Job with its admin rights,
  with nowhere for the operator to record run history / enforce concurrency (OPR-005.6).
- **ai-outfitter** — a **stateless** `resolve→compose→launch` toolchain, zero run/job state.
  `outfitter run <agent> --harness pi` launches one process; headless is pass-through today. Its
  `tasks/<id>/task.md` + `outfitter task bake` + typed `inputs` contract is **named but unbuilt**.
- **panopticon** — the deterministic task/workflow control plane + the `Runner` seam; the
  `KubernetesRunner` spawns a Job into the agent namespace (ADR 0014).

## The contract: an Agent Job

**An Agent Job is a namespaced Kubernetes Job that runs an Outfitter-composed agent *headlessly*
against a typed unit of work, in the agent's namespace, under its quota + identity, with all state
external.** Its fields:

| Field | Meaning | Source of truth |
| --- | --- | --- |
| environment | `<org>/<project>/<environment>` → agent slug + harness + workload{image, resources, timeout} | link-operator OPR-005 environment |
| inputs | **trusted identifiers only** — repo, ref/pr, run/task-id, `callback{url,auth}`; never body text | the requester (agent handler / panopticon) |
| identity | runs under the agent's namespace creds → the agent's own external identity (e.g. its GitHub account) | OPR-004 secret in the namespace |
| record | phase, jobRef, run history; cancel = delete → delete Job | the operator (OPR-006) and/or panopticon |
| execution | `outfitter run <agent> --harness pi` headless against `inputs`, one-shot | Outfitter |

## Two producers, one surface

The launch surface is a namespaced **`Run`** request (link-operator OPR-006) the operator
materializes into the Job. OPR-005 already sanctions both producers — *"a Job the agent, **or the
operator on the agent's behalf**, creates"*:

1. **The main agent's channel handlers** (in-namespace, inside-out). The persistent Deployment
   listens on email / Signal / Telegram, and a **GitHub-notification handler** creates a `Run`
   bound to a PR (`inputs {repo, pr}`), acting as the agent's own GitHub identity. It uses the
   mounted `agent-runtime` token — no new grant needed once the `Run` CRD aggregates into `admin`
   (OPR-006.5).
2. **panopticon** (external, "operator on the agent's behalf"). Its control plane creates a `Run`
   per task (`inputs {repo, task-id, callback}`) and owns the **workflow state machine + the run
   record + the callback**. panopticon stays the scheduler; the agent namespace stays the execution
   boundary.

**Executor:** Outfitter, stateless, inside the Job.

This dissolves the earlier inside-out/outside-in tension (RFC #347): the *producer* may be the agent
or an external control plane, as long as both create the same `Run`.

## What each repo changes

- **link-operator — proposed as OPR-006** (`docs/requirements/OPR-006-agent-run.md`, drafted on
  `feat/agent-run-launch-crd`): the namespaced `Run` CRD + reconciler (materialize → Job; labels
  org/project/environment/agent/parent-run; timeout; owner-ref cancel; run history; concurrency
  within quota), the missing `Project.environments` (OPR-002.4), RBAC aggregation into `admin`, and
  the headless `outfitter run` Job-entrypoint convention. Deferred/milestone-gated.
- **ai-outfitter — proposed:** make `tasks/<id>/task.md` + `outfitter task bake` + typed `inputs`
  real, plus a stable headless `--mode rpc` (structured in/out + a `callback`). Outfitter stays
  stateless — it never owns run/job state.
- **panopticon — done now (ADR 0014):** the `KubernetesRunner` emits the contract-shaped Job — the
  OPR-005.3 identity labels (`link.aioutfitter.com/organization|project|environment|agent`,
  `panopticon.parent-run`, `panopticon.task`) and the typed `inputs` as env (`PANOPTICON_INPUT_*`).
  When link-operator ships the `Run` CRD, the runner switches `kubectl apply` of the Job to `kubectl
  apply` of a `Run` — the same fields, so it's a near-drop-in — and the operator owns
  materialization + history.

## Open questions

1. Does panopticon keep creating the Job directly (interim) *or* create a `Run` and let the
   operator materialize it, once OPR-006 lands? (This RFC assumes the latter as the convergence.)
2. Where does the run record live when panopticon is the producer — panopticon's task service, the
   `Run.status`, or both (dual-write)?
3. Is the GitHub-notification handler purely agent-layer, or does link-operator want the deferred
   `Trigger`/`EventSource` → `Run` after all?
4. The Job-entrypoint convention (`outfitter run … --mode rpc`) depends on Outfitter's task
   contract — sequencing: does panopticon's pod converge on `outfitter run` before or after that
   lands?
