# -*- coding: utf-8 -*-
"""
inspect_cards.py -- Maestro pipeline debug tool

Usage:
    python scripts/inspect_cards.py [command] [options]

Commands:
    overview      (default) Tasks list: type, prereqs, transition results, sub records, activity
    prereqs       Prerequisite chain analysis -- what's blocking what, phantom IDs, deadlocks
    scheduler     Simulate scheduler state -- ready / blocked / stuck, grouped by LLM
    planning      Planning-stage diagnosis -- cooldowns, gate failures, PIP jobs, session exits
    activity      LLM activity timeline across all tasks  [--hours N, default 48]
    votes         Transition vote detail for all tasks    [--task TASK_ID]
    budget        LLM capacity and budget spending summary
    children      Parent->child tree with stage progress  [--task TASK_ID]
    gate          Planning gate check outcomes             [--task TASK_ID]
    all           Run all sections in sequence

Global options:
    --project, -p   Filter to a specific project name (default: all active tasks)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# -- Config -------------------------------------------------------------------
DB_PATH = "data/kanban.db"
DISPATCHABLE = {"idea", "planning", "indev", "conceptual_review", "optimization", "security", "final_review"}
DONE_STATUSES = {"completed", "accepted"}
NEVER_DISPATCH = {"completed", "accepted", "cancelled", "subdividing"}
PIPELINE_STAGES = [
    "idea", "planning", "indev", "conceptual_review",
    "optimization", "security", "final_review", "human_review", "completed",
]
_REJECTION_RETRY_COOLDOWN = 300  # seconds, matches scheduler.py

# -- DB helpers ---------------------------------------------------------------
def _postgres_configured() -> bool:
    """
    Return True if PostgreSQL is enabled via env var or maestro.ini,
    without importing the app package.
    """
    env_val = os.environ.get("MAESTRO_USE_POSTGRES", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False
    # Fall through to maestro.ini
    import configparser
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini_path = os.path.join(_root, "maestro.ini")
    if os.path.exists(ini_path):
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)
        val = cfg.get("database", "use_postgres", fallback="false").strip().lower()
        return val in ("1", "true", "yes")
    return False


def open_db():
    """
    Open a database connection.  Prefers the shared SQLAlchemy engine (supports
    both SQLite and PostgreSQL).  Falls back to a raw sqlite3 connection only
    when the app package is not importable AND PostgreSQL is not configured.
    If PostgreSQL is configured but the app package is not importable (missing
    dependencies), the script exits with an error.
    """
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        from app.database.session import engine
        from mcp_tools.helpers import _Conn
        sa_conn = engine.connect()
        cur = _Conn(sa_conn)
        return sa_conn, cur
    except Exception as exc:
        if _postgres_configured():
            print(
                "ERROR: PostgreSQL is configured but the app package failed to import.\n"
                "Ensure all dependencies are installed (venv/Scripts/pip install -r requirements.txt).\n"
                "Detail: {}".format(exc)
            )
            sys.exit(1)
        # SQLite fallback — only when postgres is NOT configured
        if not os.path.exists(DB_PATH):
            print("DB not found at " + DB_PATH)
            sys.exit(1)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn, conn.cursor()


def hdr(text, width=60):
    print("")
    print("=" * width)
    print("  " + text)
    print("=" * width)


def sep():
    print("-" * 50)


# -- Task fetch ---------------------------------------------------------------
def get_tasks(cur, project=None):
    """All active tasks, optionally filtered to one project."""
    if project:
        cur.execute(
            "SELECT tasks.id, tasks.title, tasks.type, tasks.prerequisites,"
            " tasks.parent_task_id, tasks.llm_id, tasks.budget_id,"
            " tasks.is_active, tasks.position, tasks.history"
            " FROM tasks"
            " JOIN projects ON projects.id = tasks.project_id"
            " WHERE tasks.is_active AND projects.name=?"
            " ORDER BY tasks.type, tasks.position",
            (project,),
        )
    else:
        cur.execute(
            "SELECT id, title, type, prerequisites, parent_task_id, "
            "llm_id, budget_id, is_active, position, history "
            "FROM tasks WHERE is_active ORDER BY type, position"
        )
    return list(cur.fetchall())


# -- Helpers ------------------------------------------------------------------
def get_prereqs(task):
    try:
        return json.loads(task["prerequisites"] or "[]") or []
    except Exception:
        return []


def is_done(task_type):
    return (task_type or "").lower() in DONE_STATUSES


def fmt_ts(ts):
    if not ts:
        return "--"
    return str(ts)[:16]


def since(ts_str):
    """Returns 'Xh Ym ago' from an ISO timestamp string."""
    if not ts_str:
        return "never"
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        total_min = int(delta.total_seconds() // 60)
        if total_min < 1:
            return "just now"
        if total_min < 60:
            return "{}m ago".format(total_min)
        h = total_min // 60
        m = total_min % 60
        return "{}h {}m ago".format(h, m) if m else "{}h ago".format(h)
    except Exception:
        return str(ts_str)[:16]


def age_seconds(ts_str):
    """Age of a timestamp in seconds, or None if unparseable."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


