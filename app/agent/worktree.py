"""
Git worktree lifecycle for Maestro agent isolation.
Each dispatched task gets its own checkout at:
    {project_path}/.maestro-worktrees/{task_id}/
"""
from __future__ import annotations
import glob as _glob
import logging, os, shutil, stat as _stat, subprocess, sys, threading, time
from typing import Iterable
from app.agent.config import GIT_SAFETY_BRANCH_PREFIX, GIT_ALLOWED_BASE_BRANCHES, GIT_TIMEOUT_SECONDS
from app.utils import normalize_path

logger = logging.getLogger(__name__)
_WORKTREE_SUBDIR = ".maestro-worktrees"
_worktrees_lock = threading.Lock()
_active_worktrees: dict[str, str] = {}   # task_id -> worktree_path
_gitignore_lock = threading.Lock()
_bootstrap_lock = threading.Lock()
_bootstrapped_projects: set[str] = set()
_env_setup_lock = threading.Lock()
_env_setup_done: set[str] = set()
_ghost_removal_failures: dict[str, float] = {}  # path -> timestamp of last failed removal
_GHOST_REMOVAL_COOLDOWN = 300.0  # retry ghost removal at most once per 5 minutes


# ---------------------------------------------------------------------------
# Process-locking helpers
# ---------------------------------------------------------------------------

def _find_processes_in_worktree(worktree_path: str) -> list[tuple[int, str]]:
    """Return [(pid, exe)] for all processes whose exe lives inside worktree_path."""
    norm_wt = os.name == 'nt' and (os.path.normcase(os.path.normpath(worktree_path)) + os.sep) or (os.path.normpath(worktree_path) + os.sep)
    try:
        import psutil
        hits = []
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = proc.info.get("exe") or ""
                if exe and (os.name == 'nt' and os.path.normcase(exe).startswith(norm_wt) or exe.startswith(norm_wt)):
                    hits.append((proc.pid, exe))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return hits
    except ImportError:
        pass

    # Fallback on Windows: wmic
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["wmic", "process", "get", "ProcessId,ExecutablePath", "/format:csv"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            hits = []
            for line in r.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    exe_path, pid_str = parts[1].strip(), parts[2].strip()
                    if exe_path and pid_str.isdigit():
                        if os.path.normcase(exe_path).startswith(norm_wt):
                            hits.append((int(pid_str), exe_path))
            return hits
        except Exception:
            pass
    return []


def _kill_worktree_processes(worktree_path: str, task_id: str) -> list[int]:
    """Kill all processes running from inside worktree_path. Returns list of killed PIDs."""
    procs = _find_processes_in_worktree(worktree_path)
    if not procs:
        return []
    killed: list[int] = []
    for pid, exe in procs:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5, check=False)
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
            logger.warning(
                "[worktree] killed PID %d (%s) locking worktree for task '%s'",
                pid, os.path.basename(exe), task_id,
            )
        except Exception as exc:
            logger.warning("[worktree] could not kill PID %d: %s", pid, exc)
    return killed


