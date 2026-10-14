#!/usr/bin/env python3
"""PostToolUse hook: suggest Sugar workflows for Bash activity.

Claude Code passes event data as JSON on stdin:

    {"session_id": "...", "tool_name": "Bash",
     "tool_input": {"command": "..."}, "tool_response": {...}}

Two checks run here, replacing the declarative filter/throttle blocks that
Claude Code's hook schema has no equivalent for:

1. Error detection in tool output -> suggest /sugar-task (max 1 per session)
2. `git commit` in tool input -> suggest sugar update --status completed
   (max 1 per session)

Throttle state lives in a temp directory keyed by session id, so it resets
naturally with the session.
"""

import json
import os
import re
import sys
import tempfile

ERROR_PATTERN = re.compile(
    r"\b(error|exception|fail(?:ed|ure)?|bug|crash)\b", re.IGNORECASE
)
COMMIT_PATTERN = re.compile(r"\bgit\s+(?:\S+\s+)*commit\b")

MAX_PER_SESSION = 1

ERROR_MESSAGE = (
    "Error detected. If this needs fixing, create a task with /sugar-task "
    'or run: sugar add "fix: <what failed>" --type bug_fix'
)
COMMIT_MESSAGE = (
    "Code committed. If this completes a Sugar task, update it with: "
    "sugar update <TASK_ID> --status completed"
)

OUTPUT_KEYS = ("stdout", "stderr", "content", "output", "error", "text", "message")


def _extract_text(value):
    """Flatten tool_response JSON into text to scan."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_extract_text(v) for k, v in value.items() if k in OUTPUT_KEYS)
    if isinstance(value, list):
        return " ".join(_extract_text(v) for v in value)
    return ""


def _state_path(session_id, check):
    state_dir = os.path.join(tempfile.gettempdir(), "sugar-hooks")
    try:
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, f"{session_id}-{check}")
    except OSError:
        return None


def _throttled(session_id, check):
    """True once this check has already fired MAX_PER_SESSION times this session."""
    path = _state_path(session_id, check)
    if path is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            if int(f.read().strip() or 0) >= MAX_PER_SESSION:
                return True
    except (OSError, ValueError):
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("1")
    except OSError:
        pass
    return False


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, TypeError):
        return

    session_id = str(event.get("session_id") or "unknown")
    tool_input = event.get("tool_input") or {}
    tool_response = event.get("tool_response") or {}

    # Check 1: error patterns in tool output
    output_text = _extract_text(tool_response)
    if not output_text:
        try:
            output_text = json.dumps(tool_response)
        except (TypeError, ValueError):
            output_text = ""
    if output_text and ERROR_PATTERN.search(output_text):
        if not _throttled(session_id, "error"):
            json.dump({"systemMessage": ERROR_MESSAGE}, sys.stdout)

    # Check 2: git commit in tool input
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if command and COMMIT_PATTERN.search(str(command)):
        if not _throttled(session_id, "commit"):
            json.dump({"systemMessage": COMMIT_MESSAGE}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hooks must never break the session.
        pass
