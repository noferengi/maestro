"""
app/agent/static_analysis.py
-----------------------------
Deterministic static analysis of Python source files using tree-sitter.

This is Stage 2a of the intake pipeline — it runs BEFORE any LLM call so
the LLM has ground-truth structural data (classes, functions, imports,
dependency graph) to reason about.

Requires:
    tree-sitter >= 0.25.2
    tree-sitter-python >= 0.25.0
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from app.agent.config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# tree-sitter bootstrap
# ---------------------------------------------------------------------------

PY_LANGUAGE = Language(tspython.language())

def _make_parser() -> Parser:
    """Create a fresh tree-sitter parser for Python."""
    return Parser(PY_LANGUAGE)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ClassInfo:
    name: str
    methods: list[str]
    bases: list[str]
    line_start: int
    line_end: int


@dataclass
class FunctionInfo:
    name: str
    params: list[str]
    line_start: int
    line_end: int
    is_async: bool


@dataclass
class FileAnalysis:
    path: str
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    global_variables: list[str] = field(default_factory=list)


@dataclass
class ProjectAnalysis:
    files: dict[str, FileAnalysis] = field(default_factory=dict)
    import_graph: dict[str, list[str]] = field(default_factory=dict)
    reverse_import_graph: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers — tree-sitter node walking
# ---------------------------------------------------------------------------

def _child_by_type(node: Any, type_name: str) -> Any | None:
    """Return the first direct child with the given node type, or None."""
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _children_by_type(node: Any, type_name: str) -> list[Any]:
    """Return all direct children with the given node type."""
    return [c for c in node.children if c.type == type_name]


def _node_text(node: Any, source: bytes) -> str:
    """Extract the source text for a node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _extract_bases(node: Any, source: bytes) -> list[str]:
    """Extract base class names from an argument_list in a class definition."""
    arg_list = _child_by_type(node, "argument_list")
    if arg_list is None:
        return []
    bases: list[str] = []
    for child in arg_list.children:
        if child.type in ("identifier", "attribute"):
            bases.append(_node_text(child, source))
    return bases


def _extract_methods(class_body: Any, source: bytes) -> list[str]:
    """Extract method names from a class body node."""
    methods: list[str] = []
    for child in class_body.children:
        if child.type in ("function_definition", "decorated_definition"):
            func_node = child
            if child.type == "decorated_definition":
                func_node = _child_by_type(child, "function_definition")
                if func_node is None:
                    continue
            name_node = _child_by_type(func_node, "identifier")
            if name_node is not None:
                methods.append(_node_text(name_node, source))
    return methods


def _extract_params(node: Any, source: bytes) -> list[str]:
    """Extract parameter names from a function's parameters node."""
    params_node = _child_by_type(node, "parameters")
    if params_node is None:
        return []
    params: list[str] = []
    for child in params_node.children:
        if child.type == "identifier":
            params.append(_node_text(child, source))
        elif child.type in (
            "default_parameter",
            "typed_parameter",
            "typed_default_parameter",
        ):
            id_node = _child_by_type(child, "identifier")
            if id_node is not None:
                params.append(_node_text(id_node, source))
        elif child.type == "list_splat_pattern":
            id_node = _child_by_type(child, "identifier")
            if id_node is not None:
                params.append("*" + _node_text(id_node, source))
        elif child.type == "dictionary_splat_pattern":
            id_node = _child_by_type(child, "identifier")
            if id_node is not None:
                params.append("**" + _node_text(id_node, source))
    return params


def _extract_import_module(node: Any, source: bytes) -> list[str]:
    """Extract module name strings from import / from-import statements."""
    modules: list[str] = []
    if node.type == "import_statement":
        # import foo, bar, baz
        for child in node.children:
            if child.type == "dotted_name":
                modules.append(_node_text(child, source))
            elif child.type == "aliased_import":
                dotted = _child_by_type(child, "dotted_name")
                if dotted is not None:
                    modules.append(_node_text(dotted, source))
    elif node.type == "import_from_statement":
        # from foo.bar import X
        dotted = _child_by_type(node, "dotted_name")
        if dotted is not None:
            modules.append(_node_text(dotted, source))
        else:
            # Handle relative imports: from . import X  or  from .foo import X
            rel = _child_by_type(node, "relative_import")
            if rel is not None:
                modules.append(_node_text(rel, source))
    return modules


