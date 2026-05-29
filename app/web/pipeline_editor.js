// pipeline_editor.js v2
// Phase 4 — Litegraph Pipeline Editor

// ============================================================
// CONSTANTS & STATE
// ============================================================

const PE_CONDITIONS = ["pass", "fail", "reject", "always", "skip"];
const PE_CONDITION_COLORS = {
    pass: "#22c55e", fail: "#ef4444", reject: "#f59e0b",
    always: "#818cf8", skip: "#94a3b8",
};
const PE_BACK_EDGE_COLOR = "#f59e0b";
const PE_FORWARD_LINK_COLOR = "#60a5fa";

let _graph = null;        // LGraph instance
let _canvas = null;       // LGraphCanvas instance
let _templateId = null;   // integer or null
let _templateData = null; // full API response
let _agentTypes = [];     // [{type_key, display_name, …}]
let _trackableTools = { categories: {}, all: [] }; // tool-success-store tracked tools by category

// Maps litegraph node.id → DB stage.position (used for back-edge detection)
let _stagePosMap = {};

// Maps stage_key → LGraph node (populated by buildGraphFromTemplate, used by overlay renderer)
let _nodeByKey = {};

// Tracks DB transition IDs for the current graph (transition_id → {from_key, to_key, condition})
let _dbTransitions = {};

// Kanban column band definitions: [{key, label, color, x_min}] sorted by x_min
let _kanbanColumns = [];

// Panel state
let _panelNode = null;    // node whose properties are currently open
let _panelSnapshot = {};  // copy of node.properties when panel was opened (for Revert)

// Simulation state
let _simActive = false;
let _simPath = [];        // ordered node IDs for the ghost walk
let _simStep = 0;
let _simHighlightedId = null;

// ============================================================
// NODE DEFINITIONS
// ============================================================

function _makeNodeColor(hex) {
    // Lighten hex color slightly for the node header
    return hex;
}

class StageNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Stage";
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.properties = {
            stage_id: null,
            stage_key: "",
            label: "",
            agent_type: "planning_agent",
            color: "#1e40af",
            intent: "",
            system_prompt: "",
            gate_type: "llm_judge",
            max_retries: 3,
            required_input_keys: "",
            output_keys: "",
        };
        this.color = "#1e40af";
        this.bgcolor = "#0f2d60";
        this.size = [220, 80];
    }

    onDrawBackground(ctx) {
        if (!this.properties.label) return;
        ctx.save();
        if ((this.properties.executor_type || "infrastructure") === "custom_python") {
            const badge = "⚙ custom";
            ctx.font = "bold 9px monospace";
            const bw = ctx.measureText(badge).width + 8;
            ctx.fillStyle = "rgba(180, 83, 9, 0.85)";
            ctx.fillRect(this.size[0] - bw - 4, 6, bw, 13);
            ctx.fillStyle = "#fff";
            ctx.fillText(badge, this.size[0] - bw, 16);
        }
        ctx.fillStyle = "rgba(255,255,255,0.55)";
        ctx.font = "11px monospace";
        ctx.fillText(this.properties.stage_key.substring(0, 18), 8, this.size[1] - 8);
        ctx.restore();
    }

    onDblClick(e, pos, graphCanvas) {
        openPanel(this);
    }

    // Ensure the output port set matches conditions (called after graph loads)
    syncOutputsToConditions(conditions) {
        // Keep existing ports that are still in the conditions list
        const existing = (this.outputs || []).map(o => o.name);
        conditions.forEach(cond => {
            if (!existing.includes(cond)) {
                this.addOutput(cond, "task");
            }
        });
    }

    getActiveConditions() {
        return (this.outputs || []).map(o => o.name);
    }
}
StageNode.title = "Stage";
StageNode.desc = "A pipeline stage with LLM agent execution and a gate";

class FactoryNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Factory";
        this.addInput("trigger", "task");
        this.addOutput("pass", "task");
        this.properties = {
            stage_id:                   null,
            stage_key:                  "",
            label:                      "",
            color:                      "#065f46",
            factory_source_type:        "folder",
            factory_source_config_json: "{}",
            factory_segmentation_mode:  "mechanical",
            factory_entry_stage:        "idea",
            factory_title_template:     "{filename}",
            factory_desc_template:      "",
            factory_triggers:           ["manual"],
            factory_cron_schedule:      "",
            intent:                     "",
        };
        this.color   = "#065f46";
        this.bgcolor = "#022c22";
        this.size = [220, 90];
    }

    onDrawBackground(ctx) {
        // Draw factory icon (gear/cog) and stage key label
        const w = this.size[0];
        const h = this.size[1];
        ctx.save();

        // gear icon
        ctx.fillStyle = "rgba(255,255,255,0.25)";
        ctx.font = "22px serif";
        ctx.fillText("⚙", 8, 30);

        // stage_key label
        ctx.fillStyle = "rgba(255,255,255,0.5)";
        ctx.font = "11px monospace";
        const sk = (this.properties.stage_key || "factory").substring(0, 20);
        ctx.fillText(sk, 36, 24);

        // ▶ Run Now button area
        const btnX = w - 68;
        const btnY = h - 26;
        const btnW = 60;
        const btnH = 20;
        ctx.fillStyle = this._runHover ? "#10b981" : "#065f46";
        ctx.strokeStyle = "#34d399";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect(btnX, btnY, btnW, btnH, 3);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.font = "bold 10px sans-serif";
        ctx.fillText("▶ Run", btnX + 10, btnY + 13);

        this._btnBounds = { x: btnX, y: btnY, w: btnW, h: btnH };
        ctx.restore();
    }

    onMouseMove(e, pos) {
        if (!this._btnBounds) return;
        const b = this._btnBounds;
        const inside = pos[0] >= b.x && pos[0] <= b.x + b.w &&
                       pos[1] >= b.y && pos[1] <= b.y + b.h;
        if (inside !== this._runHover) {
            this._runHover = inside;
            this.setDirtyCanvas(true);
        }
    }

    onMouseDown(e, pos) {
        if (!this._btnBounds) return false;
        const b = this._btnBounds;
        const inside = pos[0] >= b.x && pos[0] <= b.x + b.w &&
                       pos[1] >= b.y && pos[1] <= b.y + b.h;
        if (inside) {
            runFactoryNow(this);
            return true;  // consume event
        }
        return false;
    }

    onDblClick() { openPanel(this); }
}
FactoryNode.title = "Factory";
FactoryNode.desc = "Produces task cards from an external data source";

class ConditionalNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Conditional";
        this.addInput("in", "task");
        this.addInput("key", "data");
        this.addOutput("branch_a", "task");
        this.addOutput("branch_b", "task");
        this.properties = { branch_key: "" };
        this.color = "#7c3aed"; this.bgcolor = "#3b1a6b";
        this.size = [200, 80];
    }
    onDblClick() { openPanel(this); }
}
ConditionalNode.title = "Conditional";
ConditionalNode.desc = "Branches based on a content blob key value";

class JudgmentGateNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Best of 3";
        this.addInput("attempt_1", "task");
        this.addInput("attempt_2", "task");
        this.addInput("attempt_3", "task");
        this.addOutput("best", "task");
        this.properties = { fan_n: 3 };
        this.color = "#b45309"; this.bgcolor = "#4c2100";
        this.size = [180, 100];
    }
    onDblClick() { openPanel(this); }
}
JudgmentGateNode.title = "Judgment Gate";
JudgmentGateNode.desc = "Fan-in: receives N attempts, selects the best";

class FanOutNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "×3 Attempts";
        this.addInput("in", "task");
        this.addOutput("out", "task");
        this.properties = { attempts_n: 3 };
        this.color = "#0e7490"; this.bgcolor = "#082f49";
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
FanOutNode.title = "Fan-out";
FanOutNode.desc = "Produces N parallel attempt cards from one input card";

class HumanGateNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Human Gate";
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.addOutput("reject", "task");
        this.properties = { autopilot_hours: 0 };
        this.color = "#be185d"; this.bgcolor = "#500724";
        this.size = [180, 80];
    }
    onDblClick() { openPanel(this); }
}
HumanGateNode.title = "Human Gate";
HumanGateNode.desc = "Blocks until human approval";

class StaticAnalysisNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Static Analysis";
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.properties = {
            stage_id:     null,
            stage_key:    "",
            label:        "",
            agent_type:   "static_analysis_widget",
            output_key:   "static_analysis",
            file_pattern: "**/*.py",
            max_files:    50,
        };
        this.color   = "#b45309";  // amber — signals "tool, not LLM"
        this.bgcolor = "#451a03";
        this.size = [220, 80];
    }

    onDrawBackground(ctx) {
        ctx.save();
        ctx.fillStyle = "rgba(255,255,255,0.25)";
        ctx.font = "20px serif";
        ctx.fillText("⚙", 8, 28);  // gear icon ⚙
        ctx.fillStyle = "rgba(255,255,255,0.5)";
        ctx.font = "11px monospace";
        ctx.fillText((this.properties.stage_key || "static_analysis").substring(0, 20), 34, 22);
        ctx.restore();
    }

    onDblClick() { openPanel(this); }
}
StaticAnalysisNode.title = "Static Analysis";
StaticAnalysisNode.desc = "Deterministic tree-sitter analysis — no LLM, injects JSON into task.content";

class DangerousEditNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Dangerous Edit Agent";
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.addOutput("fail", "task");
        this.properties = {
            stage_id:            null,
            stage_key:           "",
            label:               "",
            agent_type:          "dangerous_edit_llm_agent",
            system_prompt:       "",
            max_turns:           200,
            agent_tools:         "",   // comma-sep; empty = INDEV_AGENT_TOOLS default
            required_input_keys: "",   // comma-sep
        };
        this.color   = "#7f1d1d";  // deep red — signals "writes to project working tree"
        this.bgcolor = "#3b0a0a";
        this.size = [220, 90];
    }

    onDrawBackground(ctx) {
        ctx.save();
        ctx.fillStyle = "rgba(255,255,255,0.3)";
        ctx.font = "18px serif";
        ctx.fillText("⚠", 8, 26);
        ctx.fillStyle = "rgba(255,255,255,0.5)";
        ctx.font = "11px monospace";
        ctx.fillText((this.properties.stage_key || "dangerous_edit").substring(0, 20), 32, 20);
        ctx.restore();
    }

    onDblClick() { openPanel(this); }
}
DangerousEditNode.title = "Dangerous Edit Agent";
DangerousEditNode.desc = "Wraps MaestroLoop — writes to project working tree inside a git worktree. ⚠ Configurable system prompt, tools, and max_turns.";


// ============================================================
// PARALLEL AGENTS NODE
// ============================================================