# =============================================================================
# COMMAND: overview
# =============================================================================
def cmd_overview(cur, project=None):
    hdr("Tasks -- Overview" + (" [{}]".format(project) if project else " [all projects]"))
    tasks = get_tasks(cur, project)
    task_map = {t["id"]: t for t in tasks}

    for t in tasks:
        prereqs = get_prereqs(t)
        print("")
        print("[{:20s}] {}".format(t["type"].upper(), t["title"][:60]))
        print("  id={}".format(t["id"]))
        print("  llm={}  budget={}  parent={}".format(
            t["llm_id"], t["budget_id"], t["parent_task_id"] or "--"
        ))
        print("  prereqs: {}".format(prereqs or "(none)"))

        try:
            hist = json.loads(t["history"] or "[]")
            if hist:
                last = hist[-1]
                print("  last_history: {} @ {}".format(
                    last.get("status", "?"), fmt_ts(last.get("timestamp", ""))
                ))
        except Exception:
            pass

        cur.execute(
            "SELECT outcome, transition, created_at FROM transition_results "
            "WHERE task_id=? ORDER BY created_at DESC LIMIT 5",
            (t["id"],),
        )
        trs = cur.fetchall()
        if trs:
            print("  transitions ({}):".format(len(trs)))
            for tr in trs:
                print("    {} -> {}  {}".format(tr["transition"], tr["outcome"], fmt_ts(tr["created_at"])))

        cur.execute(
            "SELECT id, status, attempt_number, created_at "
            "FROM subdivision_records WHERE parent_task_id=? ORDER BY created_at DESC LIMIT 3",
            (t["id"],),
        )
        srs = cur.fetchall()
        if srs:
            print("  subdivision records ({}):".format(len(srs)))
            for sr in srs:
                print("    #{} status={}  {}".format(
                    sr["attempt_number"], sr["status"], fmt_ts(sr["created_at"])
                ))

        cur.execute(
            "SELECT COUNT(*) as cnt, MAX(created_at) as last FROM budget_entries WHERE task_id=?",
            (t["id"],),
        )
        be = cur.fetchone()
        if be["cnt"]:
            print("  budget_entries: {} calls  last={}  ({})".format(
                be["cnt"], fmt_ts(be["last"]), since(be["last"])
            ))

        sep()

    print("\nTotal: {} tasks".format(len(tasks)))


