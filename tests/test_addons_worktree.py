"""Tests for ensure_addons_worktree and its /api/worktree-addons-repo route.

Real, disposable git repos under tmp_path — no Docker, no network. First
coverage of this neighborhood (ensure_addons_copy/is_git_repo/
list_git_branches have none either); matches test_unit.py's "still requires
a reachable Docker daemon at import time" precondition but exercises none of
it directly.
"""
import hashlib
import subprocess

import pytest

from odoo_edu_manager import app as app_module
from odoo_edu_manager import docker_manager as dm


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def worktree_cache_dir(tmp_path, monkeypatch):
    """Every test in this file gets its own cache dir, never the real
    data/addons-worktrees — this feature is brand new, so that directory
    may not even exist yet, and tests must not pollute it either way."""
    cache_dir = tmp_path / "addons-worktrees-cache"
    monkeypatch.setattr(dm, "ADDONS_WORKTREE_CACHE_DIR", str(cache_dir))
    return str(cache_dir)


def _run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path, branches=()):
    """A throwaway repo at `path` with one commit on `main`, plus each of
    `branches` as an additional *local* branch (not checked out)."""
    path.mkdir(exist_ok=True)
    _run("git", "init", "-q", "-b", "main", str(path))
    _run("git", "-C", str(path), "config", "user.email", "t@example.com")
    _run("git", "-C", str(path), "config", "user.name", "T")
    (path / "README").write_text("x")
    _run("git", "-C", str(path), "add", "README")
    _run("git", "-C", str(path), "commit", "-q", "-m", "init")
    for b in branches:
        _run("git", "-C", str(path), "branch", b)
    return str(path)


def _expected_dest(cache_dir, source_path, checked_out_name):
    repo_name = source_path.rstrip("/").rsplit("/", 1)[-1] or "repo"
    source_hash = hashlib.sha1(source_path.encode()).hexdigest()[:10]
    return f"{cache_dir}/{repo_name}-{source_hash}/{checked_out_name}"


# --- validation --------------------------------------------------------------

def test_ensure_addons_worktree_rejects_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        dm.ensure_addons_worktree(str(tmp_path / "nope"), "main")


def test_ensure_addons_worktree_rejects_non_git_dir(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        dm.ensure_addons_worktree(str(d), "main")


@pytest.mark.parametrize("branch", ["a b", "a;rm -rf /", ""])
def test_ensure_addons_worktree_rejects_bad_branch_name(tmp_path, branch):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="invalid branch name"):
        dm.ensure_addons_worktree(repo, branch)


def test_ensure_addons_worktree_rejects_path_traversal_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="invalid branch name"):
        dm.ensure_addons_worktree(repo, "..")


# --- cache-hit check ----------------------------------------------------------

