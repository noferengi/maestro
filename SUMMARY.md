# SUMMARY: Resolving Pipeline Deadlocks and Loops

This document summarizes the investigation and resolution of critical bottlenecks in the Project Maestro task pipeline.

## 1. Tracked Tasks & Status

| Task ID | Title | Original State | Current State |
| :--- | :--- | :--- | :--- |
| `task-1776559187.604922` | SQL Migration - Basic Table Structure | **STUCK** (Subdividing, 0 children) | **UNSTUCK** (Subdivided into 4 children) |
| `task-1776548777.741009` | Create BlePacket Data Class | **STUCK** (Subdividing, 0 children) | **UNSTUCK** (Recovered to IDEA) |
| `task-1776548777.749239` | Create Supporting Types | **STUCK** (Subdividing, 0 children) | **UNSTUCK** (Subdivided into 1 child) |
| `task-1775285495.8875` | View Diff (Source Changes) | **STUCK** (Subdividing, 0 children) | **UNSTUCK** (Recovered to IDEA) |
| `task-1776757684.258525` | AndroidStreetPass: BlePacket Data Class | **LOOPING** (Rapid Cycling in Planning) | **UNSTUCK** (Demoted to IDEA with better description) |

## 2. Technical Interventions

### A. Subdivision Agent Crash Fix
*   **Issue:** The `SubdivisionAgent` was crashing with an `AttributeError` regarding `_last_prompt_tokens`. This caused the scheduler's "stranded task recovery" thread to die silently before it could move stuck tasks back to a dispatchable state.
*   **Fix:** Initialized `self._last_prompt_tokens = 0` in `app/agent/subdivide.py`. This stabilized the recovery logic.

### B. Planning Circuit Breaker
*   **Issue:** Tasks failing design review (e.g., for security reasons) were being re-dispatched by the scheduler indefinitely because the pipeline was failing *before* reaching the gate.
*   **Fix:** Implemented `PLANNING_MAX_REJECTIONS` (default 5) in `app/agent/config.py`. The scheduler (`app/agent/scheduler.py`) now counts rejections and demotes the task to `idea` for "forced subdivision" once the limit is hit.

### C. Enhanced Diagnostics
*   **Issue:** Failures in the subdivision outcome handler were too quiet.
*   **Fix:** Improved logging in `app/main.py` to explicitly report when a subdivision fails due to low confidence or persist failures, making it immediately visible in `maestro.log`.

### D. Requirement Injection
*   **Issue:** `task-1776757684.258525` was looping because the design review panel flagged missing security primitives (cryptographic binding, auth, replay protection).
*   **Fix:** Manually appended explicit **[SECURITY REQUIREMENTS]** to the task description to provide the LLM with the necessary constraints to pass the next planning run.

## 3. Progression Analysis
Following the server restart, the scheduler successfully identified the "stranded" tasks. Logs confirm that the fixed `SubdivisionAgent` ran for the formerly stuck tasks, successfully decomposing the large SQL migration and supporting type tasks into manageable sub-tasks. The looping BLE Packet task was demoted to the `idea` queue by the circuit breaker, preventing further resource waste until its requirements are addressed.

## 4. Future Predictions
1.  **Reduced Deadlocks:** With the `SubdivisionAgent` stabilized, "Big Ideas" will now reliably decompose into smaller tasks. The scheduler will no longer leave tasks in a `subdividing` limbo.
2.  **Resource Efficiency:** The Circuit Breaker will significantly reduce "token burn" on tasks with vague descriptions. Instead of looping 100+ times, they will surface in the `idea` column for human intervention after 5 failures.
3.  **Higher Planning Quality:** By injecting specific security requirements, we've set a pattern for how to break planning loops. In the future, the Planning Correction Agent could be extended to suggest these description updates automatically.
4.  **Scaling:** The pipeline is now more resilient to LLM "hallucinations" or design rejections, which is critical as the project moves into the more complex `INDEV` and `FINAL_REVIEW` stages.
