# PLAN OF ACTION: Deterministic Intake Pipeline & Voting Gate System

## Vision

Replace the direct "throw it at the Wiggum Loop" approach with a **multi-stage, LLM-validated, budget-tracked pipeline** that gates every column transition behind a structured voting system. The first implementation focuses on the **IDEA to PLANNING** transition — the most complex gate — with the voting/tally mechanism designed to be reusable for all subsequent column transitions.

---

## Column Order (Updated)

```
ARCHITECTURE -> IDEA -> PLANNING -> DEVELOPMENT -> REVIEW -> COMPLETED
```

- **IDEA** is the new column between ARCHITECTURE and PLANNING.
- Humans create tasks in IDEA. Humans request advancement to PLANNING.
- The pipeline runs asynchronously in the backend when advancement is requested.
- The task stays in IDEA (with status indicators) until the pipeline resolves.

---

## The IDEA -> PLANNING Pipeline

When a human requests a task move from IDEA to PLANNING, the following stages execute:

### Execution Dependency Graph

```
Stage 1 (Scope Analysis)
    |
    +---> Stage 2a (Static Analysis)  -----> Stage 2b (Feasibility)
    |                                              |
    +---> Stage 3 (Conflict Detection) -----------+
                                                   |
                                              Tally Phase
```

- **Stage 1** runs first (determines scope, identifies affected files/modules).
- **Stage 2a** and **Stage 3** run in parallel after Stage 1 completes.
- **Stage 2b** runs after Stage 2a completes (needs the static analysis output).
- **Tally Phase** runs after all 4 stages have voted.

### Stage 1: Scope Analysis

**Type:** LLM call (structured query/response)
**Input:** Task title + description from IDEA card
**Output:** Structured JSON:

```json
{
  "scope": "single" | "multi" | "tree",
  "estimated_files": ["app/main.py", "app/database.py"],
  "estimated_complexity": "low" | "medium" | "high",
  "subtasks": [
    {"title": "...", "description": "...", "dependencies": ["subtask-id-1"]}
  ],
  "dependency_edges": [["a", "b"], ["b", "c"]],
  "rationale": "...",
  "vote": {
    "verdict": "LIKELY",
    "confidence": 94,
    "justification": "Clear single-module change with no ambiguity."
  }
}
```

**Purpose:** Determine if this is one task or many. If `scope != "single"`, the system can auto-decompose into a subtask DAG (using existing `prerequisites` field and `DAGResolver`). Account for 9B model context window limitations — tasks must be scoped to what the agent can reason about in a single run.

### Stage 2a: Static Analysis (Deterministic — No LLM)

**Type:** Tree-sitter parse + programmatic analysis
**Input:** File list from Stage 1's `estimated_files` + broader project structure
**Output:** Structured JSON:

```json
{
  "file_map": {
    "app/main.py": {
      "classes": [...],
      "functions": ["read_tasks", "update_existing_task", ...],
      "imports": ["fastapi", "database", ...],
      "references_to": ["app/database.py"],
      "referenced_by": ["app/web/kanban.js"]
    }
  },
  "call_graph": { "update_task": ["get_task", "reorder_tasks"] },
  "data_structures": { "Task": { "fields": [...], "relationships": [...] } },
  "vote": {
    "verdict": "LIKELY",
    "confidence": 98,
    "justification": "All referenced files exist, no circular imports detected."
  }
}
```

**Purpose:** Ground-truth structural map of the codebase. Not hallucinated — parsed. This output feeds Stage 2b so the LLM has real structural data to reason about, not guesses.

### Stage 2b: Feasibility & Ambiguity Analysis

**Type:** LLM call (structured query/response), informed by Stage 2a output
**Input:** Task description + Stage 1 scope + Stage 2a static analysis
**Output:** Structured JSON:

```json
{
  "feasible": true,
  "ambiguities": [
    {"question": "Should the IDEA column support drag-and-drop?", "why_it_matters": "..."}
  ],
  "dependencies_needed": ["tree-sitter-python"],
  "risk_notes": ["Modifying column order affects 3 files"],
  "affected_modules": ["app/main.py", "app/database.py", "app/web/kanban.js"],
  "vote": {
    "verdict": "POSSIBLE",
    "confidence": 82,
    "justification": "Feasible but 2 ambiguities need human clarification."
  }
}
```

**Purpose:** With the static analysis as ground truth, assess whether the task can actually be implemented, flag ambiguities that need human answers, and identify external dependencies.

