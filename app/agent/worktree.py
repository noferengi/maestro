"""
Git worktree lifecycle for Maestro agent isolation.
Each dispatched task gets its own checkout at:
    {project_path}/.maestro-worktrees/{task_id}/
"""
from __future__ import annotations
import glob as _glob
import logging, os, subprocess, sys, threading
from typing import Iterable
from app.agent.config import GIT_SAFETY_BRANCH_PREFIX, GIT_ALLOWED_BASE_BRANCHES, GIT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
_WORKTREE_SUBDIR = ".maestro-worktrees"
_worktrees_lock = threading.Lock()
_active_worktrees: dict[str, str] = {}   # task_id -> worktree_path
_gitignore_lock = threading.Lock()
_bootstrap_lock = threading.Lock()
_bootstrapped_projects: set[str] = set()
_env_setup_lock = threading.Lock()
_env_setup_done: set[str] = set()


def _run(args, cwd, timeout=GIT_TIMEOUT_SECONDS):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def _is_git_repo(path):
    rc, _, _ = _run(["git", "rev-parse", "--git-dir"], path)
    return rc == 0


def _resolve_base_branch(project_path):
    for branch in GIT_ALLOWED_BASE_BRANCHES:
        rc, _, _ = _run(["git", "rev-parse", "--verify", branch], project_path)
        if rc == 0:
            return branch
    return None


def _branch_exists(project_path, branch_name):
    rc, out, _ = _run(["git", "branch", "--list", branch_name], project_path)
    return rc == 0 and bool(out.strip())


def _ensure_gitignore(project_path):
    entry = f"/{_WORKTREE_SUBDIR}/"
    gitignore = os.path.join(project_path, ".gitignore")
    with _gitignore_lock:
        try:
            if os.path.exists(gitignore):
                if entry in open(gitignore, encoding="utf-8").read():
                    return
                open(gitignore, "a", encoding="utf-8").write(f"\n{entry}\n")
            else:
                open(gitignore, "w", encoding="utf-8").write(f"{entry}\n")
            _run(["git", "add", ".gitignore"], project_path)
        except OSError as exc:
            logger.warning("[worktree] could not update .gitignore: %s", exc)


def venv_python(project_path: str) -> str:
    """Return the absolute path to the venv Python for a project, or 'python' if no venv."""
    if os.name == "nt":
        candidate = os.path.join(project_path, "venv", "Scripts", "python.exe")
    else:
        candidate = os.path.join(project_path, "venv", "bin", "python")
    return candidate if os.path.isfile(candidate) else "python"