def _force_rmtree(worktree_dir: str, task_id: str) -> bool:
    """
    Remove worktree_dir, handling two Windows failure modes:
      1. Read-only files → chmod then retry (git objects are often read-only)
      2. DLL/pyd files locked by a subprocess from the worktree's venv → kill the
         process then retry

    Returns True if the directory is gone afterward.
    """
    def _onerror(func, path, exc_info):
        try:
            os.chmod(path, _stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    def _try_remove() -> bool:
        if not os.path.exists(worktree_dir):
            return True
        try:
            if os.path.islink(worktree_dir):
                os.unlink(worktree_dir)
            else:
                try:
                    shutil.rmtree(worktree_dir, onexc=_onerror)
                except TypeError:
                    shutil.rmtree(worktree_dir, onerror=_onerror)
            return not os.path.exists(worktree_dir)
        except Exception:
            return False

    if _try_remove():
        return True

    # Second attempt: kill processes running from inside the worktree, then retry
    killed = _kill_worktree_processes(worktree_dir, task_id)
    if killed:
        time.sleep(0.5)  # let handles close
        if _try_remove():
            return True

    return False


def _run(args, cwd, timeout=GIT_TIMEOUT_SECONDS):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def is_git_repo(path):
    """Return True if path is a git repository."""
    path = normalize_path(path)
    if not os.path.isdir(path):
        return False
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
    entries = [f"/{_WORKTREE_SUBDIR}/", "/.archive/"]
    gitignore = os.path.join(project_path, ".gitignore")
    with _gitignore_lock:
        try:
            existing = open(gitignore, encoding="utf-8").read() if os.path.exists(gitignore) else ""
            to_add = [e for e in entries if e not in existing]
            if not to_add:
                return
            with open(gitignore, "a", encoding="utf-8") as fh:
                for e in to_add:
                    fh.write(f"\n{e}\n")
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


def ensure_project_ready(project_path: str) -> bool:
    """
    One-time universal bootstrap: git init + empty initial commit so worktrees
    can branch from HEAD. Language-specific env setup is deferred to
    setup_test_environment(), called lazily before the first shell tool use.

    Returns True if the project is ready (or was already ready), False on error.
    """
    project_path = normalize_path(project_path)
    if not os.path.isdir(project_path):
        try:
            os.makedirs(project_path, exist_ok=True)
        except OSError as exc:
            logger.error("[bootstrap] failed to create directory '%s': %s", project_path, exc)
            return False

    norm = os.path.normcase(project_path)
    with _bootstrap_lock:
        if norm in _bootstrapped_projects:
            return True
        # Don't add to set yet; only if we successfully ensure it's a repo

    if not is_git_repo(project_path):
        logger.info("[bootstrap] git init '%s'", project_path)
        rc, _, err = _run(["git", "init"], project_path)
        if rc != 0:
            logger.error("[bootstrap] git init failed for '%s': %s", project_path, err)
            return False

        gitignore = os.path.join(project_path, ".gitignore")
        if not os.path.exists(gitignore):
            try:
                with open(gitignore, "w", encoding="utf-8") as f:
                    f.write("venv/\nnode_modules/\n__pycache__/\n*.pyc\ntarget/\nbuild/\n.gradle/\n")
                _run(["git", "add", ".gitignore"], project_path)
            except OSError as exc:
                logger.warning("[bootstrap] could not create .gitignore in '%s': %s", project_path, exc)

        rc, _, err = _run(["git", "commit", "--allow-empty", "-m", "chore: initial commit"], project_path)
        if rc != 0:
            # Check if it's already got commits (is_git_repo might have been false but repo existed)
            # or if commit simply failed.
            rc_check, _, _ = _run(["git", "rev-parse", "HEAD"], project_path)
            if rc_check != 0:
                logger.error("[bootstrap] initial commit failed for '%s': %s", project_path, err)
                return False

    with _bootstrap_lock:
        _bootstrapped_projects.add(norm)
    return True


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
    project_path = normalize_path(project_path)
    norm = os.path.normcase(project_path)
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
    project_path = normalize_path(project_path)
    if not is_git_repo(project_path):
        return None

    worktree_dir = normalize_path(os.path.join(project_path, _WORKTREE_SUBDIR, task_id))
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
        # Suppress repeat attempts within the cooldown window to avoid log spam on locked dirs
        now = time.time()
        last_failure = _ghost_removal_failures.get(worktree_dir, 0)
        if now - last_failure < _GHOST_REMOVAL_COOLDOWN:
            return None
        logger.warning("[worktree] directory '%s' exists but is not registered; attempting removal", worktree_dir)
        if not _force_rmtree(worktree_dir, task_id):
            logger.error("[worktree] failed to remove ghost directory '%s' (file locks remain)", worktree_dir)
            _ghost_removal_failures[worktree_dir] = now
            return None
        _ghost_removal_failures.pop(worktree_dir, None)

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
        # Check if failure is due to branch being checked out elsewhere (likely the main project_path)
        if "already checked out" in err:
            logger.info("[worktree] branch '%s' already checked out; attempting to liberate from main repo", branch_name)
            base = _resolve_base_branch(project_path)
            if base:
                # Force checkout the base branch in the main project path to free up the task branch
                rc_lib, _, err_lib = _run(["git", "checkout", base], project_path)
                if rc_lib == 0:
                    logger.info("[worktree] liberated '%s' by switching main repo to '%s'", branch_name, base)
                    # Retry worktree add
                    if _branch_exists(project_path, branch_name):
                        rc, _, err = _run(["git", "worktree", "add", worktree_dir, branch_name], project_path)
                    else:
                        rc, _, err = _run(["git", "worktree", "add", "-b", branch_name, worktree_dir, base], project_path)
                else:
                    logger.warning("[worktree] failed to liberate '%s': %s", branch_name, err_lib)

    if rc != 0:
        logger.error("[worktree] worktree add failed for '%s': %s", task_id, err)
        return None

    with _worktrees_lock:
        _active_worktrees[task_id] = worktree_dir
    logger.info("[worktree] created '%s' for task '%s'", worktree_dir, task_id)
    return worktree_dir


def teardown_task_worktree(task_id: str, project_path: str) -> None:
    """Remove the worktree for task_id. Safe to call even if no worktree was created."""
    project_path = normalize_path(project_path)
    with _worktrees_lock:
        worktree_path = _active_worktrees.pop(task_id, None)
    if worktree_path is None:
        return
    # Kill any subprocesses still running from inside this worktree's venv before
    # git tries to remove the directory — Windows DLL locks otherwise block removal.
    _kill_worktree_processes(worktree_path, task_id)
    _run(["git", "worktree", "remove", "--force", worktree_path], project_path)
    _run(["git", "worktree", "prune"], project_path)
    logger.info("[worktree] removed '%s' for task '%s'", worktree_path, task_id)


def prune_orphaned_worktrees(project_paths: Iterable[str]) -> None:
    """
    Called once at scheduler startup. Removes worktrees left over from a previous
    crashed server process.  Two passes per project:

    Pass 1 — git-registered orphans: worktrees that git still knows about but
    whose agent thread is gone (the normal crash-recovery case).

    Pass 2 — on-disk ghosts: directories under .maestro-worktrees/ that are NOT
    registered with git at all (happen when git worktree prune ran but the directory
    couldn't be deleted — e.g. a venv Python process still held the DLLs open).
    """
    for project_path in project_paths:
        if not project_path or not os.path.isdir(project_path):
            continue
        project_path = normalize_path(project_path)
        if not is_git_repo(project_path):
            continue
        worktree_base = normalize_path(os.path.join(project_path, _WORKTREE_SUBDIR))
        if not os.path.isdir(worktree_base):
            continue

        rc, out, _ = _run(["git", "worktree", "list", "--porcelain"], project_path)
        if rc != 0:
            continue

        # Collect paths git knows about
        registered: set[str] = set()
        for line in out.splitlines():
            if not line.startswith("worktree "):
                continue
            wt = os.path.normpath(line[len("worktree "):].strip())
            registered.add(wt)
            if wt.startswith(worktree_base + os.sep) or wt == worktree_base:
                logger.info("[worktree] pruning orphan '%s'", wt)
                _run(["git", "worktree", "remove", "--force", wt], project_path)

        _run(["git", "worktree", "prune"], project_path)

        # Pass 2: remove on-disk directories that git no longer knows about
        try:
            for entry in os.scandir(worktree_base):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                norm_entry = os.path.normpath(entry.path)
                if norm_entry not in registered:
                    logger.info("[worktree] removing on-disk ghost worktree '%s'", entry.path)
                    if not _force_rmtree(entry.path, entry.name):
                        logger.warning(
                            "[worktree] could not remove ghost '%s' — will retry on next dispatch",
                            entry.path,
                        )
        except OSError as exc:
            logger.warning("[worktree] error scanning '%s' for ghosts: %s", worktree_base, exc)
