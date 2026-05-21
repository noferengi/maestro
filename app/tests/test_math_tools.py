"""
Tests for math tooling: sandbox, run_sympy, verifiers, literature tools.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# sandbox.run_in_sandbox — unit tests (no real Docker required)
# ---------------------------------------------------------------------------

class TestRunInSandbox:
    def test_docker_unavailable_returns_error(self):
        with patch("app.agent.sandbox._is_docker_available", return_value=False):
            from app.agent.sandbox import run_in_sandbox
            result = run_in_sandbox("print(1)")
        assert result["ok"] is False
        assert "Docker" in result["error"]
        assert "error" in result

    def test_unknown_language_returns_error(self):
        with patch("app.agent.sandbox._is_docker_available", return_value=True):
            from app.agent.sandbox import run_in_sandbox
            result = run_in_sandbox("code", lang="brainfuck")
        assert result["ok"] is False
        assert "Unknown language" in result["error"]

    def test_timeout_sets_timed_out_flag(self):
        import subprocess

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=1)

        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.subprocess.Popen", return_value=mock_proc), \
             patch("app.agent.sandbox.subprocess.run"):  # suppress docker kill
            from app.agent.sandbox import run_in_sandbox
            result = run_in_sandbox("import time; time.sleep(999)", timeout=1)

        assert result["timed_out"] is True
        assert result["ok"] is False

    def test_successful_execution_returns_stdout(self):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0

        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.subprocess.Popen", return_value=mock_proc):
            from app.agent.sandbox import run_in_sandbox
            result = run_in_sandbox("print('hello')")

        assert result["ok"] is True
        assert result["stdout"] == "hello\n"
        assert result["stderr"] == ""
        assert result["timed_out"] is False

    def test_nonzero_exit_sets_ok_false(self):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"NameError: name 'x' is not defined\n")
        mock_proc.returncode = 1

        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.subprocess.Popen", return_value=mock_proc):
            from app.agent.sandbox import run_in_sandbox
            result = run_in_sandbox("print(x)")

        assert result["ok"] is False
        assert "NameError" in result["stderr"]


# ---------------------------------------------------------------------------
# run_sympy tool — unit tests
# ---------------------------------------------------------------------------

class TestRunSympyTool:
    def _call(self, code, timeout=120, sandbox_result=None):
        if sandbox_result is None:
            sandbox_result = {"ok": True, "stdout": "2\n", "stderr": "", "timed_out": False}
        with patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result) as mock_sandbox:
            from app.agent.tools import run_sympy
            result = run_sympy(code=code, timeout=timeout)
        return result, mock_sandbox

    def test_timeout_clamped_to_minimum(self):
        _, mock = self._call("print(1)", timeout=1)
        # The actual timeout passed to run_in_sandbox should be at least 10
        called_timeout = mock.call_args[1]["timeout"] if mock.call_args[1] else mock.call_args[0][2]
        assert called_timeout >= 10

    def test_timeout_clamped_to_maximum(self):
        _, mock = self._call("print(1)", timeout=99999)
        called_timeout = mock.call_args[1].get("timeout") or mock.call_args[0][2]
        assert called_timeout <= 600

    def test_stdout_truncated_at_8kib(self):
        big_out = "x" * 10000
        result, _ = self._call("...", sandbox_result={
            "ok": True, "stdout": big_out, "stderr": "", "timed_out": False
        })
        # Output string from run_sympy contains the truncated stdout section
        # find the stdout section length
        assert len(result) <= 8192 + 100  # 8192 content + small prefix overhead

    def test_stderr_truncated_at_8kib(self):
        big_err = "e" * 10000
        result, _ = self._call("...", sandbox_result={
            "ok": False, "stdout": "", "stderr": big_err, "timed_out": False
        })
        assert len(result) <= 8192 + 100

    def test_timed_out_flag_shown_in_output(self):
        result, _ = self._call("...", sandbox_result={
            "ok": False, "stdout": "", "stderr": "", "timed_out": True
        })
        assert "timed out" in result.lower()

    def test_docker_unavailable_shown_in_output(self):
        result, _ = self._call("...", sandbox_result={
            "ok": False, "error": "Docker is not available. Start Docker Desktop."
        })
        assert "Docker" in result

    def test_empty_output_returns_placeholder(self):
        result, _ = self._call("...", sandbox_result={
            "ok": True, "stdout": "", "stderr": "", "timed_out": False
        })
        assert result == "[no output]"


# ---------------------------------------------------------------------------
# Verifiers — unit tests
# ---------------------------------------------------------------------------

class TestLean4Verifier:
    def _make_task_content(self, content):
        mock_task = MagicMock()
        mock_task.content = content
        return mock_task

    def test_full_stderr_returned_in_log_on_failure(self, caplog):
        import logging
        task_content = {"lean4_proof": "theorem foo : 1 = 2 := by decide"}
        sandbox_result = {
            "ok": False,
            "stdout": "",
            "stderr": "main.lean:1:32: error: decide tactic failed\n",
            "timed_out": False,
        }
        with patch("app.agent.verifiers._get_task_content", return_value=task_content), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.verifiers import _run_lean4
            with caplog.at_level(logging.INFO, logger="app.agent.verifiers"):
                result = _run_lean4("task-1", {})
        assert result is False
        assert "decide tactic failed" in caplog.text

    def test_returns_true_on_successful_verification(self):
        task_content = {"lean4_proof": "theorem foo : 1 = 1 := rfl"}
        sandbox_result = {"ok": True, "stdout": "", "stderr": "", "timed_out": False}
        with patch("app.agent.verifiers._get_task_content", return_value=task_content), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.verifiers import _run_lean4
            assert _run_lean4("task-1", {}) is True

    def test_docker_unavailable_returns_false(self):
        task_content = {"lean4_proof": "theorem foo : 1 = 1 := rfl"}
        with patch("app.agent.verifiers._get_task_content", return_value=task_content), \
             patch("app.agent.sandbox.run_in_sandbox",
                   return_value={"ok": False, "error": "Docker is not available."}):
            from app.agent.verifiers import _run_lean4
            assert _run_lean4("task-1", {}) is False

    def test_missing_lean4_proof_returns_false(self):
        with patch("app.agent.verifiers._get_task_content", return_value={}):
            from app.agent.verifiers import _run_lean4
            assert _run_lean4("task-1", {}) is False


class TestSympyVerifier:
    def test_routes_through_sandbox_not_subprocess(self):
        task_content = {"sympy_proof_code": "assert 1 == 1"}
        sandbox_result = {"ok": True, "stdout": "", "stderr": "", "timed_out": False}
        with patch("app.agent.verifiers._get_task_content", return_value=task_content), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result) as mock_sandbox, \
             patch("app.agent.verifiers.subprocess.run") as mock_subprocess:
            from app.agent.verifiers import _run_sympy
            result = _run_sympy("task-1", {})
        mock_sandbox.assert_called_once()
        mock_subprocess.assert_not_called()
        assert result is True


# ---------------------------------------------------------------------------
# Literature tools — unit tests
# ---------------------------------------------------------------------------

class TestSearchArxiv:
    _SAMPLE_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2305.12345v1</id>
    <title>Bounded gaps between primes</title>
    <published>2013-05-01T00:00:00Z</published>
    <summary>This paper proves that there are infinitely many pairs of primes with gap below a fixed bound.</summary>
    <author><name>Yitang Zhang</name></author>
    <link href="http://arxiv.org/abs/2305.12345v1" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2305.12345v1" rel="related" type="application/pdf"/>
  </entry>
</feed>"""

    def test_parses_atom_xml_correctly(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._SAMPLE_ATOM
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            from app.agent.tools_math import search_arxiv
            raw = search_arxiv(query="bounded prime gaps", max_results=1)

        records = json.loads(raw)
        assert len(records) == 1
        r = records[0]
        assert r["id"] == "2305.12345"
        assert r["title"] == "Bounded gaps between primes"
        assert r["authors"] == ["Yitang Zhang"]
        assert r["year"] == 2013
        assert "infinitely many" in r["abstract"]
        assert r["url"] == "http://arxiv.org/abs/2305.12345v1"
        assert r["pdf"] == "http://arxiv.org/pdf/2305.12345v1"

    def test_max_results_honoured(self):
        # Two entries in the feed but max_results=1 is sent to the API; here
        # we test that the URL includes the correct parameter.
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            from app.agent.tools_math import search_arxiv
            search_arxiv(query="primes", max_results=3)
        call_url = mock_open.call_args[0][0]
        assert "max_results=3" in call_url

    def test_http_error_returns_json_error(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            from app.agent.tools_math import search_arxiv
            raw = search_arxiv(query="primes")
        data = json.loads(raw)
        assert "error" in data


class TestSearchOeis:
    _SAMPLE_JSON = json.dumps({
        "results": [{
            "number": 1359,
            "name": "Lesser of twin primes",
            "data": "3,5,11,17,29,41,59,71,101,107",
            "offset": "1,1",
            "formula": ["a(n) ~ 2*C2*n/log(n)^2"],
        }]
    }).encode()

    def test_parses_json_correctly(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._SAMPLE_JSON
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            from app.agent.tools_math import search_oeis
            raw = search_oeis(query="twin primes")

        records = json.loads(raw)
        assert len(records) == 1
        r = records[0]
        assert r["id"] == "A001359"
        assert r["name"] == "Lesser of twin primes"
        assert r["values"][:3] == [3, 5, 11]
        assert r["offset"] == "1,1"
        assert "2*C2*n" in r["formula"]
        assert "oeis.org/A001359" in r["url"]

    def test_graceful_on_missing_formula(self):
        data = json.dumps({"results": [{"number": 40, "name": "Primes", "data": "2,3,5"}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            from app.agent.tools_math import search_oeis
            raw = search_oeis(query="primes")

        records = json.loads(raw)
        assert records[0]["formula"] == ""

    def test_http_error_returns_json_error(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            from app.agent.tools_math import search_oeis
            raw = search_oeis(query="fibonacci")
        data = json.loads(raw)
        assert "error" in data


# ---------------------------------------------------------------------------
# search_mathlib — Gap 12 unit tests
# ---------------------------------------------------------------------------

class TestSearchMathlib:
    def test_static_index_returns_results_without_lake(self):
        # lake not available -> falls back to static index
        with patch("app.agent.tools_math.shutil.which", return_value=None):
            import importlib
            import app.agent.tools_math as tm
            # Reset cached index so it re-reads on next call
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            results = search_mathlib("prime gap sieve")
        assert len(results) >= 3
        for r in results:
            assert "name" in r
            assert "type" in r
            assert "module" in r
            assert "doc" in r

    def test_max_results_respected(self):
        with patch("app.agent.tools_math.shutil.which", return_value=None):
            import app.agent.tools_math as tm
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            results = search_mathlib("nat prime", max_results=2)
        assert len(results) <= 2

    def test_keyword_scoring_returns_prime_first(self):
        with patch("app.agent.tools_math.shutil.which", return_value=None):
            import app.agent.tools_math as tm
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            results = search_mathlib("Nat.Prime")
        assert len(results) >= 1
        assert "Prime" in results[0]["name"]

    def test_nonexistent_query_returns_empty(self):
        with patch("app.agent.tools_math.shutil.which", return_value=None):
            import app.agent.tools_math as tm
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            results = search_mathlib("xyzzy_nonexistent_theorem_99341")
        assert results == []

    def test_max_results_clamped_to_50(self):
        with patch("app.agent.tools_math.shutil.which", return_value=None):
            import app.agent.tools_math as tm
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            # 9999 > 50 cap — result set bounded by index size, not overflow
            results = search_mathlib("nat", max_results=9999)
        assert len(results) <= 50

    def test_live_path_used_when_lake_available(self):
        fake_output = "Nat.Prime : ℕ → Prop\n"
        mock_run = MagicMock()
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = fake_output
        mock_run.return_value.stderr = ""
        with patch("app.agent.tools_math.shutil.which", return_value="/usr/bin/lake"), \
             patch("app.agent.tools_math.subprocess.run", mock_run):
            import app.agent.tools_math as tm
            tm._mathlib_index = None
            from app.agent.tools_math import search_mathlib
            results = search_mathlib("Nat.Prime")
        assert len(results) == 1
        assert results[0]["name"] == "Nat.Prime"
        assert "ℕ → Prop" in results[0]["type"]


# ---------------------------------------------------------------------------
# get_lean4_proof_state — Gap 12 unit tests
# ---------------------------------------------------------------------------

class TestGetLean4ProofState:
    _LEAN_SRC = "theorem foo : True := by\n  sorry\n"

    def test_docker_unavailable_returns_error_dict(self):
        with patch("app.agent.sandbox._is_docker_available", return_value=False):
            from app.agent.sandbox import get_lean4_proof_state
            result = get_lean4_proof_state(self._LEAN_SRC, line=2)
        assert result["ok"] is False
        assert "Docker" in result["error"]
        assert result["goal"] is None
        assert result["hypotheses"] == []

    def test_sorry_goal_returned_from_driver_output(self):
        driver_json = json.dumps({
            "ok": True,
            "goal": "⊢ True",
            "hypotheses": [],
            "messages": [],
        })
        sandbox_result = {"ok": True, "stdout": driver_json + "\n", "stderr": "", "timed_out": False}
        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.sandbox import get_lean4_proof_state
            result = get_lean4_proof_state(self._LEAN_SRC, line=2)
        assert result["ok"] is True
        assert "⊢ True" in result["goal"]

    def test_malformed_driver_output_returns_error(self):
        sandbox_result = {"ok": True, "stdout": "not valid json\n", "stderr": "", "timed_out": False}
        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.sandbox import get_lean4_proof_state
            result = get_lean4_proof_state(self._LEAN_SRC, line=2)
        assert result["ok"] is False
        assert "error" in result

    def test_empty_sandbox_output_returns_error(self):
        sandbox_result = {"ok": False, "stdout": "", "stderr": "container crashed", "timed_out": False}
        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.sandbox import get_lean4_proof_state
            result = get_lean4_proof_state(self._LEAN_SRC, line=2)
        assert result["ok"] is False
        assert result["goal"] is None

    def test_col_defaults_to_zero(self):
        driver_json = json.dumps({"ok": True, "goal": "⊢ True", "hypotheses": [], "messages": []})
        sandbox_result = {"ok": True, "stdout": driver_json, "stderr": "", "timed_out": False}
        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.sandbox import get_lean4_proof_state
            # No col argument — should not raise
            result = get_lean4_proof_state(self._LEAN_SRC, line=2)
        assert "ok" in result

    def test_tool_dispatch_returns_valid_json(self):
        driver_json = json.dumps({"ok": True, "goal": "⊢ True", "hypotheses": [], "messages": []})
        sandbox_result = {"ok": True, "stdout": driver_json, "stderr": "", "timed_out": False}
        with patch("app.agent.sandbox._is_docker_available", return_value=True), \
             patch("app.agent.sandbox.run_in_sandbox", return_value=sandbox_result):
            from app.agent.tools import dispatch_tool
            raw = dispatch_tool("get_lean4_proof_state", {"lean_source": self._LEAN_SRC, "line": 2})
        parsed = json.loads(raw)
        assert "ok" in parsed


# ---------------------------------------------------------------------------
# Migration 0105 — unit tests (no DB required; tests the logic functions directly)
# ---------------------------------------------------------------------------

class TestMigration0105Logic:
    """Test the up/down logic against an in-memory mock connection."""

    def _make_conn(self, stage_configs: dict[str, dict]):
        """Build a mock psycopg2-style connection for a given set of stage configs."""
        # stage_configs: {stage_key: config_dict}
        template_row = [1]
        stage_rows = {
            k: [100 + i, json.dumps(v)]
            for i, (k, v) in enumerate(stage_configs.items())
        }
        updated: dict[int, str] = {}

        class MockCursor:
            def __init__(self):
                self._result = None

            def fetchone(self):
                return self._result

            def execute(self, sql, params=None):
                pass

        class MockConn:
            def __init__(self):
                self._updated = updated
                self._stage_rows = stage_rows

            def execute(self, sql, params=None):
                cursor = MockCursor()
                if params is None:
                    return cursor
                if "pipeline_templates" in sql and "SELECT" in sql:
                    cursor._result = tuple(template_row)
                elif "pipeline_stages" in sql and "SELECT" in sql:
                    key = params.get("key") if isinstance(params, dict) else params[1]
                    if key in self._stage_rows:
                        cursor._result = tuple(self._stage_rows[key])
                    else:
                        cursor._result = None
                elif "UPDATE pipeline_stages" in sql:
                    if isinstance(params, dict):
                        sid = params["sid"]
                        self._updated[sid] = json.loads(params["cfg"])["tool_allowlist"]
                    else:
                        sid = params[1]
                        self._updated[sid] = json.loads(params[0])["tool_allowlist"]
                return cursor

        return MockConn(), updated

    def test_up_adds_search_mathlib_to_literature_survey(self):
        import importlib, sys
        # Remove cached module to get fresh import
        for mod in list(sys.modules.keys()):
            if "0105" in mod:
                del sys.modules[mod]

        conn, updated = self._make_conn({
            "LITERATURE_SURVEY": {"tool_allowlist": ["search_arxiv", "submit_work"]},
            "PROBLEM_FORMALIZATION": {"tool_allowlist": ["run_sympy", "submit_work"]},
            "PROOF_STRATEGY": {"tool_allowlist": ["run_sympy", "submit_work"]},
            "PROOF_ATTEMPT": {"tool_allowlist": ["run_sympy", "submit_work"]},
        })
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "mig0105",
            pathlib.Path("app/migrations/versions/0105_math_tool_allowlists.py"),
        )
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        mig.up(conn)

        # LITERATURE_SURVEY sid = 100
        assert "search_mathlib" in updated[100]

    def test_up_adds_both_tools_to_proof_attempt(self):
        conn, updated = self._make_conn({
            "LITERATURE_SURVEY": {"tool_allowlist": []},
            "PROBLEM_FORMALIZATION": {"tool_allowlist": []},
            "PROOF_STRATEGY": {"tool_allowlist": []},
            "PROOF_ATTEMPT": {"tool_allowlist": ["run_sympy"]},
        })
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "mig0105b",
            pathlib.Path("app/migrations/versions/0105_math_tool_allowlists.py"),
        )
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        mig.up(conn)

        # PROOF_ATTEMPT sid = 103
        assert "search_mathlib" in updated[103]
        assert "get_lean4_proof_state" in updated[103]

    def test_up_is_idempotent(self):
        initial = {"tool_allowlist": ["search_mathlib", "get_lean4_proof_state", "run_sympy"]}
        conn, updated = self._make_conn({
            "LITERATURE_SURVEY": {"tool_allowlist": ["search_mathlib"]},
            "PROBLEM_FORMALIZATION": {"tool_allowlist": ["search_mathlib"]},
            "PROOF_STRATEGY": {"tool_allowlist": ["search_mathlib"]},
            "PROOF_ATTEMPT": initial,
        })
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "mig0105c",
            pathlib.Path("app/migrations/versions/0105_math_tool_allowlists.py"),
        )
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        mig.up(conn)

        # PROOF_ATTEMPT sid = 103 — tools should appear exactly once
        proof_tools = updated[103]
        assert proof_tools.count("search_mathlib") == 1
        assert proof_tools.count("get_lean4_proof_state") == 1