# =============================================================================
# COMMAND: prereqs
# =============================================================================
def cmd_prereqs(cur, project=None):
    hdr("Prerequisite Chain Analysis" + (" [{}]".format(project) if project else ""))
    tasks = get_tasks(cur, project)
    task_map = {t["id"]: t for t in tasks}

    all_prereq_ids = set()
    for t in tasks:
        all_prereq_ids.update(get_prereqs(t))

    outside_ids = all_prereq_ids - set(task_map.keys())
    if outside_ids:
        placeholders = ",".join("?" * len(outside_ids))
        cur.execute(
            "SELECT id, title, type, is_active FROM tasks WHERE id IN ({})".format(placeholders),
            list(outside_ids),
        )
        for row in cur.fetchall():
            task_map[row["id"]] = dict(row)

    def resolve_prereq(pid):
        if pid not in task_map:
            return "*** PHANTOM (not in DB) ***"
        t = task_map[pid]
        active = t["is_active"] if "is_active" in t.keys() else "?"
        if not active:
            return "INACTIVE [{}]".format(t["type"])
        if is_done(t["type"]):
            return "done [{}]".format(t["type"])
        return "BLOCKING [{}] {}".format(t["type"], t["title"][:40])

    print("\nPrerequisite satisfaction per task (active/non-cancelled only):\n")

    for t in tasks:
        if (t["type"] or "").lower() == "cancelled":
            continue
        prereqs = get_prereqs(t)
        if not prereqs:
            continue
        all_done = all(
            task_map.get(p) is not None and is_done(task_map[p]["type"])
            for p in prereqs
        )
        status = "ALL DONE" if all_done else "BLOCKED"
        print("  [{:20s}] {}".format(t["type"], t["title"][:50]))
        print("    Status: {}".format(status))
        for p in prereqs:
            label = resolve_prereq(p)
            marker = "  OK" if label.startswith("done") else "  !!"
            print("    {} {}  ->  {}".format(marker, p, label))

    print("\n-- Phantom prerequisite scan (IDs not found in DB) --")
    phantoms = []
    for t in tasks:
        for p in get_prereqs(t):
            if p not in task_map:
                phantoms.append((t["id"], t["title"][:50], p))
    if phantoms:
        for tid, title, pid in phantoms:
            print("  PHANTOM: task '{}' (id={}) references missing prereq {}".format(title, tid, pid))
    else:
        print("  None found.")

    print("\n-- Transitive lock detection --")

    def is_blocked(tid):
        t = task_map.get(tid)
        if t is None:
            return False
        if is_done(t["type"]):
            return False
        for p in get_prereqs(t):
            pt = task_map.get(p)
            if pt is None or not is_done(pt["type"]):
                return True
        return False

    chains = []
    for t in tasks:
        if (t["type"] or "").lower() == "cancelled":
            continue
        for p in get_prereqs(t):
            if is_blocked(p):
                blocker = task_map.get(p)
                blocker_title = (blocker["title"] if blocker else p)[:40]
                chains.append((t["title"][:45], blocker_title))
    if chains:
        for child_title, parent_title in chains:
            print("  CHAIN: '{}' <- '{}' (itself blocked)".format(child_title, parent_title))
    else:
        print("  No transitive locks detected.")