def _extract_global_variables(node: Any, source: bytes) -> list[str]:
    """Extract top-level variable assignment names from the module root."""
    names: list[str] = []
    for child in node.children:
        if child.type == "expression_statement":
            expr = child.children[0] if child.children else None
            if expr is not None and expr.type == "assignment":
                lhs = expr.children[0] if expr.children else None
                if lhs is not None:
                    if lhs.type == "identifier":
                        names.append(_node_text(lhs, source))
                    elif lhs.type == "pattern_list":
                        for sub in lhs.children:
                            if sub.type == "identifier":
                                names.append(_node_text(sub, source))
    return names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_file(file_path: str) -> FileAnalysis:
    """Parse a single file and return its structural analysis.

    - For ``.py`` files, uses tree-sitter to extract classes, functions,
      imports, and top-level variables.
    - For non-Python files, returns a minimal FileAnalysis (path only).
    - For files that do not exist, returns an empty FileAnalysis.
    """
    analysis = FileAnalysis(path=file_path)

    if not os.path.isfile(file_path):
        logger.debug("analyze_file: missing %s", file_path)
        return analysis

    if not file_path.endswith(".py"):
        # Non-Python file — just acknowledge it exists.
        return analysis

    try:
        raw = Path(file_path).read_bytes()
    except (OSError, PermissionError) as e:
        logger.warning("analyze_file: OSError reading %s: %s", file_path, e)
        return analysis

    parser = _make_parser()
    tree = parser.parse(raw)
    root = tree.root_node

    if root is None or root.child_count == 0:
        logger.warning("analyze_file: tree-sitter parse produced empty tree for %s", file_path)

    # --- classes ---
    for cls_node in _children_by_type(root, "class_definition"):
        name_node = _child_by_type(cls_node, "identifier")
        if name_node is None:
            continue
        name = _node_text(name_node, raw)
        bases = _extract_bases(cls_node, raw)
        body = _child_by_type(cls_node, "block")
        methods = _extract_methods(body, raw) if body else []
        analysis.classes.append(ClassInfo(
            name=name,
            methods=methods,
            bases=bases,
            line_start=cls_node.start_point[0] + 1,
            line_end=cls_node.end_point[0] + 1,
        ))

    # Also pick up classes inside decorated_definition at the top level
    for dec_node in _children_by_type(root, "decorated_definition"):
        cls_node = _child_by_type(dec_node, "class_definition")
        if cls_node is None:
            continue
        name_node = _child_by_type(cls_node, "identifier")
        if name_node is None:
            continue
        name = _node_text(name_node, raw)
        bases = _extract_bases(cls_node, raw)
        body = _child_by_type(cls_node, "block")
        methods = _extract_methods(body, raw) if body else []
        analysis.classes.append(ClassInfo(
            name=name,
            methods=methods,
            bases=bases,
            line_start=dec_node.start_point[0] + 1,
            line_end=dec_node.end_point[0] + 1,
        ))

    # --- top-level functions ---
    for func_node in _children_by_type(root, "function_definition"):
        name_node = _child_by_type(func_node, "identifier")
        if name_node is None:
            continue
        analysis.functions.append(FunctionInfo(
            name=_node_text(name_node, raw),
            params=_extract_params(func_node, raw),
            line_start=func_node.start_point[0] + 1,
            line_end=func_node.end_point[0] + 1,
            is_async=False,
        ))

    # Decorated top-level functions and async functions
    for dec_node in _children_by_type(root, "decorated_definition"):
        func_node = _child_by_type(dec_node, "function_definition")
        if func_node is None:
            continue
        name_node = _child_by_type(func_node, "identifier")
        if name_node is None:
            continue
        analysis.functions.append(FunctionInfo(
            name=_node_text(name_node, raw),
            params=_extract_params(func_node, raw),
            line_start=dec_node.start_point[0] + 1,
            line_end=dec_node.end_point[0] + 1,
            is_async=False,
        ))

    # Patch is_async: re-scan for top-level async functions.
    # In tree-sitter-python, async def produces a node whose first child
    # is the 'async' keyword (type == 'async').  We detect this.
    _patch_async_flags(root, raw, analysis)

    # --- imports ---
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            analysis.imports.extend(_extract_import_module(child, raw))

    # --- global variables ---
    analysis.global_variables = _extract_global_variables(root, raw)

    return analysis


