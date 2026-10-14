#!/usr/bin/env python3
"""SessionStart hook: surface Sugar queue status when a session begins.

Claude Code passes event data as JSON on stdin. This script is a no-op
unless the project is Sugar-initialized and the `sugar` CLI is on PATH,
so sessions in non-Sugar projects are never interrupted.
"""

import json
import os
import shutil
import subprocess
import sys

MAX_MESSAGE_LINES = 8


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, TypeError):
        return

    cwd = event.get("cwd") or os.getcwd()
    if not os.path.exists(os.path.join(cwd, ".sugar", "config.yaml")):
        # Not a Sugar project; stay silent.
        return

    sugar = shutil.which("sugar")
    if not sugar:
        return

    try:
        result = subprocess.run(
            [sugar, "status"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return

    if result.returncode != 0:
        return

    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if lines:
        summary = "\n".join(lines[:MAX_MESSAGE_LINES])
        json.dump({"systemMessage": summary}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hooks must never break the session.
        pass