# =============================================================================
# COMMAND: scheduler
# =============================================================================
def cmd_scheduler(cur, project=None):
    hdr("Scheduler State Simulation" + (" [{}]".format(project) if project else ""))
    tasks = get_tasks(cur, project)
    task_map = {t["id"]: t for t in tasks}

    task_ids = list(task_map.keys())

    has_transition = set()
    if task_ids:
        placeholders = ",".join("?" * len(task_ids))
        cur.execute(
            "SELECT DISTINCT task_id FROM transition_results WHERE task_id IN ({})".format(placeholders),
            task_ids,
        )
        has_transition = {r["task_id"] for r in cur.fetchall()}

    parents_with_children = set()
    if task_ids:
        placeholders = ",".join("?" * len(task_ids))
        cur.execute(
            "SELECT DISTINCT parent_task_id FROM tasks "
            "WHERE parent_task_id IN ({}) AND is_active".format(placeholders),
            task_ids,
        )
        parents_with_children = {r["parent_task_id"] for r in cur.fetchall()}

    llm_ids = {t["llm_id"] for t in tasks if t["llm_id"]}
    llm_map = {}
    if llm_ids:
        placeholders = ",".join("?" * len(llm_ids))
        cur.execute(
            "SELECT id, model, address, port, parallel_sessions FROM llms "
            "WHERE id IN ({})".format(placeholders),
            list(llm_ids),
        )
        for r in cur.fetchall():
            llm_map[r["id"]] = dict(r)

    buckets = {
        "ready":             [],
        "blocked_prereqs":   [],
        "parent_skip":       [],
        "skipped_idea":      [],
        "stuck_subdividing": [],
        "no_llm":            [],
        "no_budget":         [],
        "non_dispatchable":  [],
    }

    for t in tasks:
        tid = t["id"]
        ttype = (t["type"] or "").lower()

        if ttype == "subdividing":
            buckets["stuck_subdividing"].append(t)
            continue
        if ttype in NEVER_DISPATCH or ttype not in DISPATCHABLE:
            buckets["non_dispatchable"].append(t)
            continue
        if tid in parents_with_children:
            buckets["parent_skip"].append(t)
            continue
        if not t["llm_id"]:
            buckets["no_llm"].append(t)
            continue
        if not t["budget_id"]:
            buckets["no_budget"].append(t)
            continue
        if ttype == "idea" and tid in has_transition:
            buckets["skipped_idea"].append(t)
            continue

        prereqs = get_prereqs(t)
        unmet = [p for p in prereqs if p not in task_map or not is_done(task_map[p]["type"])]
        if unmet:
            buckets["blocked_prereqs"].append((t, unmet))
            continue

        buckets["ready"].append(t)

    def llm_label(t):
        lid = t["llm_id"]
        if lid and lid in llm_map:
            l = llm_map[lid]
            return "llm#{} {} ({}:{})".format(lid, l["model"][:25], l["address"], l["port"])
        return "llm#{}".format(lid) if lid else "(no LLM)"

    def print_task(t, indent="  "):
        print("{}[{:20s}] {}".format(indent, t["type"], t["title"][:55]))
        print("{}  {}".format(indent, llm_label(t)))

    # -- READY
    print("\n[READY] ({}) -- dispatchable, prerequisites met, no prior result".format(
        len(buckets["ready"])
    ))
    if buckets["ready"]:
        for t in buckets["ready"]:
            print_task(t)
    else:
        print("  (none)")

    # -- BLOCKED
    print("\n[BLOCKED] BY PREREQUISITES ({})".format(len(buckets["blocked_prereqs"])))
    if buckets["blocked_prereqs"]:
        for t, unmet in buckets["blocked_prereqs"]:
            print_task(t)
            for pid in unmet:
                blocker = task_map.get(pid)
                if blocker:
                    label = "[{}] {}".format(blocker["type"], blocker["title"][:40])
                else:
                    label = "{} (PHANTOM)".format(pid)
                print("    waiting on: {}".format(label))
    else:
        print("  (none)")

    # -- PARENT SKIP
    print("\n[PARENT] WITH CHILDREN -- skipped by DAG ({})".format(len(buckets["parent_skip"])))
    for t in buckets["parent_skip"]:
        print_task(t)

    # -- IDEA ALREADY TRANSITIONED
    print("\n[DONE] IDEA ALREADY TRANSITIONED -- scheduler skips re-dispatch ({})".format(
        len(buckets["skipped_idea"])
    ))
    for t in buckets["skipped_idea"]:
        cur.execute(
            "SELECT outcome, created_at FROM transition_results "
            "WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (t["id"],),
        )
        tr = cur.fetchone()
        outcome = "{} @ {}".format(tr["outcome"], fmt_ts(tr["created_at"])) if tr else "?"
        print("  [{:20s}] {}  ->  {}".format(t["type"], t["title"][:50], outcome))

    # -- STUCK SUBDIVIDING
    print("\n[STUCK] IN SUBDIVIDING -- not dispatchable, no children ({})".format(
        len(buckets["stuck_subdividing"])
    ))
    for t in buckets["stuck_subdividing"]:
        cur.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE parent_task_id=? AND is_active", (t["id"],)
        )
        n_children = cur.fetchone()["cnt"]
        hist = json.loads(t["history"] or "[]")
        created = hist[0].get("timestamp", "?") if hist else "?"
        print("  {} | {}".format(t["id"], t["title"][:50]))
        print("    children={}  created={}  ({})".format(n_children, fmt_ts(created), since(created)))

    if buckets["non_dispatchable"]:
        print("\n[!] NON-DISPATCHABLE TYPE ({}) -- not in SCHEDULER_DISPATCHABLE_TYPES".format(
            len(buckets["non_dispatchable"])
        ))
        for t in buckets["non_dispatchable"]:
            print("  [{}] {}".format(t["type"], t["title"][:55]))

    if buckets["no_llm"]:
        print("\n[!] NO LLM ASSIGNED ({})".format(len(buckets["no_llm"])))
        for t in buckets["no_llm"]:
            print("  [{}] {}".format(t["type"], t["title"][:55]))

    if buckets["no_budget"]:
        print("\n[!] NO BUDGET ASSIGNED ({})".format(len(buckets["no_budget"])))
        for t in buckets["no_budget"]:
            print("  [{}] {}".format(t["type"], t["title"][:55]))


