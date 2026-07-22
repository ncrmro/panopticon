#!/usr/bin/env bash
#
# Demo: a LOCAL panopticon (task service + host daemon on your machine) spawns task **Jobs** into a
# link-operator Agent namespace running in link-operator's microVM k3s cluster (ADR 0014 / RFC
# #347). Nothing panopticon runs in the cluster — only the task pods do, and each calls back to your
# local task service.
#
# The one networking trick: link-operator's microVM uses QEMU user-mode networking, so from inside
# the guest (and its pods) the host is the slirp gateway `10.0.2.2`. That's the pod's callback URL.
#
#   ┌ your host ─────────────────────────────┐        ┌ microVM (k3s) ──────────────┐
#   │ task service  :8000  (make serve)       │◀──10.0.2.2:8000── task pod (Job)     │
#   │ host daemon   ── kubectl (KUBECONFIG) ──┼──────────────────▶ agent-<name> ns   │
#   └─────────────────────────────────────────┘        └─────────────────────────────┘
#
# Prerequisites (in the link-operator repo's `devenv shell`):
#   devenv tasks run cluster:up
#   devenv tasks run operator:install
#   kubectl apply -f config/samples/link_v1alpha1_agent.yaml   # → provisions namespace agent-<name>
# And locally, the panopticon task service must be running (another terminal):
#   make serve            # binds 0.0.0.0:8000
#
# Then:  AGENT=<name> CLAUDE_TOKEN_FILE=~/.claude-oauth-token dev/k8s-demo.sh
#
set -euo pipefail

AGENT="${AGENT:?set AGENT=<link-operator Agent name> (its namespace is agent-<name>)}"
LINK_OP_DIR="${LINK_OP_DIR:-$HOME/repos/ncrmro/link-operator}"
IMAGE="${IMAGE:-panopticon-base:latest}"
NAMESPACE="${NAMESPACE:-agent-$AGENT}"
CREDS_SECRET="${CREDS_SECRET:-panopticon-task-secrets}"
# The daemon's own view of the local task service, and the pod's callback view (slirp gateway).
SERVICE_URL="${SERVICE_URL:-http://localhost:8000}"
CONTAINER_SERVICE_URL="${CONTAINER_SERVICE_URL:-http://10.0.2.2:8000}"

SHARE="$LINK_OP_DIR/.devenv/state/link-cluster/shared"
export KUBECONFIG="${KUBECONFIG:-$SHARE/kubeconfig}"

step() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }

step "Checking the microVM cluster is reachable ($KUBECONFIG)"
[ -s "$KUBECONFIG" ] || { echo "no kubeconfig — run 'devenv tasks run cluster:up' in $LINK_OP_DIR"; exit 1; }
kubectl get --raw=/readyz >/dev/null || { echo "cluster not ready — is the microVM up?"; exit 1; }

step "Checking the local task service is up ($SERVICE_URL)"
curl --fail --silent --show-error "$SERVICE_URL/tasks" >/dev/null \
  || { echo "task service not reachable — run 'make serve' in another terminal"; exit 1; }

step "Checking the Agent namespace exists ($NAMESPACE)"
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 \
  || { echo "namespace $NAMESPACE missing — apply an Agent so link-operator provisions it"; exit 1; }

step "Building the panopticon image and shipping it into the microVM's k3s"
( cd "$(dirname "$0")/.." && make build IMAGE="${IMAGE%%:*}" )
mkdir -p "$SHARE/images" "$SHARE/imported"
archive="$SHARE/images/panopticon-dev.tar"
docker save "$IMAGE" -o "$archive"                       # k3s ctr import handles a docker-archive tar
digest="$(sha256sum "$archive" | cut -d' ' -f1)"
stamp="$SHARE/imported/panopticon-dev.tar.sha256"
step "Waiting for the in-VM importer to load it (timer runs every 5s)"
for _ in $(seq 1 24); do
  [ -s "$stamp" ] && [ "$(tr -d '\n' <"$stamp")" = "$digest" ] && break
  sleep 3
done
[ "$(tr -d '\n' <"$stamp" 2>/dev/null)" = "$digest" ] || { echo "image import timed out"; exit 1; }

step "Ensuring the credentials Secret ($NAMESPACE/$CREDS_SECRET)"
if ! kubectl -n "$NAMESPACE" get secret "$CREDS_SECRET" >/dev/null 2>&1; then
  : "${CLAUDE_TOKEN_FILE:?secret $CREDS_SECRET missing — set CLAUDE_TOKEN_FILE=<path to a claude setup-token> to create it}"
  kubectl -n "$NAMESPACE" create secret generic "$CREDS_SECRET" \
    --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CLAUDE_TOKEN_FILE")"
fi

step "Starting the host daemon — kubernetes backend (Ctrl-C to stop)"
echo "  create a task against a repo in the dashboard, then watch:"
echo "    kubectl -n $NAMESPACE get jobs,pods -w"
echo "    kubectl -n $NAMESPACE exec -it <task-pod> -- tmux -L panopticon attach"
echo
exec python -m panopticon.sessionservice.host \
  --runner-type kubernetes \
  --k8s-agent "$AGENT" \
  --k8s-namespace "$NAMESPACE" \
  --k8s-image "$IMAGE" \
  --k8s-credentials-secret "$CREDS_SECRET" \
  --service-url "$SERVICE_URL" \
  --container-service-url "$CONTAINER_SERVICE_URL"