function _peEscHtml(s) {
    return String(s ?? "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

class ParallelAgentsNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.title = "Parallel Agents";
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.properties = {
            stage_id:   null,
            stage_key:  "",
            label:      "",
            agent_type: "parallel_agents",
            n:          3,
            output_key: "parallel_agents_output",
            max_turns:  30,
            agents:     [],
        };
        this.color   = "#0c4a6e";
        this.bgcolor = "#042940";
        this.size    = [240, 80 + 3 * 36];
    }

    _syncSize() {
        const n = Math.max(1, Math.min(10, parseInt(this.properties.n) || 3));
        this.size[1] = 80 + n * 36;
        return n;
    }

    onDrawBackground(ctx) {
        const n = this._syncSize();
        const agents = this.properties.agents || [];
        ctx.save();
        ctx.fillStyle = "rgba(255,255,255,0.35)";
        ctx.font = "16px monospace";
        ctx.fillText("⑂", 8, 24);
        ctx.fillStyle = "rgba(255,255,255,0.5)";
        ctx.font = "11px monospace";
        ctx.fillText((this.properties.stage_key || "parallel_agents").substring(0, 22), 30, 22);
        for (let i = 0; i < n; i++) {
            const name = (agents[i] || {}).name || `agent_${i}`;
            const y = 42 + i * 36;
            ctx.fillStyle = "rgba(255,255,255,0.1)";
            ctx.beginPath();
            if (ctx.roundRect) {
                ctx.roundRect(6, y, this.size[0] - 12, 28, 3);
            } else {
                ctx.rect(6, y, this.size[0] - 12, 28);
            }
            ctx.fill();
            ctx.fillStyle = "rgba(255,255,255,0.7)";
            ctx.font = "11px sans-serif";
            ctx.fillText(`● ${name}`, 12, y + 17);
        }
        ctx.restore();
    }

    onDblClick() { openPanel(this); }
}
ParallelAgentsNode.title = "Parallel Agents";
ParallelAgentsNode.desc = "FOR LOOP — runs N agents in parallel as real scheduler tasks, merges outputs";


// ---------------------------------------------------------------------------
// Intake stage nodes (Phase 7 — each wraps the corresponding executor)
// ---------------------------------------------------------------------------

class IntakeScopeNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = { stage_key: "intake_scope", label: "Intake: Scope", agent_type: "intake_scope", color: "#2d6a4f" };
        this.addInput("in", "transition");
        this.addOutput("pass", "transition");
        this.addOutput("fail", "transition");
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
IntakeScopeNode.title = "Intake: Scope";
IntakeScopeNode.desc = "LLM scope analysis — votes on size, complexity, decomposition";

class IntakeStaticNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = { stage_key: "intake_static", label: "Intake: Static", agent_type: "intake_static", color: "#1b4332" };
        this.addInput("in", "transition");
        this.addOutput("pass", "transition");
        this.addOutput("fail", "transition");
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
IntakeStaticNode.title = "Intake: Static";
IntakeStaticNode.desc = "Deterministic tree-sitter code structure analysis";

class IntakeConflictNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = { stage_key: "intake_conflict", label: "Intake: Conflict", agent_type: "intake_conflict", color: "#40916c" };
        this.addInput("in", "transition");
        this.addOutput("pass", "transition");
        this.addOutput("fail", "transition");
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
IntakeConflictNode.title = "Intake: Conflict";
IntakeConflictNode.desc = "LLM conflict detection against existing tasks";

class IntakeFeasibilityNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = { stage_key: "intake_feasibility", label: "Intake: Feasibility", agent_type: "intake_feasibility", color: "#52b788" };
        this.addInput("in", "transition");
        this.addOutput("pass", "transition");
        this.addOutput("fail", "transition");
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
IntakeFeasibilityNode.title = "Intake: Feasibility";
IntakeFeasibilityNode.desc = "LLM feasibility analysis informed by static output";

class IntakeGateNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = { stage_key: "intake_gate", label: "Intake: Gate", agent_type: "intake_gate", color: "#74c69d" };
        this.addInput("in", "transition");
        this.addOutput("pass", "transition");
        this.addOutput("fail", "transition");
        this.size = [180, 60];
    }
    onDblClick() { openPanel(this); }
}
IntakeGateNode.title = "Intake: Gate";
IntakeGateNode.desc = "Tallies all intake votes — passes, rejects, or triggers subdivide/research";

class MultiplierNode extends LiteGraph.LGraphNode {
    constructor() {
        super();
        this.properties = {
            stage_key: "multiplier_node",
            label: "Multiplier",
            agent_type: "multiplier_node",
            color: "#5b21b6",
            n: 3,
            agent_system_prompt: "",
            agent_tools: "",
            agent_max_turns: 15,
            agents: "",
            collapser_mode: "vote_tally",
            tally_strategy: "majority",
            on_tie: "reject",
            judge_system_prompt: "Compare the proposals and select the best one.",
            judge_max_turns: 10,
            required_input_keys: "",
            output_key: "fan_out_result",
        };
        this.addInput("in", "task");
        this.addOutput("pass", "task");
        this.addOutput("fail", "task");
        this.size = [200, 70];
    }
    onDrawBackground(ctx) {
        ctx.save();
        ctx.fillStyle = "rgba(255,255,255,0.35)";
        ctx.font = "bold 12px sans-serif";
        ctx.fillText("1→N", 8, 22);
        ctx.fillStyle = "rgba(255,255,255,0.5)";
        ctx.font = "10px monospace";
        const mode = this.properties.collapser_mode === "judge_select" ? "[judge]" : "[vote]";
        ctx.fillText(mode, 8, this.size[1] - 8);
        ctx.restore();
    }
    onDblClick() { openPanel(this); }
}
MultiplierNode.title = "Multiplier (Fan-Out)";
MultiplierNode.desc = "Spawns N crash-survivable child tasks; collapser aggregates via vote tally or LLM judge";


function registerNodeTypes() {
    LiteGraph.registerNodeType("maestro/stage", StageNode);
    LiteGraph.registerNodeType("maestro/factory", FactoryNode);
    LiteGraph.registerNodeType("maestro/conditional", ConditionalNode);
    LiteGraph.registerNodeType("maestro/judgment_gate", JudgmentGateNode);
    LiteGraph.registerNodeType("maestro/fan_out", FanOutNode);
    LiteGraph.registerNodeType("maestro/human_gate", HumanGateNode);
    LiteGraph.registerNodeType("maestro/static_analysis", StaticAnalysisNode);
    LiteGraph.registerNodeType("maestro/dangerous_edit",   DangerousEditNode);
    LiteGraph.registerNodeType("maestro/parallel_agents",  ParallelAgentsNode);
    LiteGraph.registerNodeType("maestro/intake_scope",       IntakeScopeNode);
    LiteGraph.registerNodeType("maestro/intake_static",      IntakeStaticNode);
    LiteGraph.registerNodeType("maestro/intake_conflict",    IntakeConflictNode);
    LiteGraph.registerNodeType("maestro/intake_feasibility", IntakeFeasibilityNode);
    LiteGraph.registerNodeType("maestro/intake_gate",        IntakeGateNode);
    LiteGraph.registerNodeType("maestro/multiplier_node",    MultiplierNode);

    // Port type colors
    LiteGraph.default_connection_color_byType = {
        task:      "#60a5fa",
        condition: "#f59e0b",
        data:      "#34d399",
    };
    LiteGraph.default_connection_color_byTypeOff = {
        task:      "#1e3a5f",
        condition: "#451a03",
        data:      "#052e16",
    };
}

// ============================================================
// BACK-EDGE RENDERING
// ============================================================

function patchLinkRendering() {
    // Override renderLink to draw back-edges as dashed amber lines.
    // A back-edge is one where the source stage has a higher DB position
    // than the target (meaning the token loops backward in the pipeline).
    const _orig = LGraphCanvas.prototype.renderLink;
    if (!_orig) return; // safety if API changes

    LGraphCanvas.prototype.renderLink = function(ctx, a, b, link, skip_border, flow, color, start_dir, end_dir, num_sublines) {
        let usedColor = color;
        let dashed = false;

        if (link && link._back_edge) {
            usedColor = PE_BACK_EDGE_COLOR;
            dashed = true;
        }

        if (dashed) {
            ctx.save();
            ctx.setLineDash([8, 5]);
        }
        _orig.call(this, ctx, a, b, link, skip_border, flow, usedColor, start_dir, end_dir, num_sublines);
        if (dashed) {
            ctx.restore();
        }
    };
}

function classifyBackEdges() {
    // Mark links as _back_edge based on DB position order.
    if (!_graph) return;
    for (const node of _graph._nodes) {
        if (!node.outputs) continue;
        for (const out of node.outputs) {
            if (!out.links) continue;
            for (const linkId of out.links) {
                const link = _graph.links[linkId];
                if (!link) continue;
                const fromPos = _stagePosMap[link.origin_id] ?? 0;
                const toPos   = _stagePosMap[link.target_id] ?? 0;
                link._back_edge = fromPos > toPos;
                link.color = link._back_edge ? PE_BACK_EDGE_COLOR : undefined;
            }
        }
    }
}

// ============================================================
// API HELPERS
// ============================================================

async function _apiFetch(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch("/api" + path, opts);
    if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
    }
    return r.json();
}

const apiGet  = (path)       => _apiFetch("GET",    path);
const apiPost = (path, body) => _apiFetch("POST",   path, body);
const apiPut  = (path, body) => _apiFetch("PUT",    path, body);
const apiDel  = (path)       => _apiFetch("DELETE", path);

// ============================================================
// DB → GRAPH (LOAD)
// ============================================================

async function loadTemplate() {
    _templateData = await apiGet(`/pipelines/${_templateId}`);
    document.title = `${_templateData.name} — Pipeline Editor`;
    document.getElementById("pe-template-name").textContent = _templateData.name;

    // Show non-blocking warning banner for built-in templates
    let banner = document.getElementById("pe-builtin-banner");
    if (_templateData.is_builtin) {
        if (!banner) {
            banner = document.createElement("div");
            banner.id = "pe-builtin-banner";
            banner.style.cssText =
                "background:#451a03;border-bottom:1px solid #92400e;color:#fbbf24;" +
                "font-size:13px;padding:8px 20px;text-align:center;";
            banner.innerHTML =
                "Editing a built-in template. " +
                "<a href='/pipelines' style='color:#fcd34d;text-decoration:underline'>Clone it first</a> " +
                "to make a private copy.";
            document.getElementById("pe-topbar").insertAdjacentElement("afterend", banner);
        }
        banner.style.display = "block";
    } else if (banner) {
        banner.style.display = "none";
    }

    _kanbanColumns = (_templateData.config?.kanban_columns || [])
        .slice()
        .sort((a, b) => (a.x_min ?? 0) - (b.x_min ?? 0));

    buildGraphFromTemplate(_templateData);
    renderColumnBandHeaders();
    setSaveStatus("Loaded");
}

