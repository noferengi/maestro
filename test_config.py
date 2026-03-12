"""Tests for config.py"""

import os
import unittest

import config


class TestConfig(unittest.TestCase):
    """Test cases for configuration management."""

    def test_app_id_exists(self):
        """Test that app ID is accessible via get_app_id()."""
        self.assertTrue(hasattr(config, "get_app_id"))
        self.assertEqual(config.get_app_id(), "maestro-orchestrator")

    def test_endpoints_class_exists(self):
        """Test that Endpoints class is defined."""
        self.assertTrue(hasattr(config, "Endpoints"))

    def test_endpoints_llm_api_base_default(self):
        """Test default LLM API base URL."""
        endpoints = config.Endpoints()
        self.assertEqual(endpoints.LLM_API_BASE, "http://localhost:8080/v1")

    def test_endpoints_llm_api_key_default(self):
        """Test default empty API key."""
        endpoints = config.Endpoints()
        self.assertEqual(endpoints.LLM_API_KEY, "")

    def test_endpoints_llm_api_base_env_override(self):
        """Test LLM API base URL from environment variable."""
        os.environ["LLM_API_BASE"] = "https://api.example.com/v1"
        try:
            endpoints = config.Endpoints()
            expected = "https://api.example.com/v1"
            self.assertEqual(endpoints.LLM_API_BASE, expected)
        finally:
            os.environ.pop("LLM_API_BASE", None)

    def test_endpoints_llm_api_key_env_override(self):
        """Test API key from environment variable."""
        os.environ["LLM_API_KEY"] = "test-key-123"
        try:
            endpoints = config.Endpoints()
            expected = "test-key-123"
            self.assertEqual(endpoints.LLM_API_KEY, expected)
        finally:
            os.environ.pop("LLM_API_KEY", None)

    def test_project_constants_class_exists(self):
        """Test that ProjectConstants class is defined."""
        self.assertTrue(hasattr(config, "ProjectConstants"))

    def test_project_constants_max_context_tokens(self):
        """Test maximum context tokens constant."""
        self.assertEqual(config.ProjectConstants.MAX_CONTEXT_TOKENS, 100_000)

    def test_project_constants_max_failure_retries(self):
        """Test maximum failure retries constant."""
        expected = 3
        self.assertEqual(config.ProjectConstants.MAX_FAILURE_RETRIES, expected)

    def test_project_constants_default_model(self):
        """Test default model constant."""
        expected = "Qwen-3-Coder-80B"
        self.assertEqual(config.ProjectConstants.DEFAULT_MODEL, expected)


if __name__ == "__main__":
    unittest.main()
