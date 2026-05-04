#!/usr/bin/env python3
"""Mock Copilot SDK agent for Symphony integration tests.

Standalone script — does NOT import symphony.  Invoked as::

    python3 mock_agent.py '<json_config>'

Config keys
-----------
turns            int   Number of turns to accept (default 1).
behavior         str   Default turn outcome: success|fail|cancel|input_required|exit|hang|error_response.
turn_behaviors   list  Per-turn override (0-indexed, falls back to *behavior*).
token_usage      dict  ``{"input": N, "output": N, "total": N}`` — emitted after each turn start.
slow_init_ms     int   Delay before init response (for read-timeout tests).
slow_turn_ms     int   Delay before sending turn outcome.
stderr_noise     bool  Write diagnostic lines to stderr.
approval_turn    int   Turn index on which to send an approval request and wait for response.
tool_call_turn   int   Turn index on which to send a tool call and wait for response.
tool_name        str   Name used in the tool call (default ``unknown_tool``).
rate_limit_turn  int   Turn index on which to send a rate-limit notification.
"""

from __future__ import annotations

import json
import sys
import time


def _read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line.strip())


def _write(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}

    turns = cfg.get("turns", 1)
    behavior = cfg.get("behavior", "success")
    turn_behaviors: list[str] = cfg.get("turn_behaviors", [])
    token_usage = cfg.get("token_usage")
    slow_init_ms = cfg.get("slow_init_ms", 0)
    slow_turn_ms = cfg.get("slow_turn_ms", 0)
    stderr_noise = cfg.get("stderr_noise", False)
    approval_turn = cfg.get("approval_turn")
    tool_call_turn = cfg.get("tool_call_turn")
    tool_name = cfg.get("tool_name", "unknown_tool")
    rate_limit_turn = cfg.get("rate_limit_turn")

    if stderr_noise:
        sys.stderr.write("MOCK_AGENT_STDERR: starting up\n")
        sys.stderr.flush()

    # ---- initialize ----
    if slow_init_ms:
        time.sleep(slow_init_ms / 1000.0)
    req = _read()
    _write({"jsonrpc": "2.0", "id": req["id"], "result": {"capabilities": {}}})

    # ---- thread/create ----
    req = _read()
    _write({"jsonrpc": "2.0", "id": req["id"], "result": {"threadId": "mock-thread-1"}})

    # ---- turns ----
    next_server_id = 1000
    for i in range(turns):
        tb = turn_behaviors[i] if i < len(turn_behaviors) else behavior

        req = _read()  # turn/start
        _write({"jsonrpc": "2.0", "id": req["id"], "result": {"turnId": f"mock-turn-{i + 1}"}})

        if slow_turn_ms:
            time.sleep(slow_turn_ms / 1000.0)

        if stderr_noise:
            sys.stderr.write(f"MOCK_AGENT_STDERR: turn {i + 1}\n")
            sys.stderr.flush()

        # Optional token-usage notification
        if token_usage:
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "usage": {
                            "inputTokens": token_usage.get("input", 100),
                            "outputTokens": token_usage.get("output", 50),
                            "totalTokens": token_usage.get("total", 150),
                        }
                    },
                }
            )

        # Optional rate-limit notification
        if rate_limit_turn is not None and i == rate_limit_turn:
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "rateLimits/updated",
                    "params": {"remaining": 42, "limit": 100, "reset": "2099-01-01T00:00:00Z"},
                }
            )

        # Optional approval request (waits for client response)
        if approval_turn is not None and i == approval_turn:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": next_server_id,
                    "method": "approval/requested",
                    "params": {"type": "command", "command": "echo test"},
                }
            )
            next_server_id += 1
            _read()  # consume approval response

        # Optional unsupported tool call (waits for client response)
        if tool_call_turn is not None and i == tool_call_turn:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": next_server_id,
                    "method": "tool/called",
                    "params": {"name": tool_name},
                }
            )
            next_server_id += 1
            _read()  # consume tool response

        # ---- terminal event ----
        if tb == "success":
            _write({"jsonrpc": "2.0", "method": "turn/completed", "params": {}})
        elif tb == "fail":
            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/failed",
                    "params": {"error": "mock turn failure"},
                }
            )
            break
        elif tb == "cancel":
            _write({"jsonrpc": "2.0", "method": "turn/cancelled", "params": {}})
            break
        elif tb == "input_required":
            _write({"jsonrpc": "2.0", "method": "turn/inputRequired", "params": {}})
            break
        elif tb == "exit":
            sys.exit(1)
        elif tb == "hang":
            while True:
                time.sleep(60)
        elif tb == "error_response":
            _write({"jsonrpc": "2.0", "error": {"code": -32000, "message": "mock protocol error"}})
            break

    # ---- shutdown ----
    try:
        _read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
