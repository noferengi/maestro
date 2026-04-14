"""
scripts/diagnose_import_speed.py
---------------------------------
Diagnoses why `import app.database` (and its deps) can freeze pytest startup.

Root cause context
------------------
On Windows with Defender enabled, loading a C-extension DLL (.pyd file) for
the FIRST time in a Python process can be serialised by AV scanning.  When
multiple Python processes start simultaneously (e.g. several failed pytest
runs left alive), each process queues behind the previous one's DLL scan.
The cumulative wait compounds — what takes 0.3 s alone can take 100+ s when
5 stuck processes are already holding scan locks on the same .pyd files.

What this script checks
-----------------------
1. Other Python processes currently running (warning: they compete for scans).
2. Whether sqlalchemy, greenlet, and other heavy .pyd files are already warm
   in the Windows DLL loader cache (proxy: import time < 1 s = warm).
3. Granular per-module import times so the slow culprit is immediately visible.
4. MAESTRO_TEST_DB env var state (conftest.py sets this before importing).

Usage
-----
    # From the repo root:
    venv/Scripts/python.exe scripts/diagnose_import_speed.py

    # With explicit DB redirect (mirrors conftest.py):
    MAESTRO_TEST_DB=data/test.db venv/Scripts/python.exe scripts/diagnose_import_speed.py
"""

import os
import sys
import time
import subprocess

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WARN  = "\033[33mWARN \033[0m"
OK    = "\033[32m OK  \033[0m"
INFO  = "\033[36mINFO \033[0m"
FAIL  = "\033[31mFAIL \033[0m"
SLOW_THRESHOLD_S = 2.0   # anything slower than this is flagged

_t_start = time.perf_counter()


def _elapsed() -> float:
    return time.perf_counter() - _t_start


def step(label: str):
    """Print a labelled checkpoint with the elapsed time since script start."""
    print(f"  {_elapsed():6.2f}s  {label}", flush=True)


def time_import(module_name: str, from_import: str | None = None) -> float:
    """
    Import *module_name* (or `from module_name import from_import`) and return
    elapsed seconds.  The module is imported into a throw-away namespace so
    the result is independent of prior import state in THIS process.

    Note: sys.modules caching means the SECOND call will always be ~0 s.
    This function deliberately does NOT clear sys.modules — we want to see
    real behaviour as pytest experiences it (each step builds on the last).
    """
    t0 = time.perf_counter()
    if from_import:
        exec(f"from {module_name} import {from_import}", {})
    else:
        exec(f"import {module_name}", {})
    return time.perf_counter() - t0


def flag(elapsed: float) -> str:
    if elapsed >= SLOW_THRESHOLD_S:
        return f"{FAIL}  *** SLOW: {elapsed:.2f} s ***"
    if elapsed >= 0.5:
        return f"{WARN}  {elapsed:.2f} s (moderate)"
    return f"{OK}  {elapsed:.3f} s"


# ---------------------------------------------------------------------------
# 1. Check for competing Python processes
# ---------------------------------------------------------------------------

print("\n=== diagnose_import_speed.py ===\n", flush=True)
print(f"Python:   {sys.version}", flush=True)
print(f"Exe:      {sys.executable}", flush=True)
print(f"Test DB:  {os.environ.get('MAESTRO_TEST_DB', '(not set — will use kanban.db)')}", flush=True)
print(flush=True)

print("--- [1] Competing Python processes ---", flush=True)
try:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=5
    )
    lines = [l.strip() for l in result.stdout.splitlines() if "python" in l.lower()]
    own_pid = str(os.getpid())
    others = [l for l in lines if own_pid not in l]
    if others:
        print(f"  {WARN} {len(others)} OTHER python.exe process(es) running.", flush=True)
        for l in others:
            print(f"       {l}", flush=True)
        print(f"  {WARN} Competing processes cause DLL scan serialisation on Windows.", flush=True)
        print(f"  {WARN} Kill them before running pytest to avoid 60-100 s hangs.", flush=True)
    else:
        print(f"  {OK} No other python.exe processes detected.", flush=True)
except Exception as e:
    print(f"  {INFO} Could not query processes ({e})", flush=True)

print(flush=True)

# ---------------------------------------------------------------------------
# 2. Granular import timing
# ---------------------------------------------------------------------------

print("--- [2] Import timing (cumulative from script start) ---", flush=True)

