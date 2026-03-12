# **PRD - The Maestro Task List**

This document outlines the implementation requirements for **The Maestro**, an agentic orchestration system that enforces a strict separation between Design (Markdown) and Implementation (Source).

## **1. Local Infrastructure (The Core)**

- [x] **Config Engine:** Create config.py to manage __app_id, API endpoints, and project-specific constants.  
- [ ] **Maestro REPL:** Implement the primary control loop that navigates the task DAG.  
- [ ] **Checkpoint Manager:** Develop a Git-based persistence layer that executes git add and git commit upon every verified task completion.  
- [ ] **Context Window Handler:** Implement a sliding-window or RAG-lite system to manage 100k+ token contexts for long-term project stability.  
- [ ] **SafeShell Execution:** Build a sandboxed shell wrapper to intercept and block destructive system commands.

## **2. Dual-Artifact Synchronization**

- [ ] **AGENTS.md Specification:** Define the standardized schema for folder-level design blueprints.  
- [ ] **Design Validator:** Implement an LLM-gate that ensures AGENTS.md contains sufficient detail for implementation before triggering coding agents.  
- [ ] **CodeSync Monitor:** Create a utility to detect drift between the Markdown blueprints and the current source code state.  
- [ ] **State Rollback Logic:** Implement the REVERT_TO_DESIGN signal to halt code loops and re-engage planning when logic errors are detected.

## **3. Advanced Agent Specialization**

### **3.1 Planning Agent (The Architect)**

- [ ] **System Prompt:** Focus on high-level architecture, dependency mapping, and Markdown generation.  
- [ ] **Tooling:** Access to file-system (Write: .md, Read: All), DAG management tools.  
- [ ] **Task:** Generate AGENTS.md and update the Task DAG based on user requirements.

### **3.2 Coding Agent (The Implementer)**

- [ ] **System Prompt:** Strict adherence to AGENTS.md and ARCHITECTURE.md. Focus on writing clean, commented code following project principles.  
- [ ] **Tooling:** Access to file-system (Write: Source/Tests, Read: All), but forbidden from modifying .md files.  
- [ ] **Task:** Transform Markdown blueprints into functional source code.

### **3.3 Debugging Agent (The Analyst)**

- [ ] **System Prompt:** Specialized in diagnostic analysis, test execution, and logic verification.  
- [ ] **Tooling:** Read-only access to all files. Permission to execute SafeShell for tests and static analysis.  
- [ ] **Task:** Execute tests, collect logs, and generate a "Structured Diagnostic Report" for the Coding Agent in case of failure.

### **3.4 Research Agent (The Librarian)**

- [ ] **System Prompt:** Focused on documentation retrieval and external API knowledge via MCP.  
- [ ] **Tooling:** MCP web-search and documentation-fetch tools. Read-only file access.  
- [ ] **Task:** Provide context on libraries, best practices, and external dependencies.

## **4. Orchestration & DAG Logic**

- [ ] **Task Schema:** Define a JSON/YAML manifest for the Task DAG, including unique IDs and prerequisite arrays.  
- [ ] **DAG Resolver:** Implement logic to calculate the "Next Ready Task" (Tasks where all prerequisites are COMPLETED).  
- [ ] **Sprint Controller:** Group tasks into logical "Sprints" to manage project milestones.  
- [ ] **State Transitioning:** Manage the flow from PENDING -> ACTIVE -> VERIFYING -> ACCEPTED/REJECTED.

## **5. Management Interface**

- [ ] **Backend Service:** Deploy a FastAPI instance to track the Maestro's internal state.  
- [ ] **Design Dashboard:** A live-rendered view of ARCHITECTURE.md and AGENTS.md.  
- [ ] **Implementation Dashboard:** A real-time view of source code changes and linter/test status.  
- [ ] **Maestro Graph View:** A visual DAG/Kanban interface for ordering tasks and monitoring agent activity.  
- [ ] **Manual Override:** Interface for the user to manually trigger tool calls or pause the loop.

## **6. Verification & Checkpointing**

- [ ] **Verification Agent:** Create a specialized single-shot check to confirm the implementation matches the design blueprints.  
- [ ] **Failure Summarizer:** Implement logic to ingest Debugger logs and produce "Advice Context" for re-attempting failed tasks.  
- [ ] **Commit Gate:** Ensure no task is marked complete without both successful test exit codes and LLM verification.  
- [ ] **Remote Continuity:** Implement automated pushes to remote repositories at milestone checkpoints.

## **7. Static Analysis & Advanced Tools**

- [ ] **Aether Integration:** Hook into Aether static analysis for deep structural verification.  
- [ ] **FITM Optimization:** Implement Fill-In-The-Middle logic for llama.cpp to optimize large-file editing.  
- [ ] **Structured Query Enforcement:** Ensure all agent handoffs use JSON-schema to prevent natural language ambiguity.

## **Notes**

* **Maestro Loop:** The system persists until all DAG nodes reach the ACCEPTED state.  
* **Dual-Artifact Integrity:** The Source Code must always be a derivative of the Markdown design.  
* **Failure Protocol:** After 3 implementation failures, the system must trigger a REVERT_TO_DESIGN signal.