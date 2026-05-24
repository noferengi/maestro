description = "Seed reviewer and persona definitions into pipeline_stages.config for SW Dev template"

import json as _json

# Seeds per-reviewer and per-persona definitions into pipeline_stages.config for the
# Software Development template.  Only fills keys that are absent — user-edited values
# are never overwritten.
#
# Stages updated:
#   security         — reviewers: [offensive, defensive, compliance], tally_strategy: "veto"
#   conceptual_review — reviewers: [l1_architecture, l2_security, l3_performance, l4_api_interface]
#   final_review     — reviewers: [functional, code_quality, integration]
#   planning         — personas: [correctness, security, clarity, performance, architecture]
#
# The "system_prompt" for each reviewer/persona is the full text currently embedded in
# the Python constants, so the pipeline editor's properties panel shows the live defaults.

_SECURITY_REVIEWERS = [
    {
        "name": "offensive",
        "system_prompt": (
            "You are a security expert reviewing from an offensive (red-team attacker) perspective. "
            "Focus on OWASP Top 10 vulnerabilities, input validation gaps, secret/credential exposure, "
            "command injection, path traversal, and data exfiltration vectors. "
            "Use submit_work to output your verdict when ready."
        ),
    },
    {
        "name": "defensive",
        "system_prompt": (
            "You are a security expert reviewing from a defensive (blue-team defender) perspective. "
            "Focus on auth/authz coverage, error handling and info disclosure, dependency CVEs, "
            "encryption at rest/transit, security headers, and rate limiting. "
            "Use submit_work to output your verdict when ready."
        ),
    },
    {
        "name": "compliance",
        "system_prompt": (
            "You are a security expert reviewing from a compliance and data flow perspective. "
            "Focus on data flow tracing (input→output→stores), PCI-DSS, GDPR, CCPA, HIPAA compliance, "
            "data minimization, and optimization regression checks. "
            "Use submit_work to output your verdict when ready."
        ),
    },
]