### Stage 3: Conflict Detection

**Type:** LLM call (structured query/response)
**Input:** Task description + Stage 1 scope + all tasks currently in PLANNING/DEVELOPMENT/REVIEW
**Output:** Structured JSON:

```json
{
  "conflicts": [
    {
      "with_task_id": "dev-2",
      "type": "file_overlap",
      "description": "Both tasks modify app/main.py routing",
      "severity": "low"
    }
  ],
  "semantic_conflicts": [],
  "priority_conflicts": [],
  "vote": {
    "verdict": "LIKELY",
    "confidence": 95,
    "justification": "No blocking conflicts. Minor file overlap with dev-2 is manageable."
  }
}
```

**Purpose:** Cross-task awareness. Detect merge conflicts, semantic contradictions, and architectural inconsistencies before the task enters the pipeline.

---

## The Voting System

### Verdicts & Confidence Ranges

| Verdict              | Range       | Meaning                                          | Gate Effect                              |
|----------------------|-------------|--------------------------------------------------|------------------------------------------|
| `REJECTED`           | [0%, 50%]   | Hard no — fundamental blocker                    | Immediate pipeline failure               |
| `NOT_SUITABLE`       | (50%, 60%]  | Soft no — task poorly scoped or inappropriate    | Needs majority soft-no to fail           |
| `NEEDS_RESEARCH`     | (60%, 75%]  | Insufficient information to assess               | Triggers research sub-agent (up to 3 total calls); re-votes after |
| `POSSIBLE`           | (75%, 92%)  | Can probably be done, some uncertainty           | Passing grade                            |
| `LIKELY`             | [92%, 100%] | High confidence — A- or better                   | Strong pass                              |

### Voters