steps = [
    # (label, module, from_symbol)
    ("greenlet",                         "greenlet",                           None),
    ("sqlalchemy.util",                  "sqlalchemy.util",                    None),
    ("sqlalchemy.sql",                   "sqlalchemy.sql",                     None),
    ("sqlalchemy.orm",                   "sqlalchemy.orm",                     None),
    ("sqlalchemy (full)",                "sqlalchemy",                         "create_engine"),
    ("app.agent.config",                 "app.agent.config",                   None),
    # Set MAESTRO_TEST_DB now, same as conftest.py does before importing app.database
    ("(set MAESTRO_TEST_DB)",            None,                                 None),
    ("app.database.session",             "app.database.session",               None),
    ("app.database.models",              "app.database.models",                None),
    ("app.database.crud_tasks",          "app.database.crud_tasks",            None),
    ("app.database.crud_projects",       "app.database.crud_projects",         None),
    ("app.database.crud_infra",          "app.database.crud_infra",            None),
    ("app.database.crud_costs",          "app.database.crud_costs",            None),
    ("app.database.crud_pipeline",       "app.database.crud_pipeline",         None),
    ("app.database.crud_jobs",           "app.database.crud_jobs",             None),
    ("app.database.crud_files",          "app.database.crud_files",            None),
    ("app.database.crud_inbox",          "app.database.crud_inbox",            None),
    ("app.database (full __init__)",     "app.database",                       None),
]

# Ensure repo root is on sys.path so `import app.*` works.
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
app_dir = os.path.join(repo_root, "app")
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

total_slow = 0

for label, module, symbol in steps:
    if module is None:
        # Special marker — set env var to mirror conftest.py behaviour.
        test_db = os.path.join(repo_root, "data", "test.db")
        os.environ.setdefault("MAESTRO_TEST_DB", test_db)
        print(f"  {'':>6}   (MAESTRO_TEST_DB = {os.environ['MAESTRO_TEST_DB']})", flush=True)
        continue

    t0 = time.perf_counter()
    try:
        elapsed = time_import(module, symbol)
        marker = flag(elapsed)
        if elapsed >= SLOW_THRESHOLD_S:
            total_slow += 1
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        marker = f"{FAIL}  ERROR: {exc}"

    print(f"  {_elapsed():6.2f}s  {label:<40}  {marker}", flush=True)

print(flush=True)

# ---------------------------------------------------------------------------
# 3. SQLAlchemy C-extension status
# ---------------------------------------------------------------------------

print("--- [3] SQLAlchemy C-extension status ---", flush=True)
try:
    import sqlalchemy.util._has_cy as _cy  # type: ignore[import]
    print(f"  {OK} C-extensions loaded (cyextension present): {_cy}", flush=True)
except ImportError:
    pass

try:
    import sqlalchemy
    cy = getattr(sqlalchemy, "util", None)
    has_cy = False
    try:
        from sqlalchemy.cyextension import util as _cut  # type: ignore[import]
        has_cy = True
    except ImportError:
        pass
    status = "enabled" if has_cy else "pure-Python fallback"
    print(f"  {OK if has_cy else INFO} SQLAlchemy {sqlalchemy.__version__} C-extensions: {status}", flush=True)
except Exception as e:
    print(f"  {INFO} Could not inspect C-extension status: {e}", flush=True)

print(flush=True)

# ---------------------------------------------------------------------------
# 4. DLL / .pyd file locations
# ---------------------------------------------------------------------------

print("--- [4] Key .pyd file locations ---", flush=True)
pyd_modules = ["greenlet._greenlet", "sqlalchemy.cyextension.immutabledict"]
for mod in pyd_modules:
    try:
        import importlib.util as ilu
        spec = ilu.find_spec(mod)
        loc = spec.origin if spec else "not found"
    except Exception:
        loc = "not found"
    print(f"  {INFO} {mod}: {loc}", flush=True)

print(flush=True)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("--- Summary ---", flush=True)
total = _elapsed()
print(f"  Total elapsed: {total:.2f} s", flush=True)
if total_slow:
    print(f"  {FAIL} {total_slow} slow import(s) detected (>= {SLOW_THRESHOLD_S} s each).", flush=True)
    print(f"  {WARN} Likely cause: Windows Defender scanning .pyd DLLs cold.", flush=True)
    print(f"  {WARN} Fix: add your venv to Defender exclusions:", flush=True)
    venv = os.path.join(repo_root, "venv")
    print(f"       Add-MpPreference -ExclusionPath '{venv}'", flush=True)
    print(f"  {WARN} Or: ensure no other python.exe processes are running before pytest.", flush=True)
else:
    print(f"  {OK} All imports fast — DLLs appear warm/cached.", flush=True)
    print(f"  {INFO} If pytest still hangs, the cause is a blocking TEST, not import time.", flush=True)

print(flush=True)