# =============================================================================
# COMMAND: planning
# =============================================================================
def cmd_planning(cur, project=None):
    """Planning-stage diagnosis: cooldowns, gate failures, session exits, PIP jobs."""
    hdr("Planning Stage Diagnosis" + (" [{}]".format(project) if project else " [all projects]"))

    tasks = get_tasks(cur, project)
    planning_tasks = [t for t in tasks if (t["type"] or "").lower() == "planning"]

    if not planning_tasks:
        print("  No active planning-stage tasks found.")
        return

    print("  {} planning task(s) found.\n".format(len(planning_tasks)))

    for t in planning_tasks:
        tid = t["id"]
        print("=" * 55)
        print("[PLANNING] {}".format(t["title"][:60]))
        print("  id={}  llm={}  budget={}".format(tid, t["llm_id"], t["budget_id"]))

        # -- Last planning pipeline transition result (planning_to_indev)
        cur.execute(
            "SELECT outcome, created_at FROM transition_results "
            "WHERE task_id=? AND transition='planning_to_indev' "
            "ORDER BY created_at DESC LIMIT 1",
            (tid,),
        )
        tr = cur.fetchone()
        if tr:
            print("  pipeline result : {} @ {} ({})".format(
                tr["outcome"], fmt_ts(tr["created_at"]), since(tr["created_at"])
            ))
        else:
            print("  pipeline result : (no planning_to_indev result yet)")

        # -- Planning gate failure count
        cur.execute(
            "SELECT COUNT(*) as cnt, MAX(created_at) as last_fail "
            "FROM transition_results "
            "WHERE task_id=? AND transition='planning_gate' AND outcome='rejected'",
            (tid,),
        )
        gf = cur.fetchone()
        gate_fail_count = gf["cnt"] if gf else 0
        last_gate_fail_ts = gf["last_fail"] if gf else None
        print("  gate failures   : {} / 5 max".format(gate_fail_count))

        # -- Cooldown estimate
        if last_gate_fail_ts:
            age = age_seconds(last_gate_fail_ts)
            if age is not None and age < _REJECTION_RETRY_COOLDOWN:
                remaining = int(_REJECTION_RETRY_COOLDOWN - age)
                print("  cooldown        : [COOLING DOWN -- {}s remaining]".format(remaining))
            else:
                print("  cooldown        : [expired -- eligible for retry]")

        # -- Last agent session
        try:
            cur.execute(
                "SELECT agent_type, started_at, ended_at, exit_reason, turn_count, max_turns "
                "FROM agent_sessions "
                "WHERE task_id=? AND agent_type='planning' "
                "ORDER BY started_at DESC LIMIT 1",
                (tid,),
            )
            sess = cur.fetchone()
            if sess:
                running = "(running)" if not sess["ended_at"] else ""
                print("  last session    : exit={!s:15s}  turns={}/{}  started={}  {}".format(
                    sess["exit_reason"] or "?",
                    sess["turn_count"] or "?", sess["max_turns"] or "?",
                    fmt_ts(sess["started_at"]), running or since(sess["ended_at"]),
                ))
            else:
                print("  last session    : (no planning session recorded)")
        except Exception as exc:
            print("  last session    : (query error: {})".format(exc))

        # -- Active PIP resolution jobs
        try:
            cur.execute(
                "SELECT status, COUNT(*) as cnt FROM pip_resolution_jobs "
                "WHERE task_id=? AND status NOT IN ('done', 'failed') "
                "GROUP BY status",
                (tid,),
            )
            pip_rows = cur.fetchall()
            if pip_rows:
                statuses = ", ".join("{}x {}".format(r["cnt"], r["status"]) for r in pip_rows)
                print("  pip jobs active : {} [{}]".format(
                    sum(r["cnt"] for r in pip_rows), statuses
                ))
            else:
                print("  pip jobs active : none")
        except Exception:
            print("  pip jobs active : (pip_resolution_jobs table not found)")

        # -- Latest planning_results row
        try:
            cur.execute(
                "SELECT status, gate_checks, created_at FROM planning_results "
                "WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (tid,),
            )
            pr = cur.fetchone()
            if pr:
                print("  planning_result : status={}  {}".format(
                    pr["status"], since(pr["created_at"])
                ))
                try:
                    checks = json.loads(pr["gate_checks"] or "[]")
                    failed = [c for c in checks if not c.get("passed")]
                    if failed:
                        print("  gate failures   :")
                        for c in failed:
                            severity = "HARD" if c.get("hard_fail") else "soft"
                            detail = c.get("detail", "")[:80]
                            print("    [{}] {}  {}".format(severity, c.get("name", "?"), detail))
                    elif checks:
                        print("  gate checks     : all {} passed".format(len(checks)))
                except Exception:
                    pass
            else:
                print("  planning_result : (no planning_results row)")
        except Exception as exc:
            print("  planning_result : (query error: {})".format(exc))

        print("")

    print("\nDiagnosis key:")
    print("  COOLING DOWN       -- gate rejected, waiting {}s before retry".format(_REJECTION_RETRY_COOLDOWN))
    print("  exit=pip_blocked   -- PIP resolution jobs must finish before planning can re-run")
    print("  gate failures 5/5  -- next rejection will demote card back to IDEA")
    print("  exit=error         -- planning pipeline crashed; check diagnostics UI")
    print("  no session + ready -- scheduler hasn't dispatched yet (capacity or LLM issue)")


