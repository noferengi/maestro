# TECHNICAL SPEC: The Maestro Evolution

## 1. Overview
This specification details the transformation of the "Dreamer" into **The Maestro** and the introduction of **Consultative Steering** to eliminate demotion loops.

---

## 2. Phase 1: The Great Rename & Memory Expansion (DONE)

### 2.1 Renaming Dreamer → Maestro (DONE)
- **File:** `app/agent/maestro.py` (Refactored from `dreamer.py`)
- **File:** `app/database/crud_maestro.py` (Refactored from `crud_dreamer.py`)
- **Class:** `MaestroAgent`, `MaestroPlan`, `MaestroRun`
- **Config:** `maestro.ini` updated with `[maestro]` section.
- **Scheduler:** `_dispatch_maestro` (L1215), `_start_maestro_thread` (L1352).

### 2.2 Global Decision Log & Schema (DONE)
- **File:** `app/database/models.py`
    - `Task.consultation_payload`: L148
    - `TaskSessionState`: L618
    - `MaestroRun`: L825
    - `ProjectDecision`: L852
- **Migration:** `app/migrations/versions/0063_maestro_evolution_schema.py`
- **Migration:** `app/migrations/versions/0064_add_task_session_states.py`

### 2.3 Context Injection (DONE)
- **File:** `app/agent/system_prompt.py` (L113)
- **File:** `app/agent/loop.py` (L385): Injects `BINDING ARCHITECTURAL DECISIONS` into context.

---

## 3. Phase 2: Consultative Steering (IN PROGRESS)

### 3.1 Task State Updates (DONE)
- **Scheduler:** `CONSULTATION GUARD` (L1035): Skips tasks awaiting hints.
- **Scheduler:** `CONSULTING` result handling (L3580): Logs question and emits Inbox notification.

### 3.2 The `consult_maestro` Tool (DONE)
- **File:** `app/agent/tools.py` (L3060): Returns terminal `CONSULT` signal.
- **File:** `app/agent/config.py`: Added `SIGNAL_CONSULT`.

### 3.3 Loop Suspension & Resumption (DONE)
- **File:** `app/agent/loop.py`
    - `_handle_terminal` (L523): Detects `CONSULT` signal and saves `TaskSessionState`.
    - `_build_messages` (L365): Resumes from `TaskSessionState` and injects `[MAESTRO STEERING HINT]`.

---

## 4. Phase 3: Flight Control (Frontend) (DONE)

### 4.1 Maestro Global Config (DONE)
- **File:** `app/web/index.html` (L155): Sidebar section.
- **File:** `app/web/index.html` (L990): Maestro Config and Decision Log modals.
- **File:** `app/web/kanban.js` (L7528): Implementation of `saveMaestroConfig`, `refreshDecisionsList`, etc.

### 4.2 Consultation Interface (DONE)
- **File:** `app/web/kanban.js` (L3115): Injected `consultationHtml` into `createTaskCard`.
- **File:** `app/web/kanban.js` (L7615): Implementation of `resumeFromConsultation`.
- **API:** `app/main.py` (L5525): `POST /api/tasks/{task_id}/resume` endpoint.
- **Data Layer:** `app/database/crud_tasks.py` (L1035): Updated `task_to_dict` to include `consultation_payload`.

---

## 5. Summary of Achievements
- **Eliminated Demotion Loops**: Agents can now pause and ask for help via `consult_maestro`, preserving worktree and turn count.
- **Centralized Knowledge**: The `ProjectDecision` log ensures architectural consistency across all agents.
- **Autonomous Stewardship**: The `MaestroAgent` (formerly Dreamer) now serves as a global project manager with high-level awareness.
- **Real-Time Steering**: The new "Cockpit" UI allows the human conductor to provide surgical 50-token hints to unblock agents mid-flight.