def detect_project_type(project_path: str) -> str | None:
    """
    Detect the primary language/build system from files present in project_path.
    Returns one of: 'python', 'node', 'rust', 'go', 'cpp', 'android', 'java', or None.
    """
    def has(*patterns: str) -> bool:
        return any(_glob.glob(os.path.join(project_path, p)) for p in patterns)

    if has("Cargo.toml"):
        return "rust"
    if has("go.mod"):
        return "go"
    if has("CMakeLists.txt", "*.cmake"):
        return "cpp"
    if has("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        return "android"
    if has("pom.xml"):
        return "java"
    if has("package.json"):
        return "node"
    if has("*.py", "requirements.txt", "setup.py", "pyproject.toml", "setup.cfg"):
        return "python"
    return None


def ensure_project_ready(project_path: str) -> None:
    """
    One-time universal bootstrap: git init + empty initial commit so worktrees
    can branch from HEAD. Language-specific env setup is deferred to
    setup_test_environment(), called lazily before the first shell tool use.
    """
    norm = os.path.normcase(os.path.normpath(project_path))
    with _bootstrap_lock:
        if norm in _bootstrapped_projects:
            return
        _bootstrapped_projects.add(norm)

    if not _is_git_repo(project_path):
        logger.info("[bootstrap] git init '%s'", project_path)
        _run(["git", "init"], project_path)
        gitignore = os.path.join(project_path, ".gitignore")
        if not os.path.exists(gitignore):
            try:
                open(gitignore, "w").write(
                    "venv/\nnode_modules/\n__pycache__/\n*.pyc\ntarget/\nbuild/\n.gradle/\n"
                )
                _run(["git", "add", ".gitignore"], project_path)
            except OSError:
                pass
        _run(["git", "commit", "--allow-empty", "-m", "chore: initial commit"], project_path)


def setup_test_environment(project_path: str) -> None:
    """
    Lazy, one-time per-project environment setup. Called before the first
    run_shell_indev / run_shell_build / run_shell_deps invocation.

    Detects project type from files present on disk (so for greenfield projects
    this must be called after the agent has written the initial files) and
    performs the minimal setup needed to run tests:
      - python  → create venv, install pytest + pytest-timeout (+ requirements.txt)
      - node    → npm install (if node_modules/ missing)
      - go      → go mod download
      - rust/cpp/android/java → no automatic setup (toolchains assumed present)

    When dependencies change after this runs, the agent should call
    run_shell_deps() explicitly — this function will not re-run.
    """
    norm = os.path.normcase(os.path.normpath(project_path))
    with _env_setup_lock:
        if norm in _env_setup_done:
            return
        _env_setup_done.add(norm)

    project_type = detect_project_type(project_path)
    logger.info("[env] detected project type '%s' at '%s'", project_type, project_path)

    if project_type == "python":
        venv_dir = os.path.join(project_path, "venv")
        if not os.path.isdir(venv_dir):
            logger.info("[env] creating Python venv at '%s'", venv_dir)
            rc, _, err = _run([sys.executable, "-m", "venv", "venv"], project_path)
            if rc != 0:
                logger.error("[env] venv creation failed: %s", err)
                return
        py = venv_python(project_path)
        req = os.path.join(project_path, "requirements.txt")
        if os.path.isfile(req):
            logger.info("[env] installing requirements.txt")
            _run([py, "-m", "pip", "install", "-r", "requirements.txt", "-q"], project_path)
        else:
            _run([py, "-m", "pip", "install", "pytest", "pytest-timeout", "-q"], project_path)

    elif project_type == "node":
        if not os.path.isdir(os.path.join(project_path, "node_modules")):
            logger.info("[env] running npm install at '%s'", project_path)
            _run(["npm", "install"], project_path, timeout=300)

    elif project_type == "go":
        if os.path.isfile(os.path.join(project_path, "go.mod")):
            logger.info("[env] running go mod download at '%s'", project_path)
            _run(["go", "mod", "download"], project_path, timeout=300)


def setup_task_worktree(task_id: str, project_path: str) -> str | None:
    """
    Create (or reuse) a git worktree for this task.
    Returns the worktree path, or None if the project is not a git repo
    or worktree creation fails (caller falls back to project_path).
    """
    if not _is_git_repo(project_path):
        return None

    worktree_dir = os.path.join(project_path, _WORKTREE_SUBDIR, task_id)
    branch_name = f"{GIT_SAFETY_BRANCH_PREFIX}{task_id}"

    _ensure_gitignore(project_path)

    # Check if the directory already exists
    if os.path.exists(worktree_dir):
        # Is it already a registered worktree?
        rc, out, _ = _run(["git", "worktree", "list", "--porcelain"], project_path)
        if rc == 0:
            norm_wt_dir = os.path.normpath(worktree_dir)
            is_registered = False
            for line in out.splitlines():
                if line.startswith("worktree ") and os.path.normpath(line[len("worktree "):].strip()) == norm_wt_dir:
                    is_registered = True
                    break
            
            if is_registered:
                logger.info("[worktree] reusing existing registered worktree for task '%s'", task_id)
                with _worktrees_lock:
                    _active_worktrees[task_id] = worktree_dir
                return worktree_dir
        
        # If exists but not registered, or registration check failed, try to clean it up
        logger.warning("[worktree] directory '%s' exists but is not registered; attempting removal", worktree_dir)
        import shutil
        try:
            if os.path.islink(worktree_dir):
                os.unlink(worktree_dir)
            elif os.path.isdir(worktree_dir):
                shutil.rmtree(worktree_dir)
            else:
                os.remove(worktree_dir)
        except Exception as exc:
            logger.error("[worktree] failed to remove ghost directory '%s': %s", worktree_dir, exc)
            return None

    if _branch_exists(project_path, branch_name):
        rc, _, err = _run(["git", "worktree", "add", worktree_dir, branch_name], project_path)
    else:
        base = _resolve_base_branch(project_path)
        if base is None:
            logger.warning("[worktree] no base branch for '%s' — skipping worktree", project_path)
            return None
        rc, _, err = _run(
            ["git", "worktree", "add", "-b", branch_name, worktree_dir, base],
            project_path,
        )

    if rc != 0:
        logger.error("[worktree] worktree add failed for '%s': %s", task_id, err)
        return None

    with _worktrees_lock:
        _active_worktrees[task_id] = worktree_dir
    logger.info("[worktree] created '%s' for task '%s'", worktree_dir, task_id)
    return worktree_dir


def teardown_task_worktree(task_id: str, project_path: str) -> None:
    """Remove the worktree for task_id. Safe to call even if no worktree was created."""
    with _worktrees_lock:
        worktree_path = _active_worktrees.pop(task_id, None)
    if worktree_path is None:
        return
    _run(["git", "worktree", "remove", "--force", worktree_path], project_path)
    _run(["git", "worktree", "prune"], project_path)
    logger.info("[worktree] removed '%s' for task '%s'", worktree_path, task_id)


def prune_orphaned_worktrees(project_paths: Iterable[str]) -> None:
    """
    Called once at scheduler startup. Removes any .maestro-worktrees/ entries
    left over from a previous crashed server process.
    """
    for project_path in project_paths:
        if not project_path or not os.path.isdir(project_path):
            continue
        if not _is_git_repo(project_path):
            continue
        worktree_base = os.path.normpath(os.path.join(project_path, _WORKTREE_SUBDIR))
        if not os.path.isdir(worktree_base):
            continue
        rc, out, _ = _run(["git", "worktree", "list", "--porcelain"], project_path)
        if rc != 0:
            continue
        for line in out.splitlines():
            if not line.startswith("worktree "):
                continue
            wt = os.path.normpath(line[len("worktree "):].strip())
            if wt.startswith(worktree_base + os.sep) or wt == worktree_base:
                logger.info("[worktree] pruning orphan '%s'", wt)
                _run(["git", "worktree", "remove", "--force", wt], project_path)
        _run(["git", "worktree", "prune"], project_path)
