#!/usr/bin/env python3
"""
On-demand LLM HVAC diagnostic.

Calls build_context() to get structured HVAC data, sends it to Gemini with
a system prompt, and returns a plain-language analysis to stdout.

Usage
-----
Default analysis (48h lookback):
    python3 hvac_diagnostic.py

Ask a specific question:
    python3 hvac_diagnostic.py --question "Why did floor 2 run so long today?"

Custom lookback window:
    python3 hvac_diagnostic.py --hours 24

Structured JSON output:
    python3 hvac_diagnostic.py --json

Override file paths:
    python3 hvac_diagnostic.py --state /path/to/state.json --events /path/to/events.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import google.generativeai as genai

from hvac_context import build_context

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an HVAC diagnostic assistant for a home monitoring system. "
    "You will be given structured data about recent HVAC behavior — zone runtimes, "
    "furnace sessions, temperatures, and any anomalies or warnings. "
    "Your job is to analyze this data and provide a clear, plain-language summary "
    "that a homeowner can understand. Be specific about numbers. Flag anything unusual. "
    "Keep your response concise (under 300 words)."
)

DEFAULT_QUESTION = (
    "Analyze the current HVAC behavior and flag anything unusual"
    " or worth the homeowner's attention."
)

GEMINI_MODEL = "gemini-2.0-flash"

JSON_MODE_INSTRUCTION = (
    "\n\nRespond ONLY with a valid JSON object (no markdown, no code fences) with these keys:\n"
    '  "summary": "2-3 sentence overview of current HVAC behavior",\n'
    '  "anomalies": ["list", "of", "unusual", "patterns"],\n'
    '  "recommendation": "one actionable recommendation, or \\"No action needed.\\""\n'
)


# ---------------------------------------------------------------------------
# Core functions (testable without argparse)
# ---------------------------------------------------------------------------


def build_diagnostic_prompt(context: str, question: str, json_mode: bool) -> str:
    """
    Build the full prompt string to send to the LLM.

    Parameters
    ----------
    context:
        HVAC context string from build_context()
    question:
        The diagnostic question to ask (user-provided or default)
    json_mode:
        If True, append JSON-mode instructions to the prompt

    Returns
    -------
    str
        Full prompt ready to send to Gemini
    """
    parts = [
        SYSTEM_PROMPT,
        "",
        "=== HVAC Context Data ===",
        context,
        "=== End Context ===",
        "",
        question,
    ]
    prompt = "\n".join(parts)

    if json_mode:
        prompt += JSON_MODE_INSTRUCTION

    return prompt


def run_diagnostic(args: argparse.Namespace) -> None:
    """
    Run the diagnostic: build context, call Gemini, print result.

    Parameters
    ----------
    args:
        Parsed argparse namespace with attributes:
        - hours (int)
        - state (str)
        - events (str)
        - question (str or None)
        - json (bool)
    """
    # Validate API key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "Error: GEMINI_API_KEY environment variable is not set. "
            "Please set it before running hvac_diagnostic.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build context
    context = build_context(
        state_path=args.state,
        events_path=args.events,
        lookback_hours=args.hours,
    )
    context_chars = len(context)

    # Build prompt
    question = args.question if args.question else DEFAULT_QUESTION
    json_mode = getattr(args, "json", False)
    prompt = build_diagnostic_prompt(context, question, json_mode)

    # Call Gemini
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    raw_text = response.text

    if json_mode:
        # Parse and re-emit structured JSON
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            # Try stripping markdown code fences if present
            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                # Remove first and last fence lines
                inner_lines = []
                in_block = False
                for line in lines:
                    if line.startswith("```") and not in_block:
                        in_block = True
                        continue
                    if line.startswith("```") and in_block:
                        break
                    if in_block:
                        inner_lines.append(line)
                cleaned = "\n".join(inner_lines)
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                print(
                    "Error: LLM returned invalid JSON. Raw response:\n" + raw_text,
                    file=sys.stderr,
                )
                sys.exit(1)

        # Ensure required keys with sensible defaults
        output = {
            "summary": parsed.get("summary", ""),
            "anomalies": parsed.get("anomalies", []),
            "recommendation": parsed.get("recommendation", "No action needed."),
            "context_chars": context_chars,
        }
        print(json.dumps(output, indent=2))
    else:
        print(raw_text)


def main() -> None:
    from hvac_context import DEFAULT_EVENTS_PATH, DEFAULT_LOOKBACK_HOURS, DEFAULT_STATE_PATH

    parser = argparse.ArgumentParser(description="On-demand LLM HVAC diagnostic using Gemini")
    parser.add_argument(
        "--question",
        default=None,
        help="Specific question to ask about the HVAC system",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Lookback window in hours (default: {DEFAULT_LOOKBACK_HOURS})",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        help="Path to state.json",
    )
    parser.add_argument(
        "--events",
        default=DEFAULT_EVENTS_PATH,
        help="Path to events.jsonl",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of plain text",
    )
    args = parser.parse_args()
    run_diagnostic(args)


if __name__ == "__main__":
    main()