function buildGraphFromTemplate(data) {
    _graph.clear();
    _stagePosMap = {};
    _dbTransitions = {};

    const nodeByKey = {};

    // --- create nodes for each stage ---
    const stages = (data.stages || []).sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
    stages.forEach((stage, idx) => {
        const isFactory         = stage.agent_type === "factory_node";
        const isHumanGate       = stage.agent_type === "human_gate";
        const isStaticAnalysis  = stage.agent_type === "static_analysis_widget";
        const isDangerousEdit   = stage.agent_type === "dangerous_edit_llm_agent";
        const isParallelAgents  = stage.agent_type === "parallel_agents";
        const isMultiplierNode  = stage.agent_type === "multiplier_node";
        const nodeType = isFactory        ? "maestro/factory"
                       : isHumanGate      ? "maestro/human_gate"
                       : isStaticAnalysis  ? "maestro/static_analysis"
                       : isDangerousEdit   ? "maestro/dangerous_edit"
                       : isParallelAgents  ? "maestro/parallel_agents"
                       : isMultiplierNode  ? "maestro/multiplier_node"
                       : "maestro/stage";
        const node      = LiteGraph.createNode(nodeType);
        const x = 80 + idx * 280;
        const y = 200 + (idx % 2) * 120;
        const cfg = stage.config || {};
        node.pos = [
            cfg._canvas_x !== undefined ? cfg._canvas_x : x,
            cfg._canvas_y !== undefined ? cfg._canvas_y : y,
        ];
        node.title = stage.label || stage.stage_key;

        if (isFactory) {
            const triggers = cfg.factory_trigger || ["manual"];
            const srcCfg   = cfg.factory_source_config || {};
            node.properties = {
                stage_id:                   stage.id,
                stage_key:                  stage.stage_key,
                label:                      stage.label || stage.stage_key,
                color:                      stage.color || "#065f46",
                factory_source_type:        cfg.factory_source_type || "folder",
                factory_source_config_json: JSON.stringify(srcCfg, null, 2),
                factory_segmentation_mode:  cfg.factory_segmentation_mode || "mechanical",
                factory_entry_stage:        cfg.factory_entry_stage || "idea",
                factory_title_template:     (cfg.factory_card_template || {}).title_template || "{filename}",
                factory_desc_template:      (cfg.factory_card_template || {}).description_template || "",
                factory_triggers:           triggers,
                factory_cron_schedule:      srcCfg.cron_schedule || cfg.factory_cron_schedule || "",
                intent:                     cfg.intent || "",
            };
        } else if (isStaticAnalysis) {
            node.properties = {
                stage_id:     stage.id,
                stage_key:    stage.stage_key,
                label:        stage.label || stage.stage_key,
                agent_type:   "static_analysis_widget",
                output_key:   cfg.output_key   || "static_analysis",
                file_pattern: cfg.file_pattern || "**/*.py",
                max_files:    cfg.max_files    ?? 50,
            };
        } else if (isDangerousEdit) {
            node.properties = {
                stage_id:            stage.id,
                stage_key:           stage.stage_key,
                label:               stage.label || stage.stage_key,
                agent_type:          "dangerous_edit_llm_agent",
                system_prompt:       cfg.system_prompt || "",
                max_turns:           cfg.max_turns ?? 200,
                agent_tools:         Array.isArray(cfg.agent_tools)
                                       ? cfg.agent_tools.join(", ")
                                       : (cfg.agent_tools || ""),
                required_input_keys: Array.isArray(cfg.required_input_keys)
                                       ? cfg.required_input_keys.join(", ")
                                       : (cfg.required_input_keys || ""),
            };
        } else if (isParallelAgents) {
            node.properties = {
                stage_id:   stage.id,
                stage_key:  stage.stage_key,
                label:      stage.label || stage.stage_key,
                agent_type: "parallel_agents",
                n:          cfg.n ?? 3,
                output_key: cfg.output_key || "parallel_agents_output",
                max_turns:  cfg.max_turns ?? 30,
                agents:     Array.isArray(cfg.agents) ? cfg.agents : [],
                dynamic_agents_from_key: cfg.dynamic_agents_from_key || "",
                // preserved transparently — not exposed as editable fields
                _cfg_subagent_type:                cfg.subagent_type || "",
                _cfg_agent_tools:                  cfg.agent_tools   || null,
                _cfg_agent_system_prompt_template: cfg.agent_system_prompt_template || "",
            };
            node._syncSize?.();
        } else if (isMultiplierNode) {
            node.properties = {
                stage_id:            stage.id,
                stage_key:           stage.stage_key,
                label:               stage.label || stage.stage_key,
                agent_type:          "multiplier_node",
                color:               stage.color || "#5b21b6",
                n:                   cfg.n ?? 3,
                agent_system_prompt: cfg.agent_system_prompt || "",
                agent_tools:         Array.isArray(cfg.agent_tools)
                                       ? cfg.agent_tools.join(", ")
                                       : (cfg.agent_tools || ""),
                agent_max_turns:     cfg.agent_max_turns ?? 15,
                agents:              cfg.agents ? JSON.stringify(cfg.agents, null, 2) : "",
                collapser_mode:      cfg.collapser_mode || "vote_tally",
                tally_strategy:      cfg.tally_strategy || "majority",
                on_tie:              cfg.on_tie || "reject",
                judge_system_prompt: cfg.judge_system_prompt || "",
                judge_max_turns:     cfg.judge_max_turns ?? 10,
                required_input_keys: Array.isArray(cfg.required_input_keys)
                                       ? cfg.required_input_keys.join(", ")
                                       : (cfg.required_input_keys || ""),
                output_key:          cfg.output_key || "fan_out_result",
            };
        } else {
            const agentTypeKey = stage.agent_type || "planning_agent";
            const agentTypeMeta = _agentTypes.find(at => (at.type_key || at.key || at.name) === agentTypeKey);
            node.properties = {
                stage_id:             stage.id,
                stage_key:            stage.stage_key,
                label:                stage.label || stage.stage_key,
                agent_type:           agentTypeKey,
                executor_type:        agentTypeMeta?.executor_type || "infrastructure",
                color:                stage.color || (isHumanGate ? "#be185d" : "#1e40af"),
                intent:               cfg.intent || "",
                system_prompt:        cfg.system_prompt || "",
                gate_type:            cfg.gate_type || "llm_judge",
                max_retries:          cfg.max_retries ?? 3,
                required_input_keys:  Array.isArray(cfg.required_input_keys)
                                        ? cfg.required_input_keys.join(", ")
                                        : (cfg.required_input_keys || ""),
                output_keys:          Array.isArray(cfg.output_keys)
                                        ? cfg.output_keys.join(", ")
                                        : (cfg.output_keys || ""),
                required_tool_groups: Array.isArray(cfg.required_tool_groups)
                                        ? cfg.required_tool_groups
                                        : [],
            };
        }

        const _defaultColor = isFactory        ? "#065f46"
                            : isHumanGate      ? "#be185d"
                            : isStaticAnalysis  ? "#b45309"
                            : isDangerousEdit   ? "#7f1d1d"
                            : isParallelAgents  ? "#0c4a6e"
                            : "#1e40af";
        node.color   = node.properties.color || _defaultColor;
        node.bgcolor = _darken(node.color);

        _graph.add(node);
        nodeByKey[stage.stage_key] = node;
        _stagePosMap[node.id] = stage.position ?? idx;
    });

    // --- connect transitions ---
    // Forward edges (pass from lower→higher stage position) become LiteGraph wires.
    // Back-edges (fail/reject from higher→lower) are stored as node annotations —
    // they show as amber text inside the target node rather than extra input slots,
    // because multiple inputs imply "AND" semantics but pipeline stages use "OR".
    const transitions = data.transitions || [];
    transitions.forEach(t => {
        const fromNode = nodeByKey[t.from_stage_key];
        const toNode   = nodeByKey[t.to_stage_key];
        if (!fromNode || !toNode) return;

        // Ensure the output port exists for this condition
        let slotIdx = (fromNode.outputs || []).findIndex(o => o.name === t.condition);
        if (slotIdx === -1) {
            slotIdx = fromNode.outputs.length;
            fromNode.addOutput(t.condition, "task");
        }

        const fromPos = _stagePosMap[fromNode.id] ?? 0;
        const toPos   = _stagePosMap[toNode.id] ?? 0;

        if (fromPos > toPos) {
            // Back-edge: annotate the target node; no LiteGraph wire
            if (!toNode._backSources) toNode._backSources = [];
            toNode._backSources.push({
                from_key: t.from_stage_key, to_key: t.to_stage_key,
                condition: t.condition, db_id: t.id,
            });
            _dbTransitions[t.id] = { from_key: t.from_stage_key, to_key: t.to_stage_key, condition: t.condition };
            return;
        }

        // Forward edge: connect to the single "in" slot
        fromNode.connect(slotIdx, toNode, 0);

        // Record DB ID so we can delete by ID on save
        const link = _lastCreatedLink(fromNode, slotIdx);
        if (link) {
            link._db_transition_id = t.id;
            _dbTransitions[t.id] = { from_key: t.from_stage_key, to_key: t.to_stage_key, condition: t.condition };
        }
    });

    _nodeByKey = nodeByKey;
    _annotateBackEdges(nodeByKey);
    classifyBackEdges();
    _canvas.setDirty(true, true);
}

function _annotateBackEdges(nodeByKey) {
    const LINE_H = 14, PAD = 5;
    for (const node of Object.values(nodeByKey)) {
        const props = node.properties || {};
        const inKeys  = (props.required_input_keys || "").trim();
        const outKeys = (props.output_keys || "").trim();
        const lines   = [];
        if (inKeys)  lines.push(`in:  ${inKeys}`);
        if (outKeys) lines.push(`out: ${outKeys}`);
        if (!lines.length) continue;

        const extraH = lines.length * LINE_H + PAD * 2 + 6;
        node.size[1] += extraH;
        node._backAnnotationLines = lines;
        node._backAnnotationH = extraH;

        node.onDrawForeground = function(ctx) {
            if (!this._backAnnotationLines?.length) return;
            const sepY = this.size[1] - this._backAnnotationH + 3;
            ctx.save();
            ctx.strokeStyle = "rgba(245,158,11,0.35)";
            ctx.lineWidth = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath();
            ctx.moveTo(4, sepY); ctx.lineTo(this.size[0] - 4, sepY);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.font = "10px monospace";
            ctx.fillStyle = "#f59e0b";
            this._backAnnotationLines.forEach((line, i) => {
                ctx.fillText(line, 6, sepY + PAD + i * LINE_H + 9);
            });
            ctx.restore();
        };
    }
}

function drawBackEdgeOverlays(ctx) {
    if (!_graph) return;
    const TH = LiteGraph.NODE_TITLE_HEIGHT || 20;
    ctx.save();
    ctx.strokeStyle = "rgba(245,158,11,0.7)";
    ctx.lineWidth   = 1.5;
    ctx.lineJoin    = "round";
    ctx.lineCap     = "round";

    for (const node of _graph._nodes) {
        if (!node._backSources?.length) continue;
        const tx = node.pos[0];
        const ty = node.pos[1];
        const th = node.size[1];

        for (const be of node._backSources) {
            const src = _nodeByKey[be.from_key];
            if (!src) continue;
            const sx = src.pos[0];
            const sy = src.pos[1];
            const sh = src.size[1];
            const sw = src.size[0];

            // Start: right edge of source node at output-slot height
            const startX = sx + sw;
            const startY = sy + TH + 12;
            // End: left edge of target node at input-slot height
            const endX = tx;
            const endY = ty + TH + 12;

            // Control points arc below both nodes
            const belowY = Math.max(sy + sh, ty + th) + 38;
            const cp1x = startX + 48;
            const cp1y = belowY;
            const cp2x = endX - 48;
            const cp2y = belowY;

            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            ctx.moveTo(startX, startY);
            ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, endX, endY);
            ctx.stroke();

            // Arrowhead at target
            const angle = Math.atan2(endY - cp2y, endX - cp2x);
            const AL = 9;
            ctx.setLineDash([]);
            ctx.beginPath();
            ctx.moveTo(endX, endY);
            ctx.lineTo(endX - AL * Math.cos(angle - 0.38), endY - AL * Math.sin(angle - 0.38));
            ctx.moveTo(endX, endY);
            ctx.lineTo(endX - AL * Math.cos(angle + 0.38), endY - AL * Math.sin(angle + 0.38));
            ctx.stroke();
        }
    }

    ctx.restore();
}

// ============================================================
// KANBAN COLUMN BANDS
// ============================================================

function _kanbanColumnForX(x) {
    // Return the column key for a given graph X coordinate, or null if none defined.
    if (!_kanbanColumns.length) return null;
    let col = _kanbanColumns[0];
    for (const c of _kanbanColumns) {
        if (x >= c.x_min) col = c;
        else break;
    }
    return col.key;
}

// Two alternating band fill colors — even/odd indices, never the same adjacent
const PE_BAND_EVEN = "#1e3a5f";  // blue-navy tint
const PE_BAND_ODD  = "#1a2e1a";  // forest-green tint

