"""Runtime tests for plugin hook scripts.

Claude Code passes hook event data as JSON on stdin and reads a JSON
response on stdout (e.g. {"systemMessage": "..."}). These tests drive the
scripts the same way the harness does.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / ".claude-plugin" / "hooks"
SESSION_START = HOOKS_DIR / "session_start.py"
POST_TOOL_USE = HOOKS_DIR / "post_tool_use.py"


def run_hook(script, event, cwd=None):
    """Run a hook script with event JSON on stdin, like Claude Code does."""
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=15,
    )


@pytest.fixture(autouse=True)
def clean_hook_state():
    """Remove throttle state between tests.

    The hook keeps state under tempfile.gettempdir(), which on macOS is
    /var/folders/.../T, not /tmp - so resolve it the same way.
    """
    state_dir = Path(tempfile.gettempdir()) / "sugar-hooks"
    shutil.rmtree(state_dir, ignore_errors=True)
    yield
    shutil.rmtree(state_dir, ignore_errors=True)


def post_tool_use_event(session_id, command="echo hello", tool_response=None):
    return {
        "session_id": session_id,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": tool_response or {},
    }


class TestHooksJsonSchema:
    """hooks.json must match Claude Code's hook schema."""

    def test_valid_schema(self):
        config = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        hooks = config["hooks"]
        assert isinstance(hooks, dict), "hooks must be an object keyed by event name"

        for event_name, event_hooks in hooks.items():
            assert isinstance(event_hooks, list)
            for hook_entry in event_hooks:
                assert set(hook_entry.keys()) <= {"matcher", "hooks"}
                for hook in hook_entry["hooks"]:
                    assert hook["type"] == "command"
                    assert hook["command"]

    def test_no_auto_approve_pretooluse(self):
        """PreToolUse auto-approval must not come back."""
        config = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        for hook_entry in config["hooks"].get("PreToolUse", []):
            for hook in hook_entry["hooks"]:
                assert "decision" not in hook.get("command", "")

    def test_hook_scripts_exist(self):
        for event_name, event_hooks in json.loads(
            (HOOKS_DIR / "hooks.json").read_text(encoding="utf-8")
        )["hooks"].items():
            for hook_entry in event_hooks:
                for hook in hook_entry["hooks"]:
                    command = hook["command"]
                    assert command.startswith(
                        "python3 "
                    ), "hook commands must run via python3"
                    script = HOOKS_DIR / command.split("/")[-1].strip('"')
                    assert script.exists(), f"hook script missing: {script}"


class TestPostToolUseHook:
    """Error and commit detection driven through stdin/stdout."""

    def test_error_in_output_emits_system_message(self):
        event = post_tool_use_event(
            "sess-error-1",
            command="pytest",
            tool_response={"stdout": "FAILED tests/test_core.py - AssertionError"},
        )
        result = run_hook(POST_TOOL_USE, event)
        assert result.returncode == 0

        message = json.loads(result.stdout)["systemMessage"]
        assert "/sugar-task" in message

    def test_error_suggestion_throttled_per_session(self):
        event = post_tool_use_event(
            "sess-error-1",
            tool_response={"stdout": "Error: connection refused"},
        )
        first = run_hook(POST_TOOL_USE, event)
        second = run_hook(POST_TOOL_USE, event)

        assert "/sugar-task" in json.loads(first.stdout)["systemMessage"]
        assert second.stdout.strip() == "", "second error must be throttled"

    def test_git_commit_emits_system_message(self):
        event = post_tool_use_event(
            "sess-commit-1", command='git commit -m "fix: handle empty input"'
        )
        result = run_hook(POST_TOOL_USE, event)
        assert result.returncode == 0

        message = json.loads(result.stdout)["systemMessage"]
        assert "sugar update" in message
        assert "--status completed" in message

    def test_commit_suggestion_throttled_per_session(self):
        event = post_tool_use_event("sess-commit-1", command="git commit -m x")
        first = run_hook(POST_TOOL_USE, event)
        second = run_hook(POST_TOOL_USE, event)

        assert "systemMessage" in json.loads(first.stdout)
        assert second.stdout.strip() == ""

    def test_clean_output_and_no_git_produce_nothing(self):
        event = post_tool_use_event(
            "sess-quiet-1", command="ls -la", tool_response={"stdout": "file1\nfile2"}
        )
        result = run_hook(POST_TOOL_USE, event)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_malformed_stdin_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(POST_TOOL_USE)],
            input="not json",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestSessionStartHook:
    def test_silent_for_non_sugar_project(self, tmp_path):
        result = run_hook(
            SESSION_START,
            {"session_id": "sess-s1", "hook_event_name": "SessionStart", "cwd": None},
            cwd=tmp_path,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    @pytest.mark.skipif(shutil.which("sugar") is None, reason="sugar CLI not installed")
    def test_reports_status_in_sugar_project(self, tmp_path):
        result = subprocess.run(
            ["sugar", "init"], cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            pytest.skip(f"sugar init failed: {result.stderr}")

        event = {
            "session_id": "sess-s2",
            "hook_event_name": "SessionStart",
            "cwd": str(tmp_path),
        }
        result = run_hook(SESSION_START, event, cwd=tmp_path)
        assert result.returncode == 0

        if shutil.which("sugar"):
            # Status surfaced only when the CLI runs cleanly.
            status = subprocess.run(
                ["sugar", "status"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if status.returncode == 0:
                assert "systemMessage" in json.loads(result.stdout)
            else:
                assert result.stdout.strip() == ""