Each of the 4 stages (1, 2a, 2b, 3) produces exactly one vote with:
- `verdict`: One of the 5 enum values above
- `confidence`: Integer 0-100 (must fall within the verdict's range)
- `justification`: Structured text explaining the reasoning

### Tally Rules

1. **Any REJECTED vote** -> Pipeline fails immediately. Full error presented to human.
2. **Majority NOT_SUITABLE** (3+ of 4 voters, or 2+ if only 3 voted after NEEDS_RESEARCH resolution) -> Pipeline fails.
3. **NEEDS_RESEARCH** -> Triggers a research sub-agent with all context. The research agent gets **3 total calls** (2 "extra lives"). Each call can recurse to spawn a new agent with additional context. After research completes, the stage re-votes. If still NEEDS_RESEARCH after exhausting lives, it's treated as NOT_SUITABLE.
4. **Tie (2-2 split)** -> A special tie-breaker research agent is spawned with all 4 voter responses. This agent investigates the disparity and casts a deciding vote. This tie-breaker pattern is **generalizable across all column transitions**.
5. **Pass** -> All votes are POSSIBLE or LIKELY (after NEEDS_RESEARCH resolution). Task advances to PLANNING.

### Budget Tracking

Every LLM call logs:
```json
{
  "task_id": "...",
  "stage": "scope_analysis | feasibility | conflict_detection | research | tiebreaker",
  "model": "omnicoder-9b",
  "prompt_tokens": 1234,
  "completion_tokens": 567,
  "budget_id": 1,
  "timestamp": "2026-03-14T..."
}
```

All votes, confidence scores, justifications, and token costs are persisted permanently for future analysis (confidence calibration, agent performance metrics, per-language/condition breakdowns).

### Research Agent Details (implemented in `app/agent/research.py`)

- **ResearchAgent class** — lightweight agentic loop with restricted read-only tools
- **Lives system**: up to 3 sequential agent runs (configurable via `RESEARCH_AGENT_MAX_LIVES`), each with 20 turns max. Life N's findings seed Life N+1's context.
- **Restricted tools**: `read_file`, `search_files`, `find_files`, `list_directory`, `run_shell` — no write access
- **Two system prompts**: general research prompt and tie-breaker prompt (with instructions to cite evidence and pick a side)
- **Terminal output**: structured JSON vote (verdict + confidence + justification + findings)
- **Fallback**: if all lives exhausted without a confident verdict, returns NOT_SUITABLE (confidence 55)
- **Integration**: intake pipeline's `run()` method calls `_handle_needs_research()` after initial tally (replaces NEEDS_RESEARCH votes with research results), then `_handle_tie()` if still tied (adds a 5th tie-breaker vote)
- **Convenience functions**: `run_research(question, context)` and `run_tiebreaker(task_description, votes)`

---

## Other Column Transitions (TBD)

The voting gate mechanism is universal. Each column transition will have its own specific stages defined later:

| Transition              | Stages                      | Status |
|-------------------------|-----------------------------|--------|
| IDEA -> PLANNING        | Scope, Static, Feasibility, Conflict | **This document** |
| PLANNING -> DEVELOPMENT | TBD                         | Future |
| DEVELOPMENT -> REVIEW   | TBD                         | Future |
| REVIEW -> COMPLETED     | TBD                         | Future |

The tie-breaker research agent and voting tally system are designed to be reused across all transitions.

---

## Human Interaction Model

- **Human creates IDEAS.** This is their primary interaction surface.
- **Human requests IDEA -> PLANNING.** This triggers the full pipeline.
- **Human intervenes on:**
  - REJECTED votes (hard no — must redesign the idea)
  - Failed majority votes (soft no — may need to refine scope/description)
  - Research exhaustion (agent couldn't find enough info — human provides clarification)
- **The gate is mandatory.** Human cannot override a REJECTED vote. The pipeline must pass for the task to advance.
- **Visual feedback:** Card gets a red outline on rejection. Card shows a status indicator during pipeline execution (spinner/progress). On success, card transitions to PLANNING column maintaining its relative position.

---

## Implementation Plan

### New Files

| File | Purpose | Status |
|------|---------|--------|
| `app/agent/verdicts.py` | Verdict enum, confidence ranges, vote dataclass, tally logic | DONE |
| `app/agent/static_analysis.py` | Tree-sitter based code structure analysis | DONE |
| `app/agent/intake.py` | Intake pipeline orchestrator (stages 1-3 + tally + research/tiebreaker dispatch) | DONE |
| `app/agent/research.py` | Research agent with lives system (general research + tie-breaker) | DONE |
| `app/migrations/versions/0006_add_idea_column_and_votes.py` | Schema: add 'idea' type, create transition_votes table | DONE |

### Modified Files

| File | Changes |
|------|---------|
| `app/main.py` | Add IDEA to column order, add `/api/tasks/{id}/advance` endpoint, add transition status endpoints |
| `app/database.py` | Add TransitionVote model, vote CRUD functions |
| `app/web/index.html` | Add IDEA column to board layout |
| `app/web/kanban.js` | Add IDEA column rendering, advance button, rejection state UI, pipeline status polling |
| `app/web/style.css` | IDEA column styles, rejection state (red outline), pipeline spinner |
| `app/agent/config.py` | Add intake pipeline config (research agent lives, tie-breaker settings) |

### Data Persistence

All pipeline results are stored permanently in `transition_votes` table:
```sql
CREATE TABLE transition_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    transition TEXT NOT NULL,          -- e.g. 'idea_to_planning'
    stage TEXT NOT NULL,               -- e.g. 'scope_analysis', 'static_analysis', 'feasibility', 'conflict_detection'
    verdict TEXT NOT NULL,             -- REJECTED | NOT_SUITABLE | NEEDS_RESEARCH | POSSIBLE | LIKELY
    confidence INTEGER NOT NULL,       -- 0-100
    justification TEXT,
    raw_response JSON,                 -- full LLM response for audit
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    model TEXT,
    budget_id INTEGER REFERENCES budgets(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE transition_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    transition TEXT NOT NULL,
    outcome TEXT NOT NULL,              -- 'passed' | 'rejected' | 'needs_research' | 'tie'
    vote_summary JSON,                 -- aggregated tally
    total_prompt_tokens INTEGER,
    total_completion_tokens INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Config Additions (app/agent/config.py)

```python
# Intake pipeline settings
RESEARCH_AGENT_MAX_LIVES: int = 3        # Total calls for a research agent
RESEARCH_AGENT_TOOLS: list[str] = [      # Restricted tool set for research
    "read_file", "search_files", "find_files", "list_directory", "run_shell"
]
TIEBREAKER_ENABLED: bool = True          # Enable tie-breaker research agent
INTAKE_LLM_TEMPERATURE: float = 0.1     # Lower temp for structured responses

# Verdict confidence ranges (inclusive bounds as documented)
VERDICT_RANGES = {
    "REJECTED":       (0, 50),
    "NOT_SUITABLE":   (51, 60),
    "NEEDS_RESEARCH": (61, 75),
    "POSSIBLE":       (76, 91),
    "LIKELY":         (92, 100),
}
```
