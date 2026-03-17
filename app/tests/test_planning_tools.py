"""
Tests for the new planning tools: generate_architecture_doc,
generate_mermaid_diagram, generate_interface_contract, spawn_research_agent.
"""

import json
import pytest
from app.agent.tools import (
    generate_architecture_doc,
    generate_mermaid_diagram,
    generate_interface_contract,
    spawn_research_agent,
    dispatch_tool,
)


class TestGenerateArchitectureDoc:
    """generate_architecture_doc returns valid markdown."""

    def test_basic_doc(self):
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
        assert "# Architecture: My System" in result
        assert "### API" in result
        assert "### DB" in result
        assert "**Technology:** FastAPI" in result
        assert "API" in result and "DB" in result
        assert "--queries-->" in result

    def test_string_components(self):
        result = generate_architecture_doc(
            title="Simple",
            components=["Frontend", "Backend"],
            relationships=["Frontend -> Backend"],
        )
        assert "# Architecture: Simple" in result
        assert "- Frontend" in result
        assert "- Backend" in result

    def test_empty_components(self):
        result = generate_architecture_doc(
            title="Empty",
            components=[],
            relationships=[],
        )
        assert "# Architecture: Empty" in result

    def test_dispatch(self):
        result = dispatch_tool("generate_architecture_doc", {
            "title": "Test",
            "components": ["A"],
            "relationships": [],
        })
        assert "# Architecture: Test" in result


class TestGenerateMermaidDiagram:
    """generate_mermaid_diagram validates types and returns formatted markup."""

    def test_flowchart(self):
        result = generate_mermaid_diagram("flowchart", "A --> B --> C")
        assert "```mermaid" in result
        assert "flowchart" in result
        assert "A --> B --> C" in result
        assert "```" in result

    def test_sequence(self):
        result = generate_mermaid_diagram("sequence", "Alice->>Bob: Hello")
        assert "sequenceDiagram" in result

    def test_class_diagram(self):
        result = generate_mermaid_diagram("class", "class Animal")
        assert "classDiagram" in result

    def test_er_diagram(self):
        result = generate_mermaid_diagram("er", "CUSTOMER ||--o{ ORDER : places")
        assert "erDiagram" in result

    def test_invalid_type(self):
        result = generate_mermaid_diagram("invalid_type", "A -> B")
        assert "ERROR" in result
        assert "Invalid diagram type" in result

    def test_already_has_directive(self):
        result = generate_mermaid_diagram("flowchart", "flowchart LR\n  A --> B")
        assert "```mermaid" in result
        assert result.count("flowchart") == 1  # should not duplicate

    def test_dispatch(self):
        result = dispatch_tool("generate_mermaid_diagram", {
            "diagram_type": "flowchart",
            "definition": "X --> Y",
        })
        assert "```mermaid" in result


class TestGenerateInterfaceContract:
    """generate_interface_contract produces structured JSON output."""

    def test_basic_contract(self):
        result = generate_interface_contract(
            component_name="AuthService",
            provides=[
                {"name": "authenticate", "type": "function", "description": "Validates credentials"},
            ],
            consumes=[
                {"name": "UserStore", "type": "database", "source": "sub-0"},
            ],
        )
        data = json.loads(result)
        assert data["component"] == "AuthService"
        assert len(data["provides"]) == 1
        assert data["provides"][0]["name"] == "authenticate"
        assert len(data["consumes"]) == 1
        assert data["consumes"][0]["source"] == "sub-0"

    def test_string_items(self):
        result = generate_interface_contract(
            component_name="Simple",
            provides=["endpoint_a", "endpoint_b"],
            consumes=["database"],
        )
        data = json.loads(result)
        assert len(data["provides"]) == 2
        assert data["provides"][0]["name"] == "endpoint_a"
        assert data["consumes"][0]["name"] == "database"

    def test_empty_lists(self):
        result = generate_interface_contract(
            component_name="Standalone",
            provides=[],
            consumes=[],
        )
        data = json.loads(result)
        assert data["component"] == "Standalone"
        assert data["provides"] == []
        assert data["consumes"] == []

    def test_dispatch(self):
        result = dispatch_tool("generate_interface_contract", {
            "component_name": "Test",
            "provides": ["x"],
            "consumes": [],
        })
        data = json.loads(result)
        assert data["component"] == "Test"


class TestSpawnResearchAgent:
    """spawn_research_agent sync placeholder returns error."""

    def test_sync_dispatch_returns_error(self):
        result = spawn_research_agent(question="What is Meshtastic?")
        assert "ERROR" in result
        assert "async" in result.lower()

    def test_dispatch_returns_error(self):
        result = dispatch_tool("spawn_research_agent", {"question": "test"})
        assert "ERROR" in result