def _patch_async_flags(root: Any, source: bytes, analysis: FileAnalysis) -> None:
    """Mark functions that are async in the analysis."""
    # Collect names of async functions from the AST.
    async_names: set[tuple[str, int]] = set()

    for child in root.children:
        # In tree-sitter-python 0.25, top-level async functions can appear
        # as either function_definition with an async child, or as a
        # separate node.  We check the source text as the most reliable way.
        if child.type == "function_definition":
            line_start = child.start_point[0] + 1
            line_text = source[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            )
            if line_text.lstrip().startswith("async "):
                name_node = _child_by_type(child, "identifier")
                if name_node:
                    async_names.add((_node_text(name_node, source), line_start))
        elif child.type == "decorated_definition":
            func_node = _child_by_type(child, "function_definition")
            if func_node is not None:
                line_start = child.start_point[0] + 1
                func_text = source[func_node.start_byte:func_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                if func_text.lstrip().startswith("async "):
                    name_node = _child_by_type(func_node, "identifier")
                    if name_node:
                        async_names.add((_node_text(name_node, source), line_start))

    for func in analysis.functions:
        if (func.name, func.line_start) in async_names:
            func.is_async = True


# ---------------------------------------------------------------------------
# Import graph resolution
# ---------------------------------------------------------------------------

def _resolve_import_to_file(
    module_name: str,
    project_root: str,
    known_files: set[str],
) -> str | None:
    """Try to resolve a dotted module name to a file path within the project.

    Returns the absolute path if found, otherwise None.
    """
    # Normalise the module name to a relative path.
    # e.g. "app.agent.config" -> "app/agent/config"
    rel = module_name.replace(".", os.sep)

    candidates = [
        os.path.join(project_root, rel + ".py"),
        os.path.join(project_root, rel, "__init__.py"),
    ]

    for candidate in candidates:
        normed = os.path.normpath(candidate)
        if normed in known_files:
            return normed

    return None


def analyze_project(file_paths: list[str]) -> ProjectAnalysis:
    """Analyse multiple files and build import / reverse-import graphs.

    Parameters
    ----------
    file_paths:
        Absolute paths of files to include in the analysis.

    Returns
    -------
    ProjectAnalysis with per-file analysis plus import graphs.
    """
    result = ProjectAnalysis()

    # Normalise all paths so lookups are consistent.
    normalised: list[str] = [os.path.normpath(p) for p in file_paths]
    known: set[str] = set(normalised)

    # 1. Analyse each file.
    for fpath in normalised:
        result.files[fpath] = analyze_file(fpath)

    # 2. Build the import graph.
    for fpath, fa in result.files.items():
        resolved: list[str] = []
        for mod in fa.imports:
            target = _resolve_import_to_file(mod, PROJECT_ROOT, known)
            if target is not None and target != fpath:
                resolved.append(target)
        result.import_graph[fpath] = sorted(set(resolved))

    # 3. Build the reverse import graph.
    reverse: dict[str, list[str]] = {fp: [] for fp in normalised}
    for fpath, targets in result.import_graph.items():
        for target in targets:
            reverse.setdefault(target, []).append(fpath)
    result.reverse_import_graph = {k: sorted(set(v)) for k, v in reverse.items()}

    return result


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _class_info_to_dict(ci: ClassInfo) -> dict[str, Any]:
    return {
        "name": ci.name,
        "methods": ci.methods,
        "bases": ci.bases,
        "line_start": ci.line_start,
        "line_end": ci.line_end,
    }


def _function_info_to_dict(fi: FunctionInfo) -> dict[str, Any]:
    return {
        "name": fi.name,
        "params": fi.params,
        "line_start": fi.line_start,
        "line_end": fi.line_end,
        "is_async": fi.is_async,
    }


def _file_analysis_to_dict(fa: FileAnalysis) -> dict[str, Any]:
    return {
        "path": fa.path,
        "classes": [_class_info_to_dict(c) for c in fa.classes],
        "functions": [_function_info_to_dict(f) for f in fa.functions],
        "imports": fa.imports,
        "global_variables": fa.global_variables,
    }


def analysis_to_dict(analysis: ProjectAnalysis) -> dict[str, Any]:
    """Convert a ProjectAnalysis to a JSON-serialisable dict."""
    return {
        "files": {
            path: _file_analysis_to_dict(fa)
            for path, fa in analysis.files.items()
        },
        "import_graph": analysis.import_graph,
        "reverse_import_graph": analysis.reverse_import_graph,
    }


# ---------------------------------------------------------------------------
# Deterministic vote generator
# ---------------------------------------------------------------------------

def _detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect cycles in the import graph using iterative DFS.

    Returns a list of cycles, each cycle being a list of file paths.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in graph}
    parent: dict[str, str | None] = {n: None for n in graph}
    cycles: list[list[str]] = []

    for start in graph:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                colour[node] = GREY
            neighbours = graph.get(node, [])
            if idx < len(neighbours):
                stack.append((node, idx + 1))
                neighbour = neighbours[idx]
                if neighbour not in colour:
                    continue
                if colour[neighbour] == GREY:
                    # Found a cycle — reconstruct it.
                    cycle = [neighbour, node]
                    p = parent.get(node)
                    while p is not None and p != neighbour:
                        cycle.append(p)
                        p = parent.get(p)
                    cycle.reverse()
                    cycles.append(cycle)
                elif colour[neighbour] == WHITE:
                    parent[neighbour] = node
                    stack.append((neighbour, 0))
            else:
                colour[node] = BLACK

    return cycles