def test_ensure_addons_worktree_returns_cached_path_on_second_call(tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch"])
    dest1 = dm.ensure_addons_worktree(repo, "other-branch")
    (open(dest1 + "/marker", "w")).close()
    dest2 = dm.ensure_addons_worktree(repo, "other-branch")
    assert dest2 == dest1
    import os
    assert os.path.exists(dest2 + "/marker")


def test_ensure_addons_worktree_recreates_when_cached_dir_is_stale(tmp_path, worktree_cache_dir):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch"])
    stale = _expected_dest(worktree_cache_dir, repo, "other-branch")
    import os
    os.makedirs(stale)
    dest = dm.ensure_addons_worktree(repo, "other-branch")
    assert dest == stale
    assert dm.is_git_repo(dest)


# --- local vs. remote dispatch ------------------------------------------------

def test_ensure_addons_worktree_local_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch"])
    dest = dm.ensure_addons_worktree(repo, "other-branch")
    head = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "other-branch"


def test_ensure_addons_worktree_remote_tracking_branch_creates_local_branch(tmp_path):
    a = _init_repo(tmp_path / "a", branches=["feature-x"])
    b = str(tmp_path / "b")
    _run("git", "clone", "-q", a, b)
    # b now has local "main" + a remote-tracking "origin/feature-x", no
    # local "feature-x" yet — exactly what list_git_branches would offer.
    dest = dm.ensure_addons_worktree(b, "origin/feature-x")
    head = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "feature-x"
    upstream = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert upstream == "origin/feature-x"
    assert dm._local_branch_exists(b, "feature-x")


def test_ensure_addons_worktree_remote_branch_name_collision_with_existing_local(tmp_path):
    a = _init_repo(tmp_path / "a", branches=["feature-x"])
    b = str(tmp_path / "b")
    _run("git", "clone", "-q", a, b)
    _run("git", "-C", b, "branch", "feature-x")  # unrelated local branch, same name
    with pytest.raises(ValueError, match="already exists locally"):
        dm.ensure_addons_worktree(b, "origin/feature-x")


# --- already-checked-out-elsewhere conflict (new_branch not given) -----------

def test_ensure_addons_worktree_conflicts_with_source_repos_own_checkout(tmp_path):
    repo = _init_repo(tmp_path / "repo")  # currently on "main"
    with pytest.raises(RuntimeError, match="its own working tree"):
        dm.ensure_addons_worktree(repo, "main")


def test_ensure_addons_worktree_conflicts_with_another_worktree(tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["feature-y"])
    other = str(tmp_path / "other-worktree")
    _run("git", "-C", repo, "worktree", "add", other, "feature-y")
    with pytest.raises(RuntimeError, match="another worktree"):
        dm.ensure_addons_worktree(repo, "feature-y")


# --- new_branch: fork a new branch instead of checking one out directly ------

def test_ensure_addons_worktree_new_branch_from_currently_checked_out_base(tmp_path):
    repo = _init_repo(tmp_path / "repo")  # currently on "main" -- would conflict directly
    dest = dm.ensure_addons_worktree(repo, "main", new_branch="my-fix")
    head = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "my-fix"


def test_ensure_addons_worktree_new_branch_from_remote_base(tmp_path):
    a = _init_repo(tmp_path / "a", branches=["feature-x"])
    b = str(tmp_path / "b")
    _run("git", "clone", "-q", a, b)
    dest = dm.ensure_addons_worktree(b, "origin/feature-x", new_branch="my-fix")
    head = subprocess.run(
        ["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "my-fix"


def test_ensure_addons_worktree_new_branch_name_collision(tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch", "my-fix"])
    with pytest.raises(ValueError, match="already exists"):
        dm.ensure_addons_worktree(repo, "other-branch", new_branch="my-fix")


def test_ensure_addons_worktree_new_branch_rejects_bad_name(tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch"])
    with pytest.raises(ValueError, match="invalid new branch name"):
        dm.ensure_addons_worktree(repo, "other-branch", new_branch="a;rm -rf /")


# --- route --------------------------------------------------------------------

def test_worktree_addons_repo_route_missing_params(client):
    resp = client.post("/api/worktree-addons-repo", data={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "missing path/branch"


def test_worktree_addons_repo_route_success(client, tmp_path):
    repo = _init_repo(tmp_path / "repo", branches=["other-branch"])
    resp = client.post("/api/worktree-addons-repo", data={"path": repo, "branch": "other-branch"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["path"]
    import os
    assert os.path.isdir(data["path"])


def test_worktree_addons_repo_route_surfaces_conflict_error(client, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    resp = client.post("/api/worktree-addons-repo", data={"path": repo, "branch": "main"})
    assert resp.status_code == 400
    assert "already checked out" in resp.get_json()["error"]


def test_worktree_addons_repo_route_with_new_branch(client, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    resp = client.post(
        "/api/worktree-addons-repo",
        data={"path": repo, "branch": "main", "new_branch": "my-fix"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    head = subprocess.run(
        ["git", "-C", data["path"], "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "my-fix"