function drawColumnBands(ctx) {
    if (!_kanbanColumns.length || !_canvas) return;
    const visRect = _canvas.visible_rect || [0, 0, 2000, 2000];
    const scale = _canvas.ds?.scale || 1;
    const BAND_HEIGHT = 100000;

    ctx.save();
    for (let i = 0; i < _kanbanColumns.length; i++) {
        const col = _kanbanColumns[i];
        const x1 = col.x_min;
        const x2 = i + 1 < _kanbanColumns.length ? _kanbanColumns[i + 1].x_min : x1 + 3000;
        const bandColor = (i % 2 === 0) ? PE_BAND_EVEN : PE_BAND_ODD;

        // Band fill
        ctx.globalAlpha = 0.18;
        ctx.fillStyle = bandColor;
        ctx.fillRect(x1, -BAND_HEIGHT / 2, x2 - x1, BAND_HEIGHT);
        ctx.globalAlpha = 1.0;

        // Divider line (not for first column)
        if (i > 0) {
            ctx.globalAlpha = 0.3;
            ctx.strokeStyle = "#475569";
            ctx.lineWidth = 1.5 / scale;
            ctx.setLineDash([5 / scale, 4 / scale]);
            ctx.beginPath();
            ctx.moveTo(x1, -BAND_HEIGHT / 2);
            ctx.lineTo(x1, BAND_HEIGHT / 2);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.globalAlpha = 1.0;
        }

        // Column label pinned to top of visible area
        const topY = visRect[1] + 8 / scale;
        const fontSize = Math.max(9, Math.min(14, 13 / scale));
        ctx.globalAlpha = 0.55;
        ctx.fillStyle = "#94a3b8";
        ctx.font = `bold ${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.fillText(col.label.toUpperCase(), x1 + (x2 - x1) / 2, topY + fontSize);
        ctx.globalAlpha = 1.0;
    }
    ctx.restore();
}

// Update the fixed HTML column header strip above the canvas
function renderColumnBandHeaders() {
    let strip = document.getElementById("pe-col-header-strip");
    if (!strip) {
        strip = document.createElement("div");
        strip.id = "pe-col-header-strip";
        document.getElementById("pe-canvas-wrap").prepend(strip);
    }
    strip.innerHTML = "";
    if (!_kanbanColumns.length) {
        strip.style.display = "none";
        return;
    }
    strip.style.display = "flex";
    _kanbanColumns.forEach((col, i) => {
        const div = document.createElement("div");
        div.className = "pe-col-header-cell";
        div.style.background = (i % 2 === 0) ? PE_BAND_EVEN + "44" : PE_BAND_ODD + "44";
        div.textContent = col.label.toUpperCase();
        strip.appendChild(div);
    });
}

function _lastCreatedLink(node, slotIdx) {
    // Retrieve the most recently created link on a given output slot
    const out = node.outputs?.[slotIdx];
    if (!out?.links?.length) return null;
    const linkId = out.links[out.links.length - 1];
    return _graph.links[linkId] || null;
}

function _darken(hex) {
    // Produce a darkened version of a hex color for node bgcolor
    try {
        const r = parseInt(hex.slice(1,3), 16);
        const g = parseInt(hex.slice(3,5), 16);
        const b = parseInt(hex.slice(5,7), 16);
        const d = (v) => Math.max(0, Math.floor(v * 0.4)).toString(16).padStart(2,"0");
        return `#${d(r)}${d(g)}${d(b)}`;
    } catch (_) { return "#0f172a"; }
}

// ============================================================
// GRAPH → DB (SAVE)
// ============================================================

async function saveGraph() {
    if (!_templateId) { peToast("No template ID — cannot save", "err"); return; }

    const btn = document.getElementById("btn-save");
    btn.disabled = true;
    setSaveStatus("Saving…");

    try {
        // 1. Upsert all stage + factory + static_analysis nodes (sorted by canvas x for position order)
        const allPipelineNodes = _graph._nodes
            .filter(n => n.type === "maestro/stage" || n.type === "maestro/factory" || n.type === "maestro/static_analysis" || n.type === "maestro/human_gate" || n.type === "maestro/dangerous_edit" || n.type === "maestro/parallel_agents" || n.type === "maestro/multiplier_node")
            .sort((a, b) => a.pos[0] - b.pos[0]);

        for (let posIdx = 0; posIdx < allPipelineNodes.length; posIdx++) {
            const node = allPipelineNodes[posIdx];
            const p = node.properties;
            let stageBody;

            if (node.type === "maestro/factory") {
                let srcCfg = {};
                try { srcCfg = JSON.parse(p.factory_source_config_json || "{}"); } catch (_) {}
                const triggers = Array.isArray(p.factory_triggers) ? p.factory_triggers : [];
                if (p.factory_cron_schedule) srcCfg.cron_schedule = p.factory_cron_schedule;
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: "factory_node",
                    color:      p.color,
                    position:   posIdx,
                    config: {
                        factory_source_type:        p.factory_source_type,
                        factory_source_config:      srcCfg,
                        factory_segmentation_mode:  p.factory_segmentation_mode,
                        factory_entry_stage:        p.factory_entry_stage || "idea",
                        factory_trigger:            triggers,
                        factory_card_template: {
                            title_template:       p.factory_title_template || "{filename}",
                            description_template: p.factory_desc_template  || "",
                        },
                        intent:    p.intent,
                        _canvas_x: Math.round(node.pos[0]),
                        _canvas_y: Math.round(node.pos[1]),
                    },
                };
            } else if (node.type === "maestro/static_analysis") {
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: "static_analysis_widget",
                    color:      "#b45309",
                    position:   posIdx,
                    config: {
                        output_key:   p.output_key   || "static_analysis",
                        file_pattern: p.file_pattern || "**/*.py",
                        max_files:    parseInt(p.max_files) || 50,
                        _canvas_x: Math.round(node.pos[0]),
                        _canvas_y: Math.round(node.pos[1]),
                    },
                };
            } else if (node.type === "maestro/dangerous_edit") {
                const _agentTools = (p.agent_tools || "").split(",").map(s=>s.trim()).filter(Boolean);
                const _reqKeys    = (p.required_input_keys || "").split(",").map(s=>s.trim()).filter(Boolean);
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: "dangerous_edit_llm_agent",
                    color:      "#7f1d1d",
                    position:   posIdx,
                    config: {
                        system_prompt:        p.system_prompt || null,
                        max_turns:            parseInt(p.max_turns) || 200,
                        agent_tools:          _agentTools.length ? _agentTools : null,
                        required_input_keys:  _reqKeys,
                        _canvas_x: Math.round(node.pos[0]),
                        _canvas_y: Math.round(node.pos[1]),
                    },
                };
            } else if (node.type === "maestro/parallel_agents") {
                const n = Math.max(1, Math.min(10, parseInt(p.n) || 3));
                const agents = (Array.isArray(p.agents) ? p.agents : []).slice(0, n);
                while (agents.length < n)
                    agents.push({name: `agent_${agents.length}`, description: "", system_prompt: "", tool_grouping_id: null});
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: "parallel_agents",
                    color:      "#0c4a6e",
                    position:   posIdx,
                    config: {
                        n,
                        agents,
                        output_key: p.output_key || "parallel_agents_output",
                        max_turns:  parseInt(p.max_turns) || 30,
                        _canvas_x:  Math.round(node.pos[0]),
                        _canvas_y:  Math.round(node.pos[1]),
                        ...(p.dynamic_agents_from_key             ? { dynamic_agents_from_key:             p.dynamic_agents_from_key }             : {}),
                        ...(p._cfg_subagent_type                  ? { subagent_type:                       p._cfg_subagent_type }                  : {}),
                        ...(p._cfg_agent_tools                    ? { agent_tools:                         p._cfg_agent_tools }                    : {}),
                        ...(p._cfg_agent_system_prompt_template   ? { agent_system_prompt_template:        p._cfg_agent_system_prompt_template }   : {}),
                    },
                };
            } else if (node.type === "maestro/multiplier_node") {
                let agentsList = null;
                if (p.agents) {
                    try { agentsList = JSON.parse(p.agents); } catch (_) { agentsList = null; }
                }
                const _reqKeys = (p.required_input_keys || "").split(",").map(s=>s.trim()).filter(Boolean);
                const _tools   = (p.agent_tools || "").split(",").map(s=>s.trim()).filter(Boolean);
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: "multiplier_node",
                    color:      p.color || "#5b21b6",
                    position:   posIdx,
                    config: {
                        n:                    Math.max(1, Math.min(20, parseInt(p.n) || 3)),
                        agent_system_prompt:  p.agent_system_prompt || "",
                        agent_tools:          _tools.length ? _tools : [],
                        agent_max_turns:      parseInt(p.agent_max_turns) || 15,
                        collapser_mode:       p.collapser_mode || "vote_tally",
                        tally_strategy:       p.tally_strategy || "majority",
                        on_tie:               p.on_tie || "reject",
                        judge_system_prompt:  p.judge_system_prompt || "",
                        judge_max_turns:      parseInt(p.judge_max_turns) || 10,
                        required_input_keys:  _reqKeys,
                        output_key:           p.output_key || "fan_out_result",
                        ...(agentsList ? { agents: agentsList } : {}),
                        _canvas_x: Math.round(node.pos[0]),
                        _canvas_y: Math.round(node.pos[1]),
                    },
                };
            } else {
                stageBody = {
                    stage_key:  p.stage_key,
                    label:      p.label || p.stage_key,
                    agent_type: p.agent_type,
                    color:      p.color,
                    position:   posIdx,
                    config: {
                        intent:              p.intent,
                        system_prompt:       p.system_prompt,
                        gate_type:           p.gate_type,
                        max_retries:         parseInt(p.max_retries) || 3,
                        required_input_keys:  p.required_input_keys.split(",").map(s=>s.trim()).filter(Boolean),
                        output_keys:          p.output_keys.split(",").map(s=>s.trim()).filter(Boolean),
                        required_tool_groups: Array.isArray(p.required_tool_groups) ? p.required_tool_groups : [],
                        _canvas_x: Math.round(node.pos[0]),
                        _canvas_y: Math.round(node.pos[1]),
                    },
                };
            }

            // Stamp kanban_column from current X position
            if (_kanbanColumns.length) {
                stageBody.config.kanban_column = _kanbanColumnForX(node.pos[0]);
            }

            if (p.stage_id) {
                await apiPut(`/pipelines/${_templateId}/stages/${p.stage_id}`, stageBody);
            } else {
                const created = await apiPost(`/pipelines/${_templateId}/stages`, stageBody);
                node.properties.stage_id = created.id;
            }
        }

        // Persist kanban column definitions on the template config
        if (_templateId) {
            await apiPut(`/pipelines/${_templateId}`, {
                config: { kanban_columns: _kanbanColumns },
            });
        }

        // Update stage position map after save so back-edge detection uses new positions
        const stageNodes = allPipelineNodes.filter(n =>
            n.type === "maestro/stage" || n.type === "maestro/static_analysis" || n.type === "maestro/human_gate" || n.type === "maestro/dangerous_edit" || n.type === "maestro/parallel_agents" || n.type === "maestro/multiplier_node"
        );
        stageNodes.forEach((n, i) => { _stagePosMap[n.id] = i; });

        // 2. Delete ALL existing transitions, then recreate from current graph
        const existing = await apiGet(`/pipelines/${_templateId}/transitions`);
        await Promise.all(existing.map(t => apiDel(`/pipelines/${_templateId}/transitions/${t.id}`)));

        // 3. Create transitions from graph links
        const nodeById = {};
        for (const n of _graph._nodes) nodeById[n.id] = n;

        for (const link of Object.values(_graph.links)) {
            const fromNode = nodeById[link.origin_id];
            const toNode   = nodeById[link.target_id];
            if (!fromNode || !toNode) continue;
            const validTypes = new Set(["maestro/stage", "maestro/factory", "maestro/human_gate", "maestro/static_analysis", "maestro/dangerous_edit", "maestro/parallel_agents", "maestro/multiplier_node"]);
            if (!validTypes.has(fromNode.type) || !validTypes.has(toNode.type)) continue;

            const condition = fromNode.outputs?.[link.origin_slot]?.name || "pass";
            const priority  = link._back_edge ? 10 : 1;

            await apiPost(`/pipelines/${_templateId}/transitions`, {
                from_stage_key: fromNode.properties.stage_key,
                to_stage_key:   toNode.properties.stage_key,
                condition,
                priority,
            });
        }

        // Save back-edges stored as node annotations (not LiteGraph links)
        for (const n of _graph._nodes) {
            if (!n._backSources?.length) continue;
            for (const be of n._backSources) {
                await apiPost(`/pipelines/${_templateId}/transitions`, {
                    from_stage_key: be.from_key,
                    to_stage_key:   be.to_key,
                    condition:      be.condition,
                    priority:       10,
                });
            }
        }

        // Re-classify back edges after save
        classifyBackEdges();
        _canvas.setDirty(true, true);

        peToast("Saved", "ok");
        setSaveStatus("Saved");

    } catch (e) {
        peToast(`Save failed: ${e.message}`, "err");
        setSaveStatus("Save failed");
    } finally {
        btn.disabled = false;
    }
}

