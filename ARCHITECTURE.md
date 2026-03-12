# **ARCHITECTURE: The Maestro Orchestrator**

## **1. Overview**

The Maestro Orchestrator is an agentic REPL (Read-Eval-Print-Loop) designed to manage large-scale software projects by strictly separating **Design (Markdown)** from **Implementation (Source Code)**. It utilizes a "Dual Artifact" system to ensure that code never drifts from its blueprints and that catastrophic failures are mitigated through mandatory git checkpointing and sandboxed execution.

## **2. Core Philosophy**

* **Markdown First:** No code is written until the design is solidified in AGENTS.md (local) and ARCHITECTURE.md (global).  
* **Single-Shot Execution:** Avoid multi-turn "chat" fatigue. Agents are invoked as single-shot tool-callers or context builders.  
* **The "Wiggum" Loop:** A simple, persistent Do-While loop that processes a Task DAG until all nodes are "Accepted."  
* **Immutable History:** Every successful task completion triggers a git commit.

## **3. The Dual-Artifact System**

The project state is represented by two parallel structures:

1. **The Blueprint (Design):** Stored in .md files. This is the "Ground Truth."  
   * `ARCHITECTURE.md`: Global system goals and high-level design.  
   * `PRD.md`: Itemized requirements and action items.  
   * `*/AGENTS.md`: Folder-specific design, logic, and instructions for subsequent agents.  
2. **The Product (Implementation):** The actual source code, tests, and assets.

## **4. Agent Specialization & Constraints**

Agents are limited by the system prompt and available MCP (Model Context Protocol) tools.

| Agent Type | Capabilities | Permissions |
| :---- | :---- | :---- |
| **Planning Agent** | Create/Edit Markdown, Manage DAG/Kanban | Read Source, Write Markdown |
| **Coding Agent** | Write/Edit Source Code | Read Markdown, Write Source |
| **Debugging Agent** | Execute Tests, Static Analysis | Read Source, Read Markdown, NO WRITE |
| **Research Agent** | Tool-based search, MCP documentation fetch | Read-Only |

## **5. Execution Logic (The Loop)**

### **Phase A: The Design Loop**

1. **Objective:** Solve the problem in Markdown.  
2. **Input:** User requirement or "Back to Drawing Board" signal.  
3. **Process:** Planning agents iterate on AGENTS.md.  
4. **Exit Condition:** A "Design Satisfaction" verification (via LLM or structured query) passes.

### **Phase B: The Implementation Loop**

1. **Objective:** Realize the design in code.  
2. **Input:** Validated AGENTS.md.  
3. **Process:** Coding agents generate source; Debugging agents run tests.  
4. **Verification:** - If Tests Pass: git commit and advance the DAG.  
   * If Tests Fail: Summarize failure -> Send to a new instance of the same agent type with "Advice Context."  
5. **Escape Hatch:** If an agent identifies a flaw in the design during implementation, it triggers a REVERT_TO_DESIGN signal, moving the task back to Phase A.

## **6. Project Management UI (Web Interface)**

The management dashboard consists of three primary views:

1. **Design View:** Live rendered Markdown of the current blueprints.  
2. **Implementation View:** Real-time status of source files, linter results, and test coverage.  
3. **Orchestration View (The DAG/Kanban):**  
   * **DAG Graph:** Visualization of tasks and prerequisites.  
   * **Logic:** A task ![][image1] is READY if ![][image2], ![][image3].  
   * **Sprints:** Grouped sets of tasks for focused execution.

## **7. Safety & Infrastructure**

* **Engine:** llama.cpp hosting Qwen-3-Coder-80B (OpenAI API compatible).  
* **Venv:** Isolated Python virtual environments for local execution.  
* **Sandboxing:** MCP tools (File System, Shell) must be wrapped in a permission-layer to prevent destructive commands (e.g., rm -rf /).  
* **Checkpointing:** Mandatory git push or local commit before any task transitions from ACTIVE to COMPLETED.

## **8. Data Formats**

* **Structured Querying:** Use JSON-mode/Schema constraints for agent handoffs.  
* **Task DAG:** Stored as a JSON or YAML manifest representing the state of the Kanban board.  
* **FITM (Fill-In-The-Middle):** To be explored for code completion/editing tasks to minimize token usage in large contexts.

## **9. Future Integration**

* **Aether/Static Analysis:** To be integrated as a "Verified Debugger" toolset once the basic REPL loop is stable.