# =============================================================================
# COMMAND: activity
# =============================================================================
def cmd_activity(cur, project=None, hours=48):
    hdr("LLM Activity -- Last {}h".format(hours) + (" [{}]".format(project) if project else ""))
    tasks = get_tasks(cur, project)
    task_ids = [t["id"] for t in tasks]
    task_map = {t["id"]: t for t in tasks}

    if not task_ids:
        print("No tasks found.")
        return

    placeholders = ",".join("?" * len(task_ids))
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    cur.execute("""
        SELECT be.task_id, be.llm_id, COUNT(*) as calls,
               MIN(be.created_at) as first, MAX(be.created_at) as last
        FROM budget_entries be
        WHERE be.task_id IN ({})
          AND be.created_at > ?
        GROUP BY be.task_id, be.llm_id
        ORDER BY last DESC
    """.format(placeholders), task_ids + [cutoff])
    rows = cur.fetchall()

    if not rows:
        print("No LLM activity in the last {}h.".format(hours))
    else:
        print("{:<45} {:>5}  {:>5}  {:<16}  {:<16}  {}".format(
            "Task title", "LLM", "Calls", "First", "Last", "Since"
        ))
        print("-" * 110)
        for r in rows:
            t = task_map.get(r["task_id"])
            title = (t["title"] if t else r["task_id"])[:44]
            print("  {:<44} #{:>4}  {:>5}  {:<16}  {:<16}  {}".format(
                title, r["llm_id"], r["calls"],
                fmt_ts(r["first"]), fmt_ts(r["last"]), since(r["last"])
            ))

    active_tasks = {r["task_id"] for r in rows}
    dispatchable_idle = [
        t for t in tasks
        if t["id"] not in active_tasks and (t["type"] or "").lower() in DISPATCHABLE
    ]
    if dispatchable_idle:
        print("\n-- Dispatchable tasks with NO activity in last {}h ({}) --".format(
            hours, len(dispatchable_idle)
        ))
        for t in dispatchable_idle:
            cur.execute(
                "SELECT MAX(created_at) as last FROM budget_entries WHERE task_id=?", (t["id"],)
            )
            last = cur.fetchone()["last"]
            print("  [{:20s}] {}  last_ever={}".format(
                t["type"], t["title"][:55], since(last)
            ))


