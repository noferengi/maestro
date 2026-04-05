"""
Tests for the planning tools: generate_architecture_doc,
generate_mermaid_diagram, generate_interface_contract, spawn_research_agent.

These tools now write to disk (.maestro/ subdirectory) rather than returning
full content into the prompt, so tests use tmp_path + a git repo for isolation.
"""

import json
import os
import subprocess
import pytest
from app.agent.tools import (
    generate_architecture_doc,
    generate_mermaid_diagram,
    generate_interface_contract,
    spawn_research_agent,
    dispatch_tool,
    _task_git_cwd,
)


@pytest.fixture(scope="module", autouse=True)
def isolated_project(tmp_path_factory):
    """
    Redirect all tool file I/O to a temp git repo.
    Runs once per module - git init is a subprocess and ~50ms per call,
    so amortising it across all tests in the file saves ~400ms.
    """
    tmp_path = tmp_path_factory.mktemp("planning_tools_project")
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True,
    )
    token = _task_git_cwd.set(str(tmp_path))
    yield tmp_path
    _task_git_cwd.reset(token)


class TestGenerateArchitectureDoc:
    """generate_architecture_doc writes to .maestro/architecture.md and returns a stub."""

    def test_basic_doc(self, isolated_project):
        result = generate_architecture_doc(
            title="My System",
            components=[
                {"name": "API", "description": "REST API layer", "technology": "FastAPI"},
                {"name": "DB", "description": "Database layer", "technology": "SQLite"},
            ],
            relationships=[
                {"from": "API", "to": "DB", "label": "queries"},
            ],
        )
        assert result.startswith("OK:")
        assert ".maestro/architecture.md" in result
        assert "2 components" in result
        assert "1 relationships" in result

        # Verify content on disk
        doc_path = isolated_project / ".maestro" / "architecture.md"
        assert doc_path.exists()
        content = doc_path.read_text(encoding="utf-8")
        assert "# Architecture: My System" in content
        assert "### API" in content
        assert "### DB" in content
        assert "**Technology:** FastAPI" in content
        assert "--queries-->" in content

    def test_string_components(self, isolated_project):
        result = generate_architecture_doc(
            title="Simple",
            components=["Frontend", "Backend"],
            relationships=["Frontend -> Backend"],
        )
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "architecture.md").read_text()
        assert "# Architecture: Simple" in content
        assert "- Frontend" in content
        assert "- Backend" in content

    def test_empty_components(self, isolated_project):
        result = generate_architecture_doc(
            title="Empty",
            components=[],
            relationships=[],
        )
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "architecture.md").read_text()
        assert "# Architecture: Empty" in content

    def test_dispatch(self, isolated_project):
        result = dispatch_tool("generate_architecture_doc", {
            "title": "Test",
            "components": ["A"],
            "relationships": [],
        })
        assert result.startswith("OK:")
        assert ".maestro/architecture.md" in result


class TestGenerateMermaidDiagram:
    """generate_mermaid_diagram writes to .maestro/diagrams/ and returns a stub."""

    def test_flowchart(self, isolated_project):
        result = generate_mermaid_diagram("flowchart", "A --> B --> C")
        assert result.startswith("OK:")
        assert ".maestro/diagrams/flowchart.md" in result

        content = (isolated_project / ".maestro" / "diagrams" / "flowchart.md").read_text()
        assert "```mermaid" in content
        assert "flowchart" in content
        assert "A --> B --> C" in content

    def test_sequence(self, isolated_project):
        result = generate_mermaid_diagram("sequence", "Alice->>Bob: Hello")
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "diagrams" / "sequence.md").read_text()
        assert "sequenceDiagram" in content

    def test_class_diagram(self, isolated_project):
        result = generate_mermaid_diagram("class", "class Animal")
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "diagrams" / "class.md").read_text()
        assert "classDiagram" in content

    def test_er_diagram(self, isolated_project):
        result = generate_mermaid_diagram("er", "CUSTOMER ||--o{ ORDER : places")
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "diagrams" / "er.md").read_text()
        assert "erDiagram" in content

    def test_invalid_type(self, isolated_project):
        result = generate_mermaid_diagram("invalid_type", "A -> B")
        assert "ERROR" in result
        assert "Invalid diagram type" in result

    def test_already_has_directive(self, isolated_project):
        result = generate_mermaid_diagram("flowchart", "flowchart LR\n  A --> B")
        assert result.startswith("OK:")
        content = (isolated_project / ".maestro" / "diagrams" / "flowchart.md").read_text()
        assert "```mermaid" in content
        assert content.count("flowchart") == 1  # no duplicate directive

    def test_dispatch(self, isolated_project):
        result = dispatch_tool("generate_mermaid_diagram", {
            "diagram_type": "flowchart",
            "definition": "X --> Y",
        })
        assert result.startswith("OK:")
        assert ".maestro/diagrams/flowchart.md" in result


class TestGenerateInterfaceContract:
    """generate_interface_contract writes to .maestro/contracts/ and returns a stub."""

    def test_basic_contract(self, isolated_project):
        result = generate_interface_contract(
            component_name="AuthService",
            provides=[
                {"name": "authenticate", "type": "function", "description": "Validates credentials"},
            ],
            consumes=[
                {"name": "UserStore", "type": "database", "source": "sub-0"},
            ],
        )
        assert result.startswith("OK:")
        assert "AuthService" in result
        assert "1 provides" in result
        assert "1 consumes" in result

        contract_path = isolated_project / ".maestro" / "contracts" / "AuthService.json"
        assert contract_path.exists()
        data = json.loads(contract_path.read_text())
        assert data["component"] == "AuthService"
        assert data["provides"][0]["name"] == "authenticate"
        assert data["consumes"][0]["source"] == "sub-0"

    def test_string_items(self, isolated_project):
        result = generate_interface_contract(
            component_name="Simple",
            provides=["endpoint_a", "endpoint_b"],
            consumes=["database"],
        )
        assert result.startswith("OK:")
        data = json.loads(
            (isolated_project / ".maestro" / "contracts" / "Simple.json").read_text()
        )
        assert len(data["provides"]) == 2
        assert data["provides"][0]["name"] == "endpoint_a"
        assert data["consumes"][0]["name"] == "database"

    def test_empty_lists(self, isolated_project):
        result = generate_interface_contract(
            component_name="Standalone",
            provides=[],
            consumes=[],
        )
        assert result.startswith("OK:")
        data = json.loads(
            (isolated_project / ".maestro" / "contracts" / "Standalone.json").read_text()
        )
        assert data["component"] == "Standalone"
        assert data["provides"] == []
        assert data["consumes"] == []

    def test_dispatch(self, isolated_project):
        result = dispatch_tool("generate_interface_contract", {
            "component_name": "Test",
            "provides": ["x"],
            "consumes": [],
        })
        assert result.startswith("OK:")
        assert "Test" in result


class TestSpawnResearchAgent:
    """spawn_research_agent sync placeholder returns error."""

    def test_sync_dispatch_returns_error(self):
        result = spawn_research_agent(question="What is Meshtastic?")
        assert "ERROR" in result
        assert "async" in result.lower()

    def test_dispatch_returns_error(self):
        result = dispatch_tool("spawn_research_agent", {"question": "test"})
        assert "ERROR" in result