function setSaveStatus(msg) {
    const el = document.getElementById("pe-save-status");
    if (el) el.textContent = msg;
}

// ============================================================
// PROPERTY PANEL
// ============================================================

function openPanel(node) {
    _panelNode = node;
    _panelSnapshot = JSON.parse(JSON.stringify(node.properties));

    const panel = document.getElementById("pipeline-property-panel");
    const body  = document.getElementById("pe-panel-body");
    const title = document.getElementById("pe-panel-title");

    // Determine node sub-type for template selection
    const typeMap = {
        "maestro/stage":            "tpl-stage",
        "maestro/factory":          "tpl-factory",
        "maestro/conditional":      "tpl-conditional",
        "maestro/judgment_gate":    "tpl-judgment_gate",
        "maestro/fan_out":          "tpl-fan_out",
        "maestro/human_gate":       "tpl-human_gate",
        "maestro/static_analysis":  "tpl-static-analysis",
        "maestro/dangerous_edit":   "tpl-dangerous_edit",
        "maestro/parallel_agents":  "tpl-parallel_agents",
        "maestro/multiplier_node":  "tpl-multiplier_node",
    };
    const tplId = typeMap[node.type] || "tpl-stage";
    const tpl = document.getElementById(tplId);

    body.innerHTML = "";
    if (tpl) {
        body.appendChild(document.importNode(tpl.content, true));
    }

    title.textContent = node.title + " Properties";

    // Populate fields from node.properties
    body.querySelectorAll("[data-bind]").forEach(el => {
        const key = el.dataset.bind;
        const val = node.properties[key];
        if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
            el.value = val !== undefined && val !== null ? val : "";
        } else {
            el.textContent = val !== undefined && val !== null ? String(val) : "";
        }
    });

    // Populate agent type dropdown
    if (tplId === "tpl-stage") {
        _populateAgentTypeSelect(body, node.properties.agent_type);
        _renderConditionsList(body, node);
        _setupConditionAdd(body, node);
        _setupToolGroupsPanel(body, node);
    }

    // Factory-specific panel wiring
    if (tplId === "tpl-factory") {
        _setupFactoryPanel(body, node);
    }

    // Parallel agents panel wiring
    if (tplId === "tpl-parallel_agents") {
        _setupParallelAgentsPanel(body, node);
    }

    // Wire up lightning buttons
    body.querySelectorAll(".pe-lightning").forEach(btn => {
        btn.addEventListener("click", () => {
            const field = btn.dataset.field;
            const target = body.querySelector(`[data-bind="${field}"]`);
            if (target) generateField(field, target, node);
        });
    });

    // Show panel
    panel.classList.remove("pe-panel-closed");
    panel.classList.add("pe-panel-open");
    document.getElementById("pe-canvas-wrap").classList.add("panel-open");
}

function closePanel() {
    _panelNode = null;
    const panel = document.getElementById("pipeline-property-panel");
    panel.classList.remove("pe-panel-open");
    panel.classList.add("pe-panel-closed");
    document.getElementById("pe-canvas-wrap").classList.remove("panel-open");
}

function applyPanel() {
    if (!_panelNode) return;
    const body = document.getElementById("pe-panel-body");

    body.querySelectorAll("[data-bind]").forEach(el => {
        const key = el.dataset.bind;
        if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
            let val = el.value;
            if (key === "max_retries" || key === "fan_n" || key === "attempts_n" || key === "autopilot_hours") {
                val = parseInt(val) || 0;
            }
            _panelNode.properties[key] = val;
        }
    });

    // Collect factory trigger checkboxes (not data-bind fields)
    if (_panelNode.type === "maestro/factory") {
        const triggers = [];
        if (body.querySelector("#pf-trigger-manual")?.checked)      triggers.push("manual");
        if (body.querySelector("#pf-trigger-predecessor")?.checked)  triggers.push("predecessor_complete");
        if (body.querySelector("#pf-trigger-cron")?.checked)         triggers.push("cron");
        _panelNode.properties.factory_triggers = triggers;
    }

    // Collect parallel agents config
    if (_panelNode.type === "maestro/parallel_agents") {
        const n = Math.max(1, Math.min(10, parseInt(document.getElementById("pf-pa-n")?.value || "3")));
        _panelNode.properties.n = n;
        _panelNode.properties.output_key = document.getElementById("pf-pa-output-key")?.value || "parallel_agents_output";
        _panelNode.properties.max_turns  = parseInt(document.getElementById("pf-pa-max-turns")?.value || "30");
        const agents = [];
        for (let i = 0; i < n; i++) {
            agents.push({
                name:              document.getElementById(`pf-pa-name-${i}`)?.value     || `agent_${i}`,
                description:       document.getElementById(`pf-pa-desc-${i}`)?.value     || "",
                system_prompt:     document.getElementById(`pf-pa-prompt-${i}`)?.value   || "",
                tool_grouping_id:  parseInt(document.getElementById(`pf-pa-tg-${i}`)?.value || "0") || null,
            });
        }
        _panelNode.properties.agents = agents;
        _panelNode._syncSize?.();
    }

    // Update node title to match label
    if (_panelNode.properties.label) {
        _panelNode.title = _panelNode.properties.label;
    }
    if (_panelNode.properties.color) {
        _panelNode.color   = _panelNode.properties.color;
        _panelNode.bgcolor = _darken(_panelNode.properties.color);
    }

    _canvas.setDirty(true, true);
    setSaveStatus("Unsaved changes");
}

function revertPanel() {
    if (!_panelNode) return;
    _panelNode.properties = JSON.parse(JSON.stringify(_panelSnapshot));
    openPanel(_panelNode); // re-render panel with original values
}

function _setupFactoryPanel(body, node) {
    const p = node.properties;
    const triggers = Array.isArray(p.factory_triggers) ? p.factory_triggers : [];

    // Populate trigger checkboxes
    const cbManual      = body.querySelector("#pf-trigger-manual");
    const cbPredecessor = body.querySelector("#pf-trigger-predecessor");
    const cbCron        = body.querySelector("#pf-trigger-cron");
    const cronInput     = body.querySelector("#pf-cron-schedule");
    if (cbManual)      cbManual.checked      = triggers.includes("manual");
    if (cbPredecessor) cbPredecessor.checked = triggers.includes("predecessor_complete");
    if (cbCron)        cbCron.checked        = triggers.includes("cron");
    if (cronInput)     cronInput.style.display = triggers.includes("cron") ? "" : "none";

    if (cbCron) cbCron.addEventListener("change", () => {
        if (cronInput) cronInput.style.display = cbCron.checked ? "" : "none";
    });

    // Wire Run Now button
    const runBtn    = body.querySelector("#pf-run-now");
    const runStatus = body.querySelector("#pf-run-status");
    if (runBtn) runBtn.addEventListener("click", () => runFactoryNow(node, runStatus));
}

// Tool groupings cache for parallel agent panel dropdowns
let _toolGroupingsCache = null;
async function _loadToolGroupings() {
    if (_toolGroupingsCache) return _toolGroupingsCache;
    try { _toolGroupingsCache = await apiGet("/tool-groupings"); } catch (_) { _toolGroupingsCache = []; }
    return _toolGroupingsCache;
}

function _renderParallelAgentCards(container, n, agents, toolGroupings) {
    container.innerHTML = "";
    for (let i = 0; i < n; i++) {
        const ag = agents[i] || {};
        const tgOptions = toolGroupings.map(tg =>
            `<option value="${tg.id}" ${ag.tool_grouping_id == tg.id ? "selected" : ""}>${_peEscHtml(tg.name)}</option>`
        ).join("");
        const card = document.createElement("div");
        card.className = "pe-agent-card";
        card.innerHTML = `
            <div class="pe-agent-card-header">Agent ${i + 1}</div>
            <div class="pe-field-row">
                <label for="pf-pa-name-${i}">Name</label>
                <input type="text" id="pf-pa-name-${i}" class="pe-input" value="${_peEscHtml(ag.name || `agent_${i}`)}">
            </div>
            <div class="pe-field-row">
                <label for="pf-pa-desc-${i}">Description</label>
                <input type="text" id="pf-pa-desc-${i}" class="pe-input" value="${_peEscHtml(ag.description || "")}">
            </div>
            <div class="pe-field-row">
                <label for="pf-pa-prompt-${i}">System prompt</label>
                <textarea id="pf-pa-prompt-${i}" class="pe-input pe-textarea" rows="4">${_peEscHtml(ag.system_prompt || "")}</textarea>
            </div>
            <div class="pe-field-row">
                <label for="pf-pa-tg-${i}">Tool grouping</label>
                <select id="pf-pa-tg-${i}" class="pe-input">
                    <option value="">— none (submit_work only) —</option>
                    ${tgOptions}
                </select>
            </div>`;
        container.appendChild(card);
    }
}

function _setupParallelAgentsPanel(body, node) {
    const p = node.properties;
    const container = body.querySelector("#pf-pa-cards");
    if (!container) return;

    const nInput = body.querySelector("#pf-pa-n");
    if (nInput) nInput.value = p.n ?? 3;

    const renderCards = (n) => {
        _loadToolGroupings().then(tgs => {
            _renderParallelAgentCards(container, n, p.agents || [], tgs);
        });
    };

    renderCards(parseInt(nInput?.value || "3"));

    if (p.dynamic_agents_from_key) {
        const note = document.createElement("p");
        note.className = "pe-field-note";
        note.style.cssText = "color:#94a3b8;font-size:11px;margin:4px 0 0;";
        note.textContent = `Dynamic mode: one agent is spawned per item in "${p.dynamic_agents_from_key}" at runtime. Agent cards below are ignored.`;
        container.insertBefore(note, container.firstChild);
    }

    if (nInput) {
        nInput.addEventListener("change", () => {
            const n = Math.max(1, Math.min(10, parseInt(nInput.value) || 3));
            nInput.value = n;
            renderCards(n);
        });
    }
}

