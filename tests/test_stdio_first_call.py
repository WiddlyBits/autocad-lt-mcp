"""First tool call over real stdio must answer while stdin stays open.

The hang needs a pending stdin read on a pipe, so only a subprocess reproduces it. It has
only been measured on Windows; on other platforms this passes with or without the fix.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading


def _rpc(proc: subprocess.Popen, answers: queue.Queue, msg_id: int, method: str, params: dict) -> dict:
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}).encode() + b"\n")
    proc.stdin.flush()
    while True:
        answer = answers.get(timeout=60)  # raises queue.Empty on a hang; stdin stays open throughout
        if answer.get("id") == msg_id:
            return answer


def _pump(stream, answers: queue.Queue) -> None:
    for line in stream:
        try:
            answers.put(json.loads(line))
        except ValueError:
            pass


def test_first_tool_calls_answer_with_stdin_open():
    # The env var keeps the server off the file_ipc path, which posts keystrokes to an open AutoCAD.
    env = {**os.environ, "AUTOCAD_MCP_BACKEND": "ezdxf"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "autocad_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    answers: queue.Queue = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, answers), daemon=True).start()
    try:
        _rpc(
            proc,
            answers,
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
        proc.stdin.flush()

        status = _rpc(proc, answers, 2, "tools/call", {"name": "system", "arguments": {"operation": "status"}})
        assert '\\"backend\\":\\"ezdxf\\"' in json.dumps(status)

        # The screenshot path lazily imports matplotlib and PIL on the event-loop thread.
        shot = _rpc(proc, answers, 3, "tools/call", {"name": "view", "arguments": {"operation": "get_screenshot"}})
        assert "result" in shot
        assert any(part["type"] == "image" for part in shot["result"]["content"]), shot
    finally:
        proc.kill()
        proc.wait()