# =============================================================================
# COMMAND: votes
# =============================================================================
def cmd_votes(cur, project=None, task_id=None):
    hdr("Transition Votes")
    tasks = get_tasks(cur, project)
    task_map = {t["id"]: t for t in tasks}

    if task_id:
        target_ids = [task_id]
    else:
        target_ids = list(task_map.keys())

    any_found = False
    for tid in target_ids:
        cur.execute(
            "SELECT stage, verdict, justification, created_at FROM transition_votes "
            "WHERE task_id=? ORDER BY created_at, id",
            (tid,),
        )
        votes = cur.fetchall()
        if not votes:
            continue
        any_found = True
        t = task_map.get(tid)
        label = t["title"][:55] if t else tid
        print("\n-- {}".format(label))
        print("   id={}".format(tid))
        for v in votes:
            print("  [{:20s}] {:25s}  {}".format(
                v["stage"], v["verdict"], fmt_ts(v["created_at"])
            ))
            if v["justification"]:
                j = v["justification"]
                print("    {}".format(j[:150]))
                if len(j) > 150:
                    print("    ... ({} chars total)".format(len(j)))

    if not any_found:
        print("  No votes found for selected tasks.")


# =============================================================================
# COMMAND: budget
# =============================================================================
def cmd_budget(cur, project=None):
    hdr("LLM & Budget Summary" + (" [{}]".format(project) if project else ""))
    tasks = get_tasks(cur, project)

    llm_ids = {t["llm_id"] for t in tasks if t["llm_id"]}
    budget_ids = {t["budget_id"] for t in tasks if t["budget_id"]}

    print("\n-- LLMs --")
    for lid in sorted(llm_ids):
        cur.execute(
            "SELECT id, model, address, port, parallel_sessions, max_context FROM llms WHERE id=?",
            (lid,),
        )
        l = cur.fetchone()
        if not l:
            print("  #{}: NOT FOUND".format(lid))
            continue
        n = sum(1 for t in tasks if t["llm_id"] == lid)
        print("  #{} model={}  {}:{}".format(l["id"], l["model"], l["address"], l["port"]))
        print("     parallel_sessions={}  max_context={}  assigned_to={} tasks".format(
            l["parallel_sessions"], l["max_context"], n
        ))

    print("\n-- Budgets --")
    for bid in sorted(budget_ids):
        cur.execute("SELECT id, name, dollar_amount FROM budgets WHERE id=?", (bid,))
        b = cur.fetchone()
        if not b:
            print("  #{}: NOT FOUND".format(bid))
            continue
        cur.execute(
            "SELECT COALESCE(SUM(total_cost_microcents),0) as spent FROM expenses WHERE budget_id=?",
            (bid,),
        )
        spent_mc = cur.fetchone()["spent"] or 0
        cap = b["dollar_amount"]
        if cap == -1:
            cap_str = "UNLIMITED"
            remaining = "inf"
        else:
            cap_mc = int(cap * 100 * 1_000_000)
            remaining = "${:.4f}".format((cap_mc - spent_mc) / 100_000_000)
            cap_str = "${:.2f}".format(cap)
        spent_str = "${:.4f}".format(spent_mc / 100_000_000)
        n = sum(1 for t in tasks if t["budget_id"] == bid)
        print("  #{} {}  cap={}  spent={}  remaining={}  assigned_to={} tasks".format(
            b["id"], b["name"], cap_str, spent_str, remaining, n
        ))


# =============================================================================
# COMMAND: children
# =============================================================================
def cmd_children(cur, project=None, task_id=None):
    hdr("Parent -> Child Tree")
    tasks = get_tasks(cur, project)
    task_map = {t["id"]: t for t in tasks}

    children_of = {}
    for t in tasks:
        pid = t["parent_task_id"]
        if pid:
            children_of.setdefault(pid, []).append(t)

    def print_tree(tid, indent=0):
        t = task_map.get(tid)
        if not t:
            print("{}[MISSING: {}]".format("    " * indent, tid))
            return

        cur.execute(
            "SELECT COUNT(*) as cnt, MAX(created_at) as last FROM budget_entries WHERE task_id=?",
            (tid,),
        )
        be = cur.fetchone()
        activity = "  {} calls, last {}".format(be["cnt"], since(be["last"])) if be["cnt"] else "  (no activity)"

        cur.execute(
            "SELECT outcome FROM transition_results WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (tid,),
        )
        tr = cur.fetchone()
        outcome = "  -> {}".format(tr["outcome"]) if tr else ""

        print("{}[{:20s}] {}{}{}".format(
            "    " * indent, t["type"], t["title"][:55], outcome, activity
        ))

        for child in sorted(children_of.get(tid, []), key=lambda x: x["position"] or 0):
            print_tree(child["id"], indent + 1)

    roots = [
        t for t in tasks
        if not t["parent_task_id"] or t["parent_task_id"] not in task_map
    ]
    if task_id:
        roots = [t for t in tasks if t["id"] == task_id]

    if not roots:
        print("No root tasks found.")
        return

    for root in roots:
        print("")
        print_tree(root["id"])


