"""Agent runner – Copilot SDK subprocess client.

Manages the lifecycle of a coding-agent app-server subprocess:
launch, session init, multi-turn execution, event streaming, and cleanup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from symphony.config import ServiceConfig
from symphony.errors import (
    CopilotNotFoundError,
    InvalidWorkspaceCwdError,
    PortExitError,
    ResponseTimeoutError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, LiveSession

logger = logging.getLogger("symphony.runner")

_MAX_LINE_SIZE = 10 * 1024 * 1024  # 10 MB


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_session_id(thread_id: str, turn_id: str) -> str:
    return f"{thread_id}-{turn_id}"


class CopilotSession:
    """Manages a live Copilot SDK app-server subprocess session.

    Supports multiple turns on the same thread within one subprocess lifetime.
    """

    def __init__(
        self,
        config: ServiceConfig,
        workspace_path: str,
        issue: Issue,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace_path
        self._issue = issue
        self._on_event = on_event
        self._process: asyncio.subprocess.Process | None = None
        self._thread_id: str = ""
        self._turn_id: str = ""
        self._request_id: int = 0
        self._session = LiveSession()
        self._started = False
        self._stderr_task: asyncio.Task | None = None

    @property
    def session(self) -> LiveSession:
        return self._session

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _emit(self, event_name: str, **kwargs: Any) -> None:
        evt = AgentEvent(
            event=event_name,
            issue_id=self._issue.id,
            timestamp=_now_utc(),
            copilot_pid=str(self._process.pid) if self._process else None,
            session_id=self._session.session_id or None,
            thread_id=self._thread_id or None,
            turn_id=self._turn_id or None,
            **kwargs,
        )
        self._session.last_copilot_event = event_name
        self._session.last_copilot_timestamp = evt.timestamp
        if kwargs.get("message"):
            self._session.last_copilot_message = kwargs["message"]
        if self._on_event:
            self._on_event(evt)

    async def _drain_stderr(self) -> None:
        """Read and log stderr to prevent pipe buffer deadlock."""
        if not self._process or not self._process.stderr:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    logger.debug("agent_stderr issue=%s: %s", self._issue.identifier, text[:500])
        except Exception:
            pass

    async def _write_message(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the subprocess stdin."""
        if not self._process or not self._process.stdin:
            raise PortExitError(None)
        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def _read_message(self, timeout_ms: int | None = None) -> dict[str, Any]:
        """Read one JSON line from subprocess stdout."""
        if not self._process or not self._process.stdout:
            raise PortExitError(None)
        timeout = (timeout_ms or self._config.copilot_read_timeout_ms) / 1000.0
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise ResponseTimeoutError("Read timed out")
        if not line:
            rc = self._process.returncode
            raise PortExitError(rc)
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("malformed_json line=%s", line[:200])
            self._emit("malformed", message=line.decode(errors="replace")[:200])
            return {"malformed": True}

    async def start(self) -> None:
        """Launch the subprocess and initialize the app-server session."""
        # Validate workspace cwd
        abs_workspace = os.path.abspath(self._workspace)
        if not os.path.isdir(abs_workspace):
            raise InvalidWorkspaceCwdError(abs_workspace, "does not exist")

        cmd = self._config.copilot_command
        logger.info(
            "agent_launch command=%r cwd=%s issue=%s",
            cmd, abs_workspace, self._issue.identifier,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                "bash", "-lc", cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=abs_workspace,
                limit=_MAX_LINE_SIZE,
            )
        except FileNotFoundError:
            raise CopilotNotFoundError(cmd)

        self._session.copilot_pid = str(self._process.pid)

        # Start background stderr drainer to prevent deadlocks
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

        # Initialize session – send initialization request
        init_msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "symphony", "version": "0.1.0"},
                "capabilities": {},
            },
        }
        await self._write_message(init_msg)
        resp = await self._read_message(self._config.copilot_read_timeout_ms)

        # Create thread
        thread_msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "thread/create",
            "params": {
                "cwd": abs_workspace,
            },
        }
        if self._config.copilot_thread_sandbox:
            thread_msg["params"]["sandbox"] = self._config.copilot_thread_sandbox
        await self._write_message(thread_msg)
        thread_resp = await self._read_message(self._config.copilot_read_timeout_ms)

        # Extract thread_id from response
        result = thread_resp.get("result", {})
        self._thread_id = str(result.get("threadId", result.get("id", f"thread-{self._process.pid}")))
        self._session.thread_id = self._thread_id
        self._started = True

        self._emit("session_started", message=f"Thread {self._thread_id} created")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Run one coding-agent turn. Returns True on success, raises on failure."""
        if not self._started or not self._process:
            raise PortExitError(None)

        turn_msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "turn/start",
            "params": {
                "threadId": self._thread_id,
                "message": prompt,
                "cwd": os.path.abspath(self._workspace),
                "title": f"{self._issue.identifier}: {self._issue.title}",
            },
        }
        if self._config.copilot_approval_policy:
            turn_msg["params"]["approvalPolicy"] = self._config.copilot_approval_policy
        if self._config.copilot_turn_sandbox_policy:
            turn_msg["params"]["sandboxPolicy"] = self._config.copilot_turn_sandbox_policy

        await self._write_message(turn_msg)

        self._session.turn_count += 1
        turn_timeout = self._config.copilot_turn_timeout_ms / 1000.0
        turn_start = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - turn_start
            remaining = turn_timeout - elapsed
            if remaining <= 0:
                raise TurnTimeoutError()

            try:
                msg = await self._read_message(int(remaining * 1000))
            except ResponseTimeoutError:
                raise TurnTimeoutError()
            except PortExitError:
                raise

            if msg.get("malformed"):
                continue

            # Process the message
            method = msg.get("method", "")
            params = msg.get("params", {})
            msg_id = msg.get("id")
            result = msg.get("result", {})

            # Handle JSON-RPC response (result for our turn/start request)
            if "result" in msg and not method:
                turn_result = result
                self._turn_id = str(turn_result.get("turnId", turn_result.get("id", f"turn-{turn_number}")))
                self._session.turn_id = self._turn_id
                self._session.session_id = _make_session_id(self._thread_id, self._turn_id)
                continue

            # Handle error response
            if "error" in msg and not method:
                error = msg["error"]
                err_msg = error.get("message", str(error))
                raise TurnFailedError(err_msg)

            # Handle notifications/events from the server
            event_type = method or params.get("type", "")

            if event_type in ("turn/completed", "turn/finished"):
                self._emit("turn_completed", message="Turn completed")
                return True

            elif event_type in ("turn/failed", "turn/error"):
                err = params.get("error", params.get("message", "unknown"))
                self._emit("turn_failed", error=str(err))
                raise TurnFailedError(str(err))

            elif event_type == "turn/cancelled":
                self._emit("turn_cancelled")
                raise TurnCancelledError()

            elif event_type == "turn/inputRequired":
                self._emit("turn_input_required")
                raise TurnInputRequiredError()

            elif event_type == "approval/requested":
                # Auto-approve (high-trust policy)
                if msg_id is not None:
                    approve_resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"approved": True},
                    }
                    await self._write_message(approve_resp)
                self._emit("approval_auto_approved", message="Auto-approved")

            elif event_type == "tool/called":
                tool_name = params.get("name", "")
                # Handle unsupported tool calls
                if msg_id is not None:
                    tool_resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "success": False,
                            "error": f"Tool '{tool_name}' is not supported",
                        },
                    }
                    await self._write_message(tool_resp)
                self._emit("unsupported_tool_call", message=f"Unsupported tool: {tool_name}")

            elif event_type in ("thread/tokenUsage/updated",):
                # Extract token usage
                usage = params.get("usage", params)
                inp = usage.get("inputTokens", usage.get("input_tokens", 0))
                out = usage.get("outputTokens", usage.get("output_tokens", 0))
                total = usage.get("totalTokens", usage.get("total_tokens", inp + out))
                self._session.copilot_input_tokens = int(inp)
                self._session.copilot_output_tokens = int(out)
                self._session.copilot_total_tokens = int(total)
                self._emit(
                    "notification",
                    usage={"input_tokens": int(inp), "output_tokens": int(out), "total_tokens": int(total)},
                )

            elif event_type.startswith("rateLimit"):
                self._emit("notification", rate_limits=params)

            else:
                summary = str(params.get("message", params.get("text", "")))[:200]
                self._emit("other_message", message=summary or event_type)

    async def stop(self) -> None:
        """Shut down the app-server subprocess."""
        # Cancel stderr drainer
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if not self._process:
            return
        try:
            # Send shutdown
            shutdown_msg = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "shutdown",
                "params": {},
            }
            if self._process.stdin and not self._process.stdin.is_closing():
                await self._write_message(shutdown_msg)
                self._process.stdin.close()
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

        self._started = False


async def run_agent_session(
    config: ServiceConfig,
    workspace_path: str,
    issue: Issue,
    prompt: str,
    attempt: int | None,
    on_event: Callable[[AgentEvent], None] | None = None,
    max_turns: int = 20,
    fetch_issue_state: Callable[[str], Any] | None = None,
) -> LiveSession:
    """Run a full agent session: start, multi-turn loop, stop.

    Returns the final :class:`LiveSession` state.
    """
    session = CopilotSession(config, workspace_path, issue, on_event=on_event)

    try:
        await session.start()
    except Exception as exc:
        logger.error("agent_startup_failed issue=%s error=%s", issue.identifier, exc)
        raise

    try:
        turn_number = 1
        current_prompt = prompt

        while True:
            await session.run_turn(current_prompt, turn_number)

            # Re-check issue state if callback is provided (spec §16.5)
            if fetch_issue_state:
                refreshed = await fetch_issue_state(issue.id)
                if refreshed and hasattr(refreshed, "state"):
                    issue_state = refreshed.state.lower()
                    active_states = config.active_states
                    if issue_state not in active_states:
                        logger.info(
                            "issue_no_longer_active issue=%s state=%s",
                            issue.identifier, issue_state,
                        )
                        break

            if turn_number >= max_turns:
                logger.info("max_turns_reached issue=%s turns=%d", issue.identifier, max_turns)
                break

            turn_number += 1
            # Continuation turns use guidance, not the full prompt
            current_prompt = (
                "Continue working on the issue. Review your previous progress "
                "and continue from where you left off."
            )

    finally:
        await session.stop()

    return session.session