async function runFactoryNow(node, statusEl) {
    const stageId = node.properties.stage_id;
    if (!stageId) {
        if (statusEl) statusEl.textContent = "Save first to get a stage ID.";
        return;
    }
    // Determine project from URL or a global
    const project = window._peProject || new URLSearchParams(location.search).get("project") || "";
    if (!project) {
        if (statusEl) statusEl.textContent = "No project in URL (?project=Name).";
        return;
    }
    if (statusEl) { statusEl.textContent = "Running…"; statusEl.style.color = "#94a3b8"; }
    try {
        const result = await apiPost(
            `/pipelines/stages/${stageId}/trigger-factory?project=${encodeURIComponent(project)}`,
            {}
        );
        const msg = result.status === "completed"
            ? `✓ Done — ${result.cards_created} cards created`
            : `Status: ${result.status}`;
        if (statusEl) { statusEl.textContent = msg; statusEl.style.color = result.status === "completed" ? "#34d399" : "#f87171"; }
        peToast(msg, result.status === "completed" ? "ok" : "err");
    } catch (e) {
        if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = "#f87171"; }
        peToast(`Factory error: ${e.message}`, "err");
    }
}

function _agentTypeHint(typeKey) {
    const meta = _agentTypes.find(at => (at.type_key || at.key || at.name) === typeKey);
    if (!meta) return "";
    if (meta.executor_type === "custom_python")
        return "⚙ Backed by custom Python — prompt lives in stage config but logic is in app/agent/";
    if (meta.executor_type === "user_defined")
        return "User-defined agent from custom_agent_definitions";
    return "";
}

function _populateAgentTypeSelect(body, selectedType) {
    const sel = body.querySelector("#pf-agent-type");
    if (!sel) return;
    sel.innerHTML = "";
    _agentTypes.forEach(at => {
        const opt = document.createElement("option");
        opt.value = at.type_key || at.key || at.name || at;
        opt.textContent = at.display_name || opt.value;
        if (opt.value === selectedType) opt.selected = true;
        sel.appendChild(opt);
    });
    // Fallback: add current value if not in list
    if (selectedType && !Array.from(sel.options).some(o => o.value === selectedType)) {
        const opt = document.createElement("option");
        opt.value = selectedType;
        opt.textContent = selectedType;
        opt.selected = true;
        sel.prepend(opt);
    }
    const hint = body.querySelector("#pf-agent-type-hint");
    if (hint) hint.textContent = _agentTypeHint(sel.value);
    sel.addEventListener("change", () => {
        if (hint) hint.textContent = _agentTypeHint(sel.value);
    });
}

function _renderConditionsList(body, node) {
    const list = body.querySelector("#pf-conditions-list");
    if (!list) return;
    list.innerHTML = "";
    (node.outputs || []).forEach((out, idx) => {
        const row = document.createElement("div");
        row.className = "pe-condition-row";
        const tag = document.createElement("span");
        tag.className = "pe-condition-tag";
        tag.textContent = out.name;
        tag.style.borderColor = PE_CONDITION_COLORS[out.name] || "#60a5fa";
        tag.style.color       = PE_CONDITION_COLORS[out.name] || "#60a5fa";

        const del = document.createElement("button");
        del.className = "pe-condition-del";
        del.textContent = "✕";
        del.title = "Remove condition (only if no connections)";
        del.addEventListener("click", () => {
            // Don't delete if there are active connections
            if (out.links && out.links.length > 0) {
                peToast("Disconnect the edge first", "err");
                return;
            }
            node.outputs.splice(idx, 1);
            _canvas.setDirty(true, true);
            _renderConditionsList(body, node); // re-render list
        });

        row.appendChild(tag);
        row.appendChild(del);
        list.appendChild(row);
    });
}

function _setupConditionAdd(body, node) {
    const btn = body.querySelector("#pf-add-condition");
    if (!btn) return;
    btn.addEventListener("click", () => {
        const name = prompt("Condition name (pass / fail / reject / always / skip / custom):", "fail");
        if (!name || !name.trim()) return;
        const cond = name.trim().toLowerCase().replace(/\s+/g, "_");
        if ((node.outputs || []).some(o => o.name === cond)) {
            peToast("Condition already exists", "err");
            return;
        }
        node.addOutput(cond, "task");
        _canvas.setDirty(true, true);
        _renderConditionsList(body, node);
    });
}

// ============================================================
// Required tool groups panel
// ============================================================

function _renderToolGroupsList(body, node) {
    const list = body.querySelector("#pf-tool-groups-list");
    if (!list) return;
    list.innerHTML = "";

    const groups = Array.isArray(node.properties.required_tool_groups)
        ? node.properties.required_tool_groups
        : [];

    groups.forEach((group, gIdx) => {
        const row = document.createElement("div");
        row.className = "pe-tool-group-row";

        // Header: "Group N" label + delete-group button
        const header = document.createElement("div");
        header.className = "pe-tool-group-header";
        const title = document.createElement("span");
        title.textContent = `Group ${gIdx + 1} — any one must succeed`;
        const delGroup = document.createElement("button");
        delGroup.className = "pe-condition-del";
        delGroup.textContent = "✕ group";
        delGroup.title = "Remove this group";
        delGroup.addEventListener("click", () => {
            node.properties.required_tool_groups.splice(gIdx, 1);
            _renderToolGroupsList(body, node);
            _canvas.setDirty(true, true);
            setSaveStatus("Unsaved changes");
        });
        header.appendChild(title);
        header.appendChild(delGroup);
        row.appendChild(header);

        // Tool chips
        const chips = document.createElement("div");
        chips.className = "pe-tool-chips";
        if (group.length === 0) {
            const empty = document.createElement("span");
            empty.className = "pe-field-hint";
            empty.style.padding = "2px 0";
            empty.textContent = "No tools yet — add one below";
            chips.appendChild(empty);
        } else {
            group.forEach((toolName, tIdx) => {
                const chip = document.createElement("span");
                chip.className = "pe-tool-chip";
                chip.textContent = toolName;

                const chipDel = document.createElement("button");
                chipDel.className = "pe-tool-chip-del";
                chipDel.textContent = "×";
                chipDel.title = `Remove ${toolName}`;
                chipDel.addEventListener("click", () => {
                    node.properties.required_tool_groups[gIdx].splice(tIdx, 1);
                    _renderToolGroupsList(body, node);
                    _canvas.setDirty(true, true);
                    setSaveStatus("Unsaved changes");
                });
                chip.appendChild(chipDel);
                chips.appendChild(chip);
            });
        }
        row.appendChild(chips);

        // Add-tool row: dropdown + button
        const addRow = document.createElement("div");
        addRow.className = "pe-tool-group-add-row";

        const sel = document.createElement("select");
        sel.className = "pe-input pe-select";
        sel.style.flex = "1";
        sel.style.fontSize = "11px";

        const blankOpt = document.createElement("option");
        blankOpt.value = "";
        blankOpt.textContent = "— add tool —";
        sel.appendChild(blankOpt);

        Object.entries(_trackableTools.categories || {}).forEach(([catName, tools]) => {
            const og = document.createElement("optgroup");
            og.label = catName;
            tools.forEach(t => {
                const opt = document.createElement("option");
                opt.value = t;
                opt.textContent = t;
                if (group.includes(t)) opt.disabled = true;
                og.appendChild(opt);
            });
            sel.appendChild(og);
        });

        const addBtn = document.createElement("button");
        addBtn.className = "pe-btn pe-btn-xs pe-btn-secondary";
        addBtn.textContent = "+ Add";
        addBtn.addEventListener("click", () => {
            const tool = sel.value;
            if (!tool) return;
            if (!node.properties.required_tool_groups[gIdx].includes(tool)) {
                node.properties.required_tool_groups[gIdx].push(tool);
                _renderToolGroupsList(body, node);
                _canvas.setDirty(true, true);
                setSaveStatus("Unsaved changes");
            }
        });

        addRow.appendChild(sel);
        addRow.appendChild(addBtn);
        row.appendChild(addRow);
        list.appendChild(row);
    });
}

function _setupToolGroupsPanel(body, node) {
    if (!Array.isArray(node.properties.required_tool_groups)) {
        node.properties.required_tool_groups = [];
    }
    _renderToolGroupsList(body, node);

    const addGroupBtn = body.querySelector("#pf-add-tool-group");
    if (!addGroupBtn) return;
    addGroupBtn.addEventListener("click", () => {
        node.properties.required_tool_groups.push([]);
        _renderToolGroupsList(body, node);
        _canvas.setDirty(true, true);
        setSaveStatus("Unsaved changes");
    });
}

// ============================================================
// ⚡ FIELD GENERATION (streaming)
// ============================================================

async function generateField(fieldName, targetEl, node) {
    const btn = document.querySelector(`.pe-lightning[data-field="${fieldName}"]`);
    if (btn) btn.classList.add("pe-generating");
    const orig = targetEl.value;

    // Collect context from the current panel / graph
    const nodeState = {};
    document.getElementById("pe-panel-body").querySelectorAll("[data-bind]").forEach(el => {
        nodeState[el.dataset.bind] = el.value !== undefined ? el.value : el.textContent;
    });

    // Graph context: predecessor/successor node titles
    const predecessors = [], successors = [];
    if (node && _graph) {
        (node.inputs || []).forEach(inp => {
            if (inp.link !== null) {
                const link = _graph.links[inp.link];
                if (link) {
                    const pred = _graph.getNodeById(link.origin_id);
                    if (pred) predecessors.push(pred.title);
                }
            }
        });
        (node.outputs || []).forEach(out => {
            (out.links || []).forEach(linkId => {
                const link = _graph.links[linkId];
                if (link) {
                    const succ = _graph.getNodeById(link.target_id);
                    if (succ) successors.push(succ.title);
                }
            });
        });
    }

    try {
        const resp = await fetch("/api/pipelines/generate-field", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                field: fieldName,
                node_state: nodeState,
                graph_context: { predecessors, successors },
                template_id: _templateId,
            }),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || resp.statusText);
        }

        // Stream tokens into the field
        targetEl.value = "";
        targetEl.classList.add("pe-ai-generated");

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let accumulated = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value, { stream: true });
            // Strip SSE "data:" prefix lines
            for (const line of chunk.split("\n")) {
                if (line.startsWith("data: ")) {
                    const token = line.slice(6);
                    if (token === "[DONE]") break;
                    accumulated += token;
                    targetEl.value = accumulated;
                }
            }
        }

        if (!accumulated) targetEl.value = orig; // fallback

    } catch (e) {
        targetEl.value = orig;
        peToast(`⚡ Failed: ${e.message}`, "err");
    } finally {
        if (btn) btn.classList.remove("pe-generating");
    }
}

// ============================================================
// TIDY LAYOUT (topological sort → left-to-right positions)
// ============================================================

