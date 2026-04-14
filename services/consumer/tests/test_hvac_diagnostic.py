"""
Tests for hvac_diagnostic.py

All tests mock google.generativeai completely — no real API calls are made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure services/consumer is importable
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Pre-register google.generativeai as a mock so the import in hvac_diagnostic
# doesn't fail even if the package isn't installed in the test environment.
_genai_mock = types.ModuleType("google.generativeai")
_google_mock = types.ModuleType("google")
_google_mock.generativeai = _genai_mock  # type: ignore[attr-defined]
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.generativeai", _genai_mock)

from hvac_diagnostic import (  # noqa: E402
    DEFAULT_QUESTION,
    GEMINI_MODEL,
    SYSTEM_PROMPT,
    build_diagnostic_prompt,
    run_diagnostic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_CONTEXT = "=== HomeOps HVAC Context Summary ===\nFurnace: OFF\nFloor 1: 68°F"
FAKE_QUESTION = "Why did floor 2 run so long today?"


def _make_args(
    question=None,
    hours=48,
    state="state/consumer/state.json",
    events="state/consumer/events.jsonl",
    json_mode=False,
):
    """Build a mock argparse.Namespace for run_diagnostic()."""
    ns = argparse.Namespace(
        question=question,
        hours=hours,
        state=state,
        events=events,
    )
    setattr(ns, "json", json_mode)
    return ns


# ---------------------------------------------------------------------------
# build_diagnostic_prompt tests
# ---------------------------------------------------------------------------


class TestBuildDiagnosticPrompt:
    def test_system_prompt_included(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        assert SYSTEM_PROMPT in prompt

    def test_context_included(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        assert FAKE_CONTEXT in prompt

    def test_question_included(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        assert FAKE_QUESTION in prompt

    def test_context_appears_after_system_prompt(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        sys_idx = prompt.index(SYSTEM_PROMPT)
        ctx_idx = prompt.index(FAKE_CONTEXT)
        assert ctx_idx > sys_idx

    def test_question_appears_after_context(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        ctx_idx = prompt.index(FAKE_CONTEXT)
        q_idx = prompt.index(FAKE_QUESTION)
        assert q_idx > ctx_idx

    def test_json_mode_adds_json_instruction(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=True)
        assert "JSON" in prompt
        assert '"summary"' in prompt
        assert '"anomalies"' in prompt
        assert '"recommendation"' in prompt

    def test_non_json_mode_no_json_instruction(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        # Should not contain the JSON key instructions
        assert '"anomalies"' not in prompt

    def test_context_section_markers_present(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, FAKE_QUESTION, json_mode=False)
        assert "=== HVAC Context Data ===" in prompt
        assert "=== End Context ===" in prompt


# ---------------------------------------------------------------------------
# Default question tests
# ---------------------------------------------------------------------------


class TestDefaultQuestion:
    def test_default_question_constant_is_set(self):
        assert DEFAULT_QUESTION
        assert len(DEFAULT_QUESTION) > 10

    def test_default_question_used_when_no_question_provided(self):
        prompt = build_diagnostic_prompt(FAKE_CONTEXT, DEFAULT_QUESTION, json_mode=False)
        assert DEFAULT_QUESTION in prompt

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_run_diagnostic_uses_default_question_when_none(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="All looks fine.")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(question=None)
        run_diagnostic(args)

        call_args = mock_model.generate_content.call_args[0][0]
        assert DEFAULT_QUESTION in call_args

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_run_diagnostic_uses_custom_question(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="Custom answer.")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(question=FAKE_QUESTION)
        run_diagnostic(args)

        call_args = mock_model.generate_content.call_args[0][0]
        assert FAKE_QUESTION in call_args


# ---------------------------------------------------------------------------
# JSON mode tests
# ---------------------------------------------------------------------------

VALID_JSON_RESPONSE = json.dumps(
    {
        "summary": "System running normally.",
        "anomalies": ["Floor 2 ran 45 min — above threshold"],
        "recommendation": "Check floor 2 damper.",
    }
)

INVALID_JSON_RESPONSE = "This is not JSON at all, just plain text."

FENCED_JSON_RESPONSE = (
    "```json\n"
    + json.dumps(
        {
            "summary": "Fenced summary.",
            "anomalies": [],
            "recommendation": "No action needed.",
        }
    )
    + "\n```"
)


class TestJsonMode:
    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_valid_json_parsed_correctly(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=VALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        run_diagnostic(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["summary"] == "System running normally."
        assert output["anomalies"] == ["Floor 2 ran 45 min — above threshold"]
        assert output["recommendation"] == "Check floor 2 damper."

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_json_output_includes_context_chars(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=VALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        run_diagnostic(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["context_chars"] == len(FAKE_CONTEXT)

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_invalid_json_exits_with_error(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=INVALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        with pytest.raises(SystemExit) as exc_info:
            run_diagnostic(args)
        assert exc_info.value.code == 1

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_invalid_json_prints_error_to_stderr(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=INVALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        with pytest.raises(SystemExit):
            run_diagnostic(args)
        captured = capsys.readouterr()
        assert "invalid JSON" in captured.err or "Error" in captured.err

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_fenced_json_parsed_correctly(self, mock_genai, mock_bc, monkeypatch, capsys):
        """LLM sometimes wraps JSON in ```json ... ``` — should still parse."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=FENCED_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        run_diagnostic(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["summary"] == "Fenced summary."
        assert output["anomalies"] == []

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_json_mode_output_has_all_required_keys(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=VALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        run_diagnostic(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        for key in ("summary", "anomalies", "recommendation", "context_chars"):
            assert key in output, f"Missing key: {key}"

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_context_chars_matches_len_context(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text=VALID_JSON_RESPONSE)
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(json_mode=True)
        run_diagnostic(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        # build_context is mocked to return FAKE_CONTEXT
        assert output["context_chars"] == len(FAKE_CONTEXT)


# ---------------------------------------------------------------------------
# GEMINI_API_KEY missing tests
# ---------------------------------------------------------------------------


class TestApiKeyMissing:
    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_missing_api_key_exits_with_code_1(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        args = _make_args()
        with pytest.raises(SystemExit) as exc_info:
            run_diagnostic(args)
        assert exc_info.value.code == 1

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_missing_api_key_prints_clear_error(self, mock_genai, mock_bc, monkeypatch, capsys):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        args = _make_args()
        with pytest.raises(SystemExit):
            run_diagnostic(args)
        captured = capsys.readouterr()
        assert "GEMINI_API_KEY" in captured.err

    @patch("hvac_diagnostic.build_context", return_value=FAKE_CONTEXT)
    @patch("hvac_diagnostic.genai")
    def test_missing_api_key_does_not_call_genai(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        args = _make_args()
        with pytest.raises(SystemExit):
            run_diagnostic(args)
        mock_genai.configure.assert_not_called()
        mock_genai.GenerativeModel.assert_not_called()


# ---------------------------------------------------------------------------
# Pass-through argument tests (--hours, --state, --events)
# ---------------------------------------------------------------------------


class TestArgumentPassthrough:
    @patch("hvac_diagnostic.build_context")
    @patch("hvac_diagnostic.genai")
    def test_custom_hours_passed_to_build_context(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_bc.return_value = FAKE_CONTEXT
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="OK")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(hours=12)
        run_diagnostic(args)

        mock_bc.assert_called_once_with(
            state_path="state/consumer/state.json",
            events_path="state/consumer/events.jsonl",
            lookback_hours=12,
        )

    @patch("hvac_diagnostic.build_context")
    @patch("hvac_diagnostic.genai")
    def test_custom_state_path_passed_to_build_context(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_bc.return_value = FAKE_CONTEXT
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="OK")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(state="/tmp/custom_state.json")
        run_diagnostic(args)

        mock_bc.assert_called_once_with(
            state_path="/tmp/custom_state.json",
            events_path="state/consumer/events.jsonl",
            lookback_hours=48,
        )

    @patch("hvac_diagnostic.build_context")
    @patch("hvac_diagnostic.genai")
    def test_custom_events_path_passed_to_build_context(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_bc.return_value = FAKE_CONTEXT
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="OK")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args(events="/tmp/custom_events.jsonl")
        run_diagnostic(args)

        mock_bc.assert_called_once_with(
            state_path="state/consumer/state.json",
            events_path="/tmp/custom_events.jsonl",
            lookback_hours=48,
        )

    @patch("hvac_diagnostic.build_context")
    @patch("hvac_diagnostic.genai")
    def test_gemini_model_name_is_correct(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_bc.return_value = FAKE_CONTEXT
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="OK")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args()
        run_diagnostic(args)

        mock_genai.GenerativeModel.assert_called_once_with(GEMINI_MODEL)

    @patch("hvac_diagnostic.build_context")
    @patch("hvac_diagnostic.genai")
    def test_genai_configured_with_api_key(self, mock_genai, mock_bc, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "my-secret-key")
        mock_bc.return_value = FAKE_CONTEXT
        mock_model = MagicMock()
        mock_model.generate_content.return_value = MagicMock(text="OK")
        mock_genai.GenerativeModel.return_value = mock_model

        args = _make_args()
        run_diagnostic(args)

        mock_genai.configure.assert_called_once_with(api_key="my-secret-key")
