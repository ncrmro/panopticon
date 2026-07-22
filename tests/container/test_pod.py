"""The in-pod bootstrap for the Kubernetes runner (RFC #347): unit tests pin the deterministic
pieces — the clone-if-empty decision, the tmux agent argv, and one in-container provision step —
with fakes. The real subprocess/thread wiring in ``main`` is injectable and only runs in a live
pod (no Docker/tmux/git/cluster in CI). No LLM."""

from __future__ import annotations

from collections.abc import Sequence

from panopticon.container import pod


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> object:
        self.calls.append(list(args))
        return None


def test_clone_workspace_clones_when_empty() -> None:
    run = _Runner()
    cloned = pod.clone_workspace(
        "https://forge/r1.git", "/workspace", run=run, exists=lambda _p: False
    )
    assert cloned is True
    assert run.calls == [["git", "clone", "https://forge/r1.git", "/workspace"]]


def test_clone_workspace_skips_a_populated_workspace() -> None:
    run = _Runner()
    # `/workspace/.git` already present → a respawn kept the checkout; don't clone over it
    cloned = pod.clone_workspace(
        "https://forge/r1.git", "/workspace", run=run, exists=lambda p: p.endswith(".git")
    )
    assert cloned is False
    assert run.calls == []


def test_tmux_agent_command_runs_the_agent_launcher_detached_in_the_workspace() -> None:
    argv = pod.tmux_agent_command("panopticon-t1")
    assert argv == [
        "tmux",
        "-L",
        "panopticon",
        "new-session",
        "-d",
        "-s",
        "panopticon-t1",
        "-c",
        "/workspace",
        "python",
        "-m",
        "panopticon.container.agent",
    ]


class _FakeClient:
    def __init__(self, task: dict[str, object]) -> None:
        self._task = task
        self.recorded: list[tuple[str, str, str]] = []

    def get_task(self, task_id: str) -> dict[str, object]:
        return self._task

    def record_provisioning(self, task_id: str, branch: str, clone: str) -> dict[str, object]:
        self.recorded.append((task_id, branch, clone))
        return self._task


class _FakeGit:
    def __init__(self) -> None:
        self.branched: list[tuple[str, str]] = []

    def create_branch(self, *, repo_path: str, branch: str) -> None:
        self.branched.append((repo_path, branch))


def test_provision_once_branches_and_records_when_the_slug_is_set() -> None:
    client = _FakeClient({"id": "t1", "slug": "add-widget", "provisioned": False})
    git = _FakeGit()
    branch = pod.provision_once(client, "t1", "/workspace", git=git)  # type: ignore[arg-type]
    assert branch == "panopticon/add-widget"
    assert git.branched == [("/workspace", "panopticon/add-widget")]
    assert client.recorded == [("t1", "panopticon/add-widget", "/workspace")]


def test_provision_once_noops_before_a_slug_exists() -> None:
    client = _FakeClient({"id": "t1", "slug": None, "provisioned": False})
    git = _FakeGit()
    assert pod.provision_once(client, "t1", "/workspace", git=git) is None  # type: ignore[arg-type]
    assert git.branched == []
    assert client.recorded == []


def test_provision_once_noops_when_already_provisioned() -> None:
    client = _FakeClient({"id": "t1", "slug": "add-widget", "provisioned": True})
    git = _FakeGit()
    assert pod.provision_once(client, "t1", "/workspace", git=git) is None  # type: ignore[arg-type]
    assert client.recorded == []  # idempotent — the branch already exists
