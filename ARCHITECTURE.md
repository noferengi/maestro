# **ARCHITECTURE: The Ralph-Wiggum Orchestrator**

## **1\. Overview**

The Ralph-Wiggum Orchestrator is an agentic REPL (Read-Eval-Print-Loop) designed to manage large-scale software projects by strictly separating **Design (Markdown)** from **Implementation (Source Code)**. It utilizes a "Dual Artifact" system to ensure that code never drifts from its blueprints and that catastrophic failures are mitigated through mandatory git checkpointing and sandboxed execution.

## **2\. Core Philosophy**

* **Markdown First:** No code is written until the design is solidified in AGENTS.md (local) and ARCHITECTURE.md (global).  
* **Single-Shot Execution:** Avoid multi-turn "chat" fatigue. Agents are invoked as single-shot tool-callers or context builders.  
* **The "Wiggum" Loop:** A simple, persistent Do-While loop that processes a Task DAG until all nodes are "Accepted."  
* **Immutable History:** Every successful task completion triggers a git commit.

## **3\. The Dual-Artifact System**

The project state is represented by two parallel structures:

1. **The Blueprint (Design):** Stored in .md files. This is the "Ground Truth."  
   * ARCHITECTURE.md: Global system goals and high-level design.  
   * PRD.md: Itemized requirements and action items.  
   * \*/AGENTS.md: Folder-specific design, logic, and instructions for subsequent agents.  
2. **The Product (Implementation):** The actual source code, tests, and assets.

## **4\. Agent Specialization & Constraints**

Agents are limited by the system prompt and available MCP (Model Context Protocol) tools.

| Agent Type | Capabilities | Permissions |
| :---- | :---- | :---- |
| **Planning Agent** | Create/Edit Markdown, Manage DAG/Kanban | Read Source, Write Markdown |
| **Coding Agent** | Write/Edit Source Code | Read Markdown, Write Source |
| **Debugging Agent** | Execute Tests, Static Analysis | Read Source, Read Markdown, NO WRITE |
| **Research Agent** | Tool-based search, MCP documentation fetch | Read-Only |

## **5\. Execution Logic (The Loop)**

### **Phase A: The Design Loop**

1. **Objective:** Solve the problem in Markdown.  
2. **Input:** User requirement or "Back to Drawing Board" signal.  
3. **Process:** Planning agents iterate on AGENTS.md.  
4. **Exit Condition:** A "Design Satisfaction" verification (via LLM or structured query) passes.

### **Phase B: The Implementation Loop**

1. **Objective:** Realize the design in code.  
2. **Input:** Validated AGENTS.md.  
3. **Process:** Coding agents generate source; Debugging agents run tests.  
4. **Verification:** \- If Tests Pass: git commit and advance the DAG.  
   * If Tests Fail: Summarize failure \-\> Send to a new instance of the same agent type with "Advice Context."  
5. **Escape Hatch:** If an agent identifies a flaw in the design during implementation, it triggers a REVERT\_TO\_DESIGN signal, moving the task back to Phase A.

## **6\. Project Management UI (Web Interface)**

The management dashboard consists of three primary views:

1. **Design View:** Live rendered Markdown of the current blueprints.  
2. **Implementation View:** Real-time status of source files, linter results, and test coverage.  
3. **Orchestration View (The DAG/Kanban):**  
   * **DAG Graph:** Visualization of tasks and prerequisites.  
   * **Logic:** A task ![][image1] is READY if ![][image2], ![][image3].  
   * **Sprints:** Grouped sets of tasks for focused execution.

## **7\. Safety & Infrastructure**

* **Engine:** llama.cpp hosting Qwen-3-Coder-80B (OpenAI API compatible).  
* **Venv:** Isolated Python virtual environments for local execution.  
* **Sandboxing:** MCP tools (File System, Shell) must be wrapped in a permission-layer to prevent destructive commands (e.g., rm \-rf /).  
* **Checkpointing:** Mandatory git push or local commit before any task transitions from ACTIVE to COMPLETED.

## **8\. Data Formats**

* **Structured Querying:** Use JSON-mode/Schema constraints for agent handoffs.  
* **Task DAG:** Stored as a JSON or YAML manifest representing the state of the Kanban board.  
* **FITM (Fill-In-The-Middle):** To be explored for code completion/editing tasks to minimize token usage in large contexts.

## **9\. Future Integration**

* **Aether/Static Analysis:** To be integrated as a "Verified Debugger" toolset once the basic REPL loop is stable.

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA4AAAAZCAYAAAABmx/yAAAA50lEQVR4XmNgGCFAXl5eEoh75eTkZuHDQDXTZGVlI2RkZDjBGoGC6UDBXyBJkASQDgHiBiA+DeMD5ZOB9BUgvismJiYOsk0TyFkMNwUCWIBia4DYBUmMAWiIKVCsH8wBaowGYWQFUlJSIkAFV6WlpWWQxYFivkDNRWCOuLg4N5BiRVNgA8S/FRQUOJDFRUVFedDFUADIVKAr/qOLEwIw//1Dl8ALQCEGCjkgfoIuhxfA/Ad06lZ0ObwA5j+g5nJ0OXwA7j/0OMQLgIlAGmjbA5D/0OMQA0BTzluQ89AxUHwGuvpRMLwAADS5QKMaun9/AAAAAElFTkSuQmCC>

[image2]: <data:image/png;base64,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>

[image3]: <data:image/png;base64,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>