def generate_vote(analysis: ProjectAnalysis, task_description: str) -> dict[str, Any]:
    """Produce a deterministic (non-LLM) vote on task feasibility.

    Checks:
    - Do all referenced files exist on disk?
    - Are there circular imports among the analysed files?
    - Are there any parse errors (files that couldn't be read)?

    Returns
    -------
    dict with keys ``verdict``, ``confidence``, ``justification``.
    """
    issues: list[str] = []

    # --- Check for missing files ---
    missing_files: list[str] = []
    for fpath, fa in analysis.files.items():
        if not os.path.isfile(fpath):
            missing_files.append(fpath)
    if missing_files:
        issues.append(
            f"{len(missing_files)} referenced file(s) do not exist: "
            + ", ".join(os.path.basename(f) for f in missing_files[:5])
        )

    # --- Check for empty analyses on .py files (likely parse failures) ---
    parse_errors: list[str] = []
    for fpath, fa in analysis.files.items():
        if fpath.endswith(".py") and os.path.isfile(fpath):
            if (
                not fa.classes
                and not fa.functions
                and not fa.imports
                and not fa.global_variables
            ):
                # Could be an empty __init__.py — only flag if file is > 0 bytes
                try:
                    if os.path.getsize(fpath) > 10:
                        parse_errors.append(fpath)
                except OSError:
                    parse_errors.append(fpath)
    if parse_errors:
        issues.append(
            f"{len(parse_errors)} file(s) produced empty analysis (possible parse errors): "
            + ", ".join(os.path.basename(f) for f in parse_errors[:5])
        )

    # --- Check for circular imports ---
    cycles = _detect_cycles(analysis.import_graph)
    if cycles:
        cycle_strs = []
        for cycle in cycles[:3]:
            short = [os.path.basename(p) for p in cycle]
            cycle_strs.append(" -> ".join(short))
        issues.append(
            f"{len(cycles)} circular import(s) detected: " + "; ".join(cycle_strs)
        )

    # --- Determine verdict + confidence ---
    if cycles:
        confidence = max(61, 75 - len(cycles) * 2)
        verdict = "NEEDS_RESEARCH"
    elif missing_files or parse_errors:
        severity = len(missing_files) + len(parse_errors)
        confidence = max(76, 91 - severity * 3)
        verdict = "POSSIBLE"
    else:
        total_files = len(analysis.files)
        confidence = min(98, 92 + total_files)
        verdict = "LIKELY"

    justification_parts = []
    if not issues:
        justification_parts.append(
            f"All {len(analysis.files)} file(s) parsed cleanly with no missing "
            "references or circular imports."
        )
    else:
        justification_parts.extend(issues)

    if task_description:
        justification_parts.append(f"Task: {task_description[:200]}")

    logger.info(
        "Static analysis vote: %s (confidence=%d) for task: %s",
        verdict, confidence, task_description[:80],
    )
    return {
        "verdict": verdict,
        "confidence": confidence,
        "justification": " | ".join(justification_parts),
    }