# =============================================================================
# COMMAND: gate
# =============================================================================
def cmd_gate(cur, project=None, task_id=None):
    """Show planning gate check outcomes stored in planning_results.gate_checks."""
    hdr("Planning Gate Checks")

    try:
        if task_id:
            cur.execute(
                "SELECT id, task_id, status, gate_checks, created_at FROM planning_results "
                "WHERE task_id=? ORDER BY created_at DESC LIMIT 5",
                (task_id,),
            )
        else:
            cur.execute(
                "SELECT id, task_id, status, gate_checks, created_at FROM planning_results "
                "WHERE gate_checks IS NOT NULL ORDER BY created_at DESC LIMIT 20"
            )
    except Exception as exc:
        print("  ERROR querying planning_results: {}".format(exc))
        return

    rows = cur.fetchall()
    if not rows:
        print("  No planning gate results found{}.".format(
            " for task {}".format(task_id) if task_id else ""
        ))
        return

    for row in rows:
        try:
            checks = json.loads(row["gate_checks"] or "[]")
        except (json.JSONDecodeError, TypeError):
            checks = []

        passed_count = sum(1 for c in checks if c.get("passed"))
        print("\n[{}]  {}  status={}  {}/{} checks passed".format(
            row["task_id"],
            fmt_ts(row["created_at"]),
            row["status"],
            passed_count,
            len(checks),
        ))

        for c in checks:
            if c.get("passed"):
                icon = "PASS    "
            elif c.get("hard_fail"):
                icon = "HARD-FAIL"
            else:
                icon = "soft-fail"
            detail = c.get("detail", "")
            print("  [{:9s}] {:30s}  {}".format(
                icon, c.get("name", "?"), detail[:80]
            ))
            if len(detail) > 80:
                print("             {}".format(detail[80:160]))


# =============================================================================
# Entry point
# =============================================================================
COMMANDS = {
    "overview":  lambda cur, args: cmd_overview(cur, args.project),
    "prereqs":   lambda cur, args: cmd_prereqs(cur, args.project),
    "scheduler": lambda cur, args: cmd_scheduler(cur, args.project),
    "planning":  lambda cur, args: cmd_planning(cur, args.project),
    "activity":  lambda cur, args: cmd_activity(cur, args.project, hours=args.hours),
    "votes":     lambda cur, args: cmd_votes(cur, args.project, task_id=args.task),
    "budget":    lambda cur, args: cmd_budget(cur, args.project),
    "children":  lambda cur, args: cmd_children(cur, args.project, task_id=args.task),
    "gate":      lambda cur, args: cmd_gate(cur, args.project, task_id=args.task),
    "all": lambda cur, args: [
        cmd_overview(cur, args.project),
        cmd_prereqs(cur, args.project),
        cmd_scheduler(cur, args.project),
        cmd_planning(cur, args.project),
        cmd_activity(cur, args.project, hours=args.hours),
        cmd_votes(cur, args.project),
        cmd_budget(cur, args.project),
        cmd_children(cur, args.project),
        cmd_gate(cur, args.project),
    ],
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Maestro pipeline debug tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="overview",
        choices=list(COMMANDS.keys()),
        help="What to display (default: overview)",
    )
    parser.add_argument(
        "--project", "-p",
        default=None,
        help="Filter to a specific project name (default: all projects)",
    )
    parser.add_argument(
        "--task", "-t",
        default=None,
        help="Filter to a specific task ID (used by: votes, children, gate)",
    )
    parser.add_argument(
        "--hours", "-H",
        type=int,
        default=48,
        help="Activity window in hours (used by: activity, default: 48)",
    )
    args = parser.parse_args()

    conn, cur = open_db()
    try:
        COMMANDS[args.command](cur, args)
    finally:
        conn.close()