function tidyLayout() {
    if (!_graph) return;
    const nodes = _graph._nodes.filter(n => n.type === "maestro/stage");
    if (!nodes.length) return;

    // Build adjacency for topological sort (output → inputs)
    const nodeIds = new Set(nodes.map(n => n.id));
    const inDeg   = {};
    const adj     = {};  // nodeId → [successor nodeId]
    nodes.forEach(n => { inDeg[n.id] = 0; adj[n.id] = []; });

    // Count in-degrees from graph links
    for (const link of Object.values(_graph.links)) {
        if (nodeIds.has(link.origin_id) && nodeIds.has(link.target_id)) {
            if (!link._back_edge) {  // skip back-edges for layout
                adj[link.origin_id].push(link.target_id);
                inDeg[link.target_id] = (inDeg[link.target_id] || 0) + 1;
            }
        }
    }

    // Kahn's algorithm — assign each node to a column (depth level)
    const queue   = nodes.filter(n => (inDeg[n.id] || 0) === 0).map(n => n.id);
    const colOf   = {};  // nodeId → column index
    let   col = 0;
    const visited = new Set();

    while (queue.length) {
        const nextQueue = [];
        queue.forEach(id => {
            if (visited.has(id)) return;
            visited.add(id);
            colOf[id] = col;
            (adj[id] || []).forEach(succId => {
                inDeg[succId]--;
                if (inDeg[succId] === 0) nextQueue.push(succId);
            });
        });
        col++;
        queue.length = 0;
        queue.push(...nextQueue);
    }

    // Nodes not reached (in cycles) get appended after
    nodes.filter(n => !visited.has(n.id)).forEach(n => { colOf[n.id] = col++; });

    // Group by column, assign row positions
    const cols = {};
    nodes.forEach(n => {
        const c = colOf[n.id] ?? 0;
        if (!cols[c]) cols[c] = [];
        cols[c].push(n);
    });

    const COL_W = 280, ROW_H = 140, MARGIN_X = 80, MARGIN_Y = 80;
    Object.entries(cols).forEach(([c, colNodes]) => {
        const totalH = colNodes.length * ROW_H;
        const startY = MARGIN_Y - totalH / 2 + 200;
        colNodes.forEach((node, r) => {
            node.pos = [MARGIN_X + parseInt(c) * COL_W, startY + r * ROW_H];
        });
    });

    _canvas.setDirty(true, true);
    peToast("Layout applied", "ok");
}

// ============================================================
// SIMULATION (ghost token walk)
// ============================================================

function startSimulation() {
    if (!_graph) return;
    // Build ordered path using topological sort (pass condition only)
    const nodes   = _graph._nodes.filter(n => n.type === "maestro/stage");
    if (!nodes.length) { peToast("No stages to simulate", "err"); return; }

    // Find the start node: lowest DB position / leftmost canvas pos
    const sorted = [...nodes].sort((a, b) =>
        (_stagePosMap[a.id] ?? a.pos[0]) - (_stagePosMap[b.id] ?? b.pos[0])
    );

    _simPath = [];
    const visited = new Set();
    let current = sorted[0];

    while (current && !visited.has(current.id)) {
        _simPath.push(current.id);
        visited.add(current.id);
        // Follow the "pass" output
        const passSlot = (current.outputs || []).findIndex(o => o.name === "pass");
        const slot = passSlot !== -1 ? passSlot : 0;
        const out = current.outputs?.[slot];
        if (!out?.links?.length) break;
        const link = _graph.links[out.links[0]];
        if (!link) break;
        current = _graph.getNodeById(link.target_id);
    }

    if (!_simPath.length) { peToast("Could not build simulation path", "err"); return; }

    _simStep = 0;
    _simActive = true;

    document.getElementById("pe-sim-overlay").classList.remove("pe-hidden");
    _highlightSimNode();
}

function stopSimulation() {
    _simActive = false;
    _simPath = [];
    _simStep = 0;
    _clearSimHighlight();
    document.getElementById("pe-sim-overlay").classList.add("pe-hidden");
}

function stepSimulation() {
    if (!_simActive) return;
    _simStep++;
    if (_simStep >= _simPath.length) {
        peToast("Simulation complete — end of path", "ok");
        stopSimulation();
        return;
    }
    _highlightSimNode();
}

function _highlightSimNode() {
    _clearSimHighlight();
    const nodeId = _simPath[_simStep];
    const node = _graph.getNodeById(nodeId);
    if (!node) return;

    _simHighlightedId = nodeId;
    node._sim_highlight = true;
    document.getElementById("pe-sim-label").textContent =
        `Step ${_simStep + 1}/${_simPath.length} — ${node.title}`;

    // Pan canvas to show the current node
    _canvas.centerOnNode(node);
    _canvas.setDirty(true, true);
}

function _clearSimHighlight() {
    if (_simHighlightedId !== null) {
        const node = _graph.getNodeById(_simHighlightedId);
        if (node) node._sim_highlight = false;
        _simHighlightedId = null;
    }
}

// Patch node draw to show simulation highlight ring
const _origDrawNodeShape = LiteGraph.LGraphNode?.prototype?.drawNodeShape;

function patchSimulationDraw() {
    const _origOnDrawFG = LiteGraph.LGraphCanvas?.prototype?.drawNodeShape;
    // Patch via node's onDrawForeground hook instead
    // (simpler than patching canvas-level method)
    // Each node with _sim_highlight = true draws a gold ring
    const OrigStageOnDrawBG = StageNode.prototype.onDrawBackground;
    StageNode.prototype.onDrawBackground = function(ctx) {
        if (OrigStageOnDrawBG) OrigStageOnDrawBG.call(this, ctx);
        if (this._sim_highlight) {
            ctx.save();
            ctx.strokeStyle = "#f59e0b";
            ctx.lineWidth = 4;
            ctx.strokeRect(-2, -2, this.size[0] + 4, this.size[1] + 4);
            ctx.restore();
        }
    };
}

// ============================================================
// TOAST
// ============================================================

let _toastTimer = null;
function peToast(msg, type = "ok") {
    const el = document.getElementById("pe-toast");
    el.textContent = msg;
    el.className = `pe-toast pe-toast-${type}`;
    el.classList.remove("pe-hidden");
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.add("pe-hidden"), 3000);
}

// ============================================================
// EVENT WIRING
// ============================================================

// ============================================================
// TOOL GROUPS MODAL
// ============================================================

async function openToolGroupsModal() {
    _toolGroupingsCache = null; // force refresh
    const modal = document.getElementById("pe-tg-modal");
    modal.classList.remove("pe-hidden");
    await _renderToolGroupsList();
    document.getElementById("pe-tg-close").onclick = () => modal.classList.add("pe-hidden");
    document.getElementById("pe-tg-new").onclick = () => _showToolGroupForm(null);
}

async function _renderToolGroupsList() {
    const tgs = await _loadToolGroupings();
    const body = document.getElementById("pe-tg-body");
    if (!tgs.length) {
        body.innerHTML = `<p style="color:#64748b;font-size:13px;">No tool groupings yet.</p>`;
        return;
    }
    body.innerHTML = tgs.map(tg => `
        <div class="pe-tg-row" id="pe-tg-row-${tg.id}">
          <div class="pe-tg-row-info">
            <div class="pe-tg-row-name">${_peEscHtml(tg.name)}${tg.is_builtin ? '<span class="pe-tg-builtin-badge">builtin</span>' : ""}</div>
            <div class="pe-tg-row-desc">${_peEscHtml(tg.description || "")}</div>
            <div class="pe-tg-row-tools">${tg.tools.length} tool${tg.tools.length !== 1 ? "s" : ""}: ${_peEscHtml(tg.tools.slice(0, 5).join(", ") + (tg.tools.length > 5 ? "…" : ""))}</div>
          </div>
          <div class="pe-tg-row-actions">
            <button class="pe-btn pe-btn-secondary" style="font-size:12px;padding:3px 8px;"
              onclick="_showToolGroupForm(${tg.id})">Edit</button>
            <button class="pe-btn pe-btn-secondary" style="font-size:12px;padding:3px 8px;"
              onclick="_cloneToolGroup(${tg.id})">Clone</button>
            ${!tg.is_builtin ? `<button class="pe-btn pe-btn-secondary" style="font-size:12px;padding:3px 8px;color:#f87171;"
              onclick="_deleteToolGroup(${tg.id})">Del</button>` : ""}
          </div>
        </div>`).join("");
}

async function _showToolGroupForm(groupingId) {
    let tg = null;
    if (groupingId) {
        const tgs = await _loadToolGroupings();
        tg = tgs.find(g => g.id === groupingId);
    }
    const body = document.getElementById("pe-tg-body");
    const formHtml = `
        <div class="pe-tg-edit-form" id="pe-tg-edit-form">
          <div class="pe-field-row">
            <label>Name</label>
            <input type="text" id="pe-tg-f-name" class="pe-input" value="${_peEscHtml(tg?.name || "")}">
          </div>
          <div class="pe-field-row">
            <label>Description</label>
            <input type="text" id="pe-tg-f-desc" class="pe-input" value="${_peEscHtml(tg?.description || "")}">
          </div>
          <div class="pe-field-row">
            <label>Tools <span style="color:#94a3b8;font-size:11px;">(one per line)</span></label>
            <textarea id="pe-tg-f-tools" class="pe-input pe-textarea" rows="8">${_peEscHtml((tg?.tools || []).join("\n"))}</textarea>
          </div>
          <div style="display:flex;gap:8px;margin-top:4px;">
            <button class="pe-btn pe-btn-primary" onclick="_saveToolGroupForm(${groupingId || "null"})">Save</button>
            <button class="pe-btn pe-btn-secondary" onclick="_renderToolGroupsList()">Cancel</button>
          </div>
        </div>`;
    body.innerHTML = formHtml;
}

async function _saveToolGroupForm(groupingId) {
    const name  = document.getElementById("pe-tg-f-name")?.value?.trim();
    const desc  = document.getElementById("pe-tg-f-desc")?.value?.trim() || "";
    const tools = (document.getElementById("pe-tg-f-tools")?.value || "")
        .split("\n").map(s => s.trim()).filter(Boolean);
    if (!name) { peToast("Name is required", "err"); return; }
    try {
        if (groupingId) {
            await apiPut(`/tool-groupings/${groupingId}`, {name, description: desc, tools});
        } else {
            await apiPost(`/tool-groupings`, {name, description: desc, tools});
        }
        _toolGroupingsCache = null;
        peToast("Saved", "ok");
        await _renderToolGroupsList();
    } catch (e) {
        peToast(`Save failed: ${e.message}`, "err");
    }
}

async function _cloneToolGroup(groupingId) {
    const tgs = await _loadToolGroupings();
    const src = tgs.find(g => g.id === groupingId);
    if (!src) return;
    const name = prompt("Name for the cloned group:", `${src.name} (copy)`);
    if (!name?.trim()) return;
    try {
        await apiPost(`/tool-groupings`, {name: name.trim(), description: src.description || "", tools: src.tools});
        _toolGroupingsCache = null;
        peToast("Cloned", "ok");
        await _renderToolGroupsList();
    } catch (e) {
        peToast(`Clone failed: ${e.message}`, "err");
    }
}

async function _deleteToolGroup(groupingId) {
    if (!confirm("Delete this tool grouping?")) return;
    try {
        await apiDel(`/tool-groupings/${groupingId}`);
        _toolGroupingsCache = null;
        peToast("Deleted", "ok");
        await _renderToolGroupsList();
    } catch (e) {
        peToast(`Delete failed: ${e.message}`, "err");
    }
}


// ============================================================
// COLUMNS MODAL
// ============================================================

const PE_COL_PRESETS = [
    { key: "idea",         label: "Idea" },
    { key: "planning",     label: "Planning" },
    { key: "indev",        label: "In Development" },
    { key: "ai_review",    label: "AI Review" },
    { key: "human_review", label: "Human Review" },
    { key: "complete",     label: "Complete" },
];

function openColumnsModal() {
    document.getElementById("pe-columns-modal").classList.remove("pe-hidden");
    _renderColumnRows();
}

function closeColumnsModal() {
    document.getElementById("pe-columns-modal").classList.add("pe-hidden");
}