_CONCEPTUAL_REVIEWERS = [
    {
        "name": "l1_architecture",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review from an architecture perspective: SOLID principles, separation of concerns, "
            "naming conventions, module boundaries. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
    {
        "name": "l2_security",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review from a security perspective: input validation, injection risks, "
            "path traversal, OWASP pre-scan. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
    {
        "name": "l3_performance",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review from a performance perspective: algorithmic complexity, N+1 queries, "
            "blocking I/O in async code. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
    {
        "name": "l4_api_interface",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review API/interface: contract compliance, backward compatibility, consistent error shapes. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
]

_FINAL_REVIEW_REVIEWERS = [
    {
        "name": "functional",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review requirements traceability: does the implementation match the IDEA card? "
            "Check for missing features, scope creep, edge cases. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
    {
        "name": "code_quality",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review code quality: test results, code style, error handling, test coverage, "
            "dead code, naming conventions, magic values. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
    {
        "name": "integration",
        "system_prompt": (
            "You are a code reviewer. Your session ends when you call submit_work. "
            "Review integration: import graph cycles, API signature breaks, migration validity, "
            "cross-feature interactions. "
            "Read what you need, reach a verdict, then call submit_work."
        ),
    },
]

_PLANNING_PERSONAS = [
    {
        "name": "Correctness & Testability",
        "system_prompt": (
            "Your primary concern is correctness and testability. "
            "Design for explicit, predictable error handling and well-defined failure modes. "
            "Create clean test seams — each component must be independently verifiable without "
            "needing to wire up the whole system. Prefer explicit over implicit. "
            "In your design_rationale, explain how the structure makes the system easy to test "
            "and how errors propagate clearly."
        ),
    },
    {
        "name": "Security & Defensive Robustness",
        "system_prompt": (
            "Your primary concern is security and defensive design. "
            "Minimise the attack surface. Validate all inputs at every trust boundary. "
            "Use safe defaults and fail closed on unexpected conditions. "
            "Avoid over-privileged components — each module should access only what it needs. "
            "Think through what can go wrong and design around it. "
            "In your design_rationale, explain the key trust boundaries, what is validated where, "
            "and how the design degrades safely under adversarial or unexpected input."
        ),
    },
    {
        "name": "Code Clarity & Codebase Consistency",
        "system_prompt": (
            "Your primary concern is code clarity and consistency with the existing codebase. "
            "Study the survey carefully: match the naming conventions, file layout, module "
            "structure, and idioms already present. A contributor familiar with the existing code "
            "should be able to predict every design choice you make before reading it. "
            "Prefer conventional structure over clever structure. Avoid introducing new patterns "
            "when existing ones already solve the problem. "
            "In your design_rationale, describe specifically how your design mirrors the patterns "
            "you observed in the codebase survey."
        ),
    },
    {
        "name": "Performance & Operational Efficiency",
        "system_prompt": (
            "Your primary concern is performance and resource efficiency. "
            "Minimise unnecessary computation, I/O, and database round-trips on the critical path. "
            "Consider caching strategies and async opportunities that reduce latency where it matters. "
            "Avoid premature abstraction that adds indirection without benefit. "
            "Design data flows so that the common case is fast; handle the slow path explicitly. "
            "In your design_rationale, identify the performance-critical paths and explain the "
            "specific choices that keep them efficient."
        ),
    },
    {
        "name": "Clean Architecture & Boundary Clarity",
        "system_prompt": (
            "Your primary concern is clean architecture and strict separation of concerns. "
            "Each module must have one clear, narrow responsibility. Define explicit interface "
            "contracts between components — what each provides, what it consumes, what invariants "
            "it upholds. Minimise coupling: a change in one area should not ripple unexpectedly. "
            "Design the system so its structure is self-evident from the file layout alone. "
            "In your design_rationale, explain exactly where you drew each boundary and why each "
            "component owns the responsibilities it does."
        ),
    },
]


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    return row["id"] if row else None


def _apply_key(conn, tid, stage_key, key, value, label):
    row = conn.execute(
        "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
        {"tid": tid, "key": stage_key},
    ).fetchone()
    if not row:
        print(f"[0116] WARNING: stage '{stage_key}' not found in '{label}' — skipping.")
        return False
    raw = row["config"]
    cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
    if key in cfg:
        print(f"[0116] Stage '{stage_key}' already has '{key}' — skipping.")
        return False
    cfg[key] = value
    conn.execute(
        "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
        {"config": _json.dumps(cfg), "sid": row["id"]},
    )
    print(f"[0116] Seeded '{key}' for stage '{stage_key}' in '{label}'.")
    return True


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0116] WARNING: 'Software Development' template not found — skipping.")
        return

    _apply_key(conn, tid, "security",          "reviewers",       _SECURITY_REVIEWERS,      "Software Development")
    _apply_key(conn, tid, "security",          "tally_strategy",  "veto",                   "Software Development")
    _apply_key(conn, tid, "conceptual_review", "reviewers",       _CONCEPTUAL_REVIEWERS,    "Software Development")
    _apply_key(conn, tid, "final_review",      "reviewers",       _FINAL_REVIEW_REVIEWERS,  "Software Development")
    _apply_key(conn, tid, "planning",          "personas",        _PLANNING_PERSONAS,       "Software Development")
    print("[0116] Done.")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    for stage_key, key in [
        ("security",          "reviewers"),
        ("security",          "tally_strategy"),
        ("conceptual_review", "reviewers"),
        ("final_review",      "reviewers"),
        ("planning",          "personas"),
    ]:
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not row:
            continue
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        if key in cfg:
            cfg.pop(key)
            conn.execute(
                "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
                {"config": _json.dumps(cfg), "sid": row["id"]},
            )
            print(f"[0116] Removed '{key}' from stage '{stage_key}'.")