function _renderColumnRows() {
    const list = document.getElementById("pe-col-list");
    list.innerHTML = "";

    if (!_kanbanColumns.length) {
        list.innerHTML = '<div style="color:#64748b;font-size:12px;padding:8px 0">No columns defined. Add one below.</div>';
        return;
    }

    _kanbanColumns.forEach((col, i) => {
        const row = document.createElement("div");
        row.className = "pe-col-row";
        row.innerHTML = `
            <span class="pe-col-band-swatch" style="background:${i % 2 === 0 ? PE_BAND_EVEN : PE_BAND_ODD}"></span>
            <input class="pe-col-input pe-col-label" data-idx="${i}" value="${_escHtml(col.label)}" placeholder="Label" title="Column label" />
            <input class="pe-col-input pe-col-key" data-idx="${i}" value="${_escHtml(col.key)}" placeholder="key" title="Column key (used in stage config)" style="font-family:monospace;width:110px" />
            <input class="pe-col-input pe-col-xmin" data-idx="${i}" type="number" value="${col.x_min}" placeholder="X start" title="Left edge X in graph space" style="width:80px" />
            <button class="pe-col-del pe-btn pe-btn-danger-sm" data-idx="${i}" title="Remove column">✕</button>
        `;
        list.appendChild(row);
    });

    // Wire live-update events
    list.querySelectorAll(".pe-col-label").forEach(el => {
        el.addEventListener("input", e => {
            _kanbanColumns[+e.target.dataset.idx].label = e.target.value;
            _refreshBands();
        });
    });
    list.querySelectorAll(".pe-col-key").forEach(el => {
        el.addEventListener("input", e => {
            _kanbanColumns[+e.target.dataset.idx].key = e.target.value;
        });
    });
    list.querySelectorAll(".pe-col-xmin").forEach(el => {
        el.addEventListener("input", e => {
            const v = parseInt(e.target.value);
            if (!isNaN(v)) {
                _kanbanColumns[+e.target.dataset.idx].x_min = v;
                _kanbanColumns.sort((a, b) => a.x_min - b.x_min);
                _refreshBands();
                _renderColumnRows();
            }
        });
    });
    list.querySelectorAll(".pe-col-del").forEach(el => {
        el.addEventListener("click", e => {
            _kanbanColumns.splice(+e.target.dataset.idx, 1);
            _refreshBands();
            _renderColumnRows();
        });
    });
}

function _refreshBands() {
    renderColumnBandHeaders();
    if (_canvas) _canvas.setDirty(true, true);
}

function _escHtml(s) {
    return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function _addColumn() {
    const maxX = _kanbanColumns.reduce((m, c) => Math.max(m, c.x_min), 0);
    const idx = _kanbanColumns.length;
    _kanbanColumns.push({
        key:   `column_${idx}`,
        label: `Column ${idx + 1}`,
        x_min: maxX + 500,
    });
    _refreshBands();
    _renderColumnRows();
}

function _applyPreset() {
    const COL_W = 500;
    _kanbanColumns = PE_COL_PRESETS.map((p, i) => ({ ...p, x_min: i * COL_W }));
    _refreshBands();
    _renderColumnRows();
}

function setupEvents() {
    // Top bar buttons
    document.getElementById("btn-save").addEventListener("click", saveGraph);
    document.getElementById("btn-tidy").addEventListener("click", tidyLayout);
    document.getElementById("btn-columns").addEventListener("click", openColumnsModal);
    document.getElementById("btn-tool-groups").addEventListener("click", openToolGroupsModal);
    document.getElementById("btn-simulate").addEventListener("click", () => {
        if (_simActive) stopSimulation();
        else startSimulation();
    });

    // Simulation controls
    document.getElementById("pe-sim-step").addEventListener("click", stepSimulation);
    document.getElementById("pe-sim-stop").addEventListener("click", stopSimulation);

    // Add-node menu
    const addBtn  = document.getElementById("btn-add-node");
    const addMenu = document.getElementById("pe-add-menu");
    addBtn.addEventListener("click", e => {
        e.stopPropagation();
        addMenu.classList.toggle("pe-hidden");
    });
    document.addEventListener("click", () => addMenu.classList.add("pe-hidden"));

    document.querySelectorAll(".pe-add-item").forEach(item => {
        item.addEventListener("click", () => {
            const type = item.dataset.nodeType;
            const node = LiteGraph.createNode(type);
            // Place near center of current canvas view
            const cx = (_canvas.offset?.[0] || 0) + (_canvas.canvas.width  / 2) / _canvas.scale;
            const cy = (_canvas.offset?.[1] || 0) + (_canvas.canvas.height / 2) / _canvas.scale;
            node.pos = [cx - 110, cy - 40];
            _graph.add(node);
            _canvas.setDirty(true, true);
            addMenu.classList.add("pe-hidden");
        });
    });

    // Property panel actions
    document.getElementById("pe-panel-close").addEventListener("click", closePanel);
    document.getElementById("pe-save-node").addEventListener("click", applyPanel);
    document.getElementById("pe-revert-node").addEventListener("click", revertPanel);

    // Canvas double-click → open panel (also handled via onDblClick on each node)
    // Right-click context menu for edges (condition setting)
    _canvas.onShowLinkMenu = function(link, e) {
        const menu = new LiteGraph.ContextMenu([
            {
                content: "Set condition…",
                callback: () => {
                    const cond = prompt(
                        `Condition for this edge:\n(${PE_CONDITIONS.join(" / ")})`,
                        _getEdgeCondition(link) || "pass"
                    );
                    if (!cond) return;
                    // Change the output port name to the new condition
                    const fromNode = _graph.getNodeById(link.origin_id);
                    if (fromNode) {
                        const out = fromNode.outputs?.[link.origin_slot];
                        if (out) {
                            out.name = cond.trim().toLowerCase();
                            classifyBackEdges();
                            _canvas.setDirty(true, true);
                        }
                    }
                },
            },
            {
                content: "Delete edge",
                callback: () => { _graph.removeLink(link.id); classifyBackEdges(); },
            },
        ], { event: e });
        return false; // prevent default
    };

    // Node right-click → rename / delete / color
    _canvas.onShowNodeMenu = function(node, e) {
        if (node.type !== "maestro/stage") return;
        new LiteGraph.ContextMenu([
            {
                content: "Open properties",
                callback: () => openPanel(node),
            },
            {
                content: "Set color…",
                callback: () => {
                    const c = prompt("Node color (hex):", node.color || "#1e40af");
                    if (!c) return;
                    node.color   = c;
                    node.bgcolor = _darken(c);
                    node.properties.color = c;
                    _canvas.setDirty(true, true);
                },
            },
            {
                content: "Delete stage…",
                callback: () => _deleteNodeWithDialog(node),
            },
        ], { event: e });
    };

    // Mark graph dirty on any structural change
    _graph.onNodeAdded = () => setSaveStatus("Unsaved changes");
    _graph.onNodeRemoved = () => setSaveStatus("Unsaved changes");
    _graph.onConnectionChange = () => {
        classifyBackEdges();
        setSaveStatus("Unsaved changes");
    };

    // Resize canvas on window resize
    window.addEventListener("resize", resizeCanvas);
}

function _getEdgeCondition(link) {
    const node = _graph.getNodeById(link.origin_id);
    return node?.outputs?.[link.origin_slot]?.name || "pass";
}

async function _deleteNodeWithDialog(node) {
    const stageId  = node.properties.stage_id;
    const stageKey = node.properties.stage_key;

    if (!stageId) {
        // Not yet saved — just remove from canvas
        _graph.remove(node);
        return;
    }

    // Check if there are tasks in this stage (simplified: ask user for redirect)
    const redirect = prompt(
        `Delete stage "${stageKey}"?\n\n` +
        `If tasks exist in this stage they will be moved to another stage.\n` +
        `Enter the destination stage key, or leave blank to delete without redirect:`
    );

    if (redirect === null) return; // cancelled

    try {
        if (redirect.trim()) {
            await apiPost(`/pipelines/${_templateId}/stages/${stageId}/delete-with-redirect`, {
                redirect_stage_key: redirect.trim(),
            });
        } else {
            await apiDel(`/pipelines/${_templateId}/stages/${stageId}`);
        }
        _graph.remove(node);
        classifyBackEdges();
        peToast("Stage deleted", "ok");
    } catch (e) {
        peToast(`Delete failed: ${e.message}`, "err");
    }
}

// ============================================================
// CANVAS SETUP & RESIZE
// ============================================================

function resizeCanvas() {
    const wrap = document.getElementById("pe-canvas-wrap");
    const canvas = document.getElementById("pe-canvas");
    canvas.width  = wrap.clientWidth;
    canvas.height = wrap.clientHeight;
    if (_canvas) _canvas.setDirty(true, true);
}

// ============================================================
// INIT
// ============================================================

document.addEventListener("DOMContentLoaded", async function () {
    // Extract template ID from URL: /pipelines/{id}/edit
    const m = window.location.pathname.match(/\/pipelines\/(\d+)\/edit/);
    _templateId = m ? parseInt(m[1]) : null;

    // Register node types before creating the graph
    registerNodeTypes();
    patchSimulationDraw();

    // Create graph and canvas
    _graph  = new LGraph();
    const canvasEl = document.getElementById("pe-canvas");
    _canvas = new LGraphCanvas(canvasEl, _graph);

    // Canvas appearance
    _canvas.background_image = null;
    _canvas.render_canvas_border = false;
    _canvas.render_connections_border = false;
    _canvas.always_render_background = true;
    _canvas.background_color = "#1a1a2e";
    _canvas.node_title_color = "#f1f5f9";

    // Initial canvas size
    resizeCanvas();

    // Patch rendering
    patchLinkRendering();

    // Draw column bands behind graph content
    _canvas.onDrawBackground = function(ctx) { drawColumnBands(ctx); };

    // Draw back-edge overlay arrows above all LiteGraph content
    _canvas.onDrawForeground = function(ctx) { drawBackEdgeOverlays(ctx); };

    // Wire up events
    setupEvents();

    // Load agent types and trackable tools
    try {
        _agentTypes = await apiGet("/pipelines/agent-types");
    } catch (_) {
        _agentTypes = [];
    }
    try {
        _trackableTools = await apiGet("/pipelines/trackable-tools");
    } catch (_) {
        _trackableTools = { categories: {}, all: [] };
    }

    // Load template or start empty
    if (_templateId) {
        try {
            await loadTemplate();
        } catch (e) {
            peToast(`Failed to load template: ${e.message}`, "err");
            document.getElementById("pe-template-name").textContent = "Load failed";
        }
    } else {
        document.getElementById("pe-template-name").textContent = "New Pipeline";
        setSaveStatus("Unsaved");
    }

    // Start graph engine (required for litegraph to process events)
    _graph.start();
});

// ── Template Tags Modal ──────────────────────────────────────────────────────

function openTagsModal() {
    const tags = (_templateData && _templateData.tags) ? _templateData.tags : [];
    document.getElementById("pe-tags-input").value = tags.join(", ");
    const modal = document.getElementById("pe-tags-modal");
    modal.style.display = "flex";
}

function closeTagsModal() {
    document.getElementById("pe-tags-modal").style.display = "none";
}

async function saveTemplateTags() {
    if (!_templateId) { peToast("Save the template first before editing tags.", "warn"); return; }
    const raw = document.getElementById("pe-tags-input").value;
    const tags = raw.split(",").map(s => s.trim()).filter(Boolean);
    try {
        const updated = await apiPut(`/pipelines/${_templateId}`, { tags });
        if (_templateData) _templateData.tags = updated.tags || tags;
        closeTagsModal();
        peToast(`Tags saved: ${tags.length ? tags.join(", ") : "(none)"}`);
    } catch (e) {
        peToast(`Failed to save tags: ${e.message}`, "err");
    }
}
