"""Orchestrator – poll loop, dispatch, reconciliation, retry.

This is the single authority that mutates scheduling state.  Workers
communicate back via an ``asyncio.Queue`` of immutable events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from symphony.config import ServiceConfig
from symphony.errors import SymphonyError
from symphony.models import (
    AgentEvent,
    CopilotTotals,
    Issue,
    LiveSession,
    OrchestratorState,
    RateLimitInfo,
    RetryEntry,
    RunningEntry,
    WorkerResult,
)
from symphony.prompt import render_prompt
from symphony.runner import run_agent_session
from symphony.tracker import GitHubTrackerClient
from symphony.workflow import WorkflowDefinition, get_workflow_mtime, load_workflow
from symphony import workspace as ws_mod

logger = logging.getLogger("symphony.orchestrator")

_CONTINUATION_DELAY_MS = 1000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _issue_number(identifier: str) -> int | None:
    """Extract issue number from identifier like ``#123``."""
    m = re.match(r"#?(\d+)", identifier)
    return int(m.group(1)) if m else None


def _sort_key(issue: Issue) -> tuple:
    """Dispatch sort key: priority asc (null last), created_at asc, identifier."""
    p = issue.priority if issue.priority is not None else 999999
    c = issue.created_at or datetime.max.replace(tzinfo=timezone.utc)
    return (p, c, issue.identifier)


class Orchestrator:
    """Central scheduling authority for Symphony."""

    def __init__(
        self,
        workflow_path: str,
        port: int | None = None,
    ) -> None:
        self._workflow_path = os.path.abspath(workflow_path)
        self._workflow_dir = os.path.dirname(self._workflow_path)
        self._cli_port = port
        self._state = OrchestratorState()
        self._event_queue: asyncio.Queue[AgentEvent | WorkerResult] = asyncio.Queue()
        self._config: ServiceConfig | None = None
        self._last_good_config: ServiceConfig | None = None
        self._workflow_mtime: float | None = None
        self._tracker: GitHubTrackerClient | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observers: list[Callable[[], None]] = []

    @property
    def state(self) -> OrchestratorState:
        return self._state

    @property
    def config(self) -> ServiceConfig | None:
        return self._last_good_config or self._config

    def _effective_config(self) -> ServiceConfig | None:
        return self._last_good_config or self._config

    # --- Workflow reload ---

    def _load_and_apply_workflow(self) -> list[str]:
        """Load WORKFLOW.md and apply config. Returns validation errors."""
        try:
            wf = load_workflow(self._workflow_path)
        except SymphonyError as exc:
            logger.error("workflow_load_failed error=%s", exc)
            return [str(exc)]

        try:
            cfg = ServiceConfig(wf, self._workflow_dir)
        except Exception as exc:
            logger.error("config_build_failed error=%s", exc)
            return [str(exc)]

        errors = cfg.validate_dispatch()
        if errors:
            logger.error("config_validation_failed errors=%s", errors)
            return errors

        # Config is valid – apply
        self._config = cfg
        self._last_good_config = cfg
        self._workflow_mtime = get_workflow_mtime(self._workflow_path)

        # Update orchestrator state
        self._state.poll_interval_ms = cfg.poll_interval_ms
        self._state.max_concurrent_agents = cfg.max_concurrent_agents

        # Update tracker client
        if self._tracker:
            self._tracker.update_config(
                cfg.tracker_endpoint,
                cfg.tracker_api_key,
                cfg.tracker_repo,
                cfg.active_states,
                cfg.terminal_states,
            )
        else:
            self._tracker = GitHubTrackerClient(
                endpoint=cfg.tracker_endpoint,
                api_key=cfg.tracker_api_key,
                repo=cfg.tracker_repo,
                active_states=cfg.active_states,
                terminal_states=cfg.terminal_states,
            )

        logger.info("workflow_loaded path=%s", self._workflow_path)
        return []

    def _check_workflow_reload(self) -> None:
        """Re-read WORKFLOW.md if it has changed on disk (or been deleted)."""
        mtime = get_workflow_mtime(self._workflow_path)
        if mtime is None:
            # File is missing/unreadable – emit error, keep last good config
            if self._workflow_mtime is not None:
                logger.error(
                    "workflow_missing path=%s, keeping last good config",
                    self._workflow_path,
                )
                self._workflow_mtime = None
            return
        if mtime != self._workflow_mtime:
            logger.info("workflow_change_detected path=%s", self._workflow_path)
            errors = self._load_and_apply_workflow()
            if errors:
                logger.error("workflow_reload_failed errors=%s, keeping last good config", errors)

    # --- Startup ---

    async def start(self) -> None:
        """Start the orchestrator: validate, cleanup terminals, begin polling."""
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Initial load
        errors = self._load_and_apply_workflow()
        if errors:
            raise SymphonyError(
                f"Startup validation failed: {'; '.join(errors)}",
                code="startup_validation_failed",
            )

        # Startup terminal workspace cleanup
        await self._startup_terminal_cleanup()

        # Start event processor
        asyncio.ensure_future(self._process_events())

        # Schedule first tick immediately
        self._schedule_tick(0)

        logger.info("orchestrator_started poll_interval_ms=%d", self._state.poll_interval_ms)

    async def stop(self) -> None:
        """Gracefully stop the orchestrator."""
        self._running = False
        if self._tick_handle:
            self._tick_handle.cancel()

        # Cancel all running workers
        for entry in list(self._state.running.values()):
            if entry.worker_task and not entry.worker_task.done():
                entry.worker_task.cancel()

        # Cancel retry timers
        for retry in self._state.retry_attempts.values():
            if retry.timer_handle:
                retry.timer_handle.cancel()

        # Close tracker
        if self._tracker:
            await self._tracker.close()

        logger.info("orchestrator_stopped")

    # --- Terminal cleanup ---

    async def _startup_terminal_cleanup(self) -> None:
        """Remove workspaces for issues already in terminal states."""
        cfg = self._effective_config()
        if not cfg or not self._tracker:
            return
        try:
            terminal_issues = await self._tracker.fetch_issues_by_states(cfg.terminal_states)
            for issue in terminal_issues:
                await ws_mod.cleanup_workspace(cfg, issue.identifier)
            if terminal_issues:
                logger.info("startup_cleanup removed=%d workspaces", len(terminal_issues))
        except Exception as exc:
            logger.warning("startup_cleanup_failed error=%s", exc)

    # --- Tick scheduling ---

    def _schedule_tick(self, delay_ms: int) -> None:
        if not self._running or not self._loop:
            return
        if self._tick_handle:
            self._tick_handle.cancel()
        self._tick_handle = self._loop.call_later(
            delay_ms / 1000.0,
            lambda: asyncio.ensure_future(self._on_tick()),
        )

    async def _on_tick(self) -> None:
        """One poll-and-dispatch cycle."""
        if not self._running:
            return

        try:
            # Check for workflow changes
            self._check_workflow_reload()

            cfg = self._effective_config()
            if not cfg or not self._tracker:
                self._schedule_tick(self._state.poll_interval_ms)
                return

            # 1. Reconcile running issues
            await self._reconcile()

            # 2. Dispatch preflight validation
            errors = cfg.validate_dispatch()
            if errors:
                logger.error("dispatch_validation_failed errors=%s", errors)
                self._notify_observers()
                self._schedule_tick(self._state.poll_interval_ms)
                return

            # 3. Fetch candidate issues
            try:
                candidates = await self._tracker.fetch_candidate_issues()
            except Exception as exc:
                logger.error("candidate_fetch_failed error=%s", exc)
                self._notify_observers()
                self._schedule_tick(self._state.poll_interval_ms)
                return

            # 4. Sort for dispatch
            candidates.sort(key=_sort_key)

            # 5. Dispatch eligible issues
            for issue in candidates:
                if self._available_slots() <= 0:
                    break
                if self._should_dispatch(issue):
                    await self._dispatch_issue(issue, attempt=None)

            self._notify_observers()
        except Exception as exc:
            logger.error("tick_error error=%s", exc, exc_info=True)
        finally:
            self._schedule_tick(self._state.poll_interval_ms)

    # --- Dispatch ---

    def _available_slots(self) -> int:
        return max(self._state.max_concurrent_agents - len(self._state.running), 0)

    def _per_state_slots(self, state: str) -> int:
        cfg = self._effective_config()
        if not cfg:
            return 0
        by_state = cfg.max_concurrent_agents_by_state
        state_lower = state.lower()
        if state_lower not in by_state:
            return self._available_slots()
        limit = by_state[state_lower]
        current = sum(
            1 for e in self._state.running.values()
            if e.state.lower() == state_lower
        )
        return max(limit - current, 0)

    def _should_dispatch(self, issue: Issue) -> bool:
        """Check if an issue is dispatch-eligible."""
        # Required fields
        if not all([issue.id, issue.identifier, issue.title, issue.state]):
            return False

        cfg = self._effective_config()
        if not cfg:
            return False

        state = issue.state.lower()

        # Must be active, not terminal
        if state not in cfg.active_states:
            return False
        if state in cfg.terminal_states:
            return False

        # Not already running or claimed
        if issue.id in self._state.running:
            return False
        if issue.id in self._state.claimed:
            return False

        # Global slots
        if self._available_slots() <= 0:
            return False

        # Per-state slots
        if self._per_state_slots(state) <= 0:
            return False

        # Blocker rule for Todo state
        if state == "todo":
            for blocker in issue.blocked_by:
                blocker_state = (blocker.state or "").lower()
                if blocker_state and blocker_state not in cfg.terminal_states:
                    return False

        return True

    async def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        """Spawn a worker task for the issue."""
        cfg = self._effective_config()
        if not cfg:
            return

        logger.info(
            "dispatch issue_id=%s issue_identifier=%s attempt=%s",
            issue.id, issue.identifier, attempt,
        )

        # Claim
        self._state.claimed.add(issue.id)

        # Remove from retry if present
        retry = self._state.retry_attempts.pop(issue.id, None)
        if retry and retry.timer_handle:
            retry.timer_handle.cancel()

        # Create running entry
        entry = RunningEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            issue=issue,
            retry_attempt=attempt,
            started_at=_now_utc(),
            state=issue.state,
        )

        # Spawn worker
        task = asyncio.ensure_future(self._run_worker(issue, attempt))
        entry.worker_task = task
        self._state.running[issue.id] = entry

    async def _run_worker(self, issue: Issue, attempt: int | None) -> None:
        """Worker coroutine: workspace + prompt + agent session."""
        cfg = self._effective_config()
        if not cfg:
            return
        started = _now_utc()
        result = WorkerResult(
            issue_id=issue.id,
            identifier=issue.identifier,
            success=False,
            started_at=started,
        )

        try:
            # 1. Create workspace
            workspace = await ws_mod.create_workspace(cfg, issue.identifier)

            # 2. Run before_run hook
            if cfg.hook_before_run:
                await ws_mod.run_hook("before_run", cfg.hook_before_run, workspace.path, cfg.hook_timeout_ms)

            # 3. Build prompt
            issue_dict = issue.to_template_dict()
            prompt = render_prompt(cfg.prompt_template, issue_dict, attempt)

            # 4. Build issue state refresh callback for multi-turn
            async def _fetch_issue_state(issue_id: str) -> Issue | None:
                num = _issue_number(issue.identifier)
                if num is None or not self._tracker:
                    return None
                issues = await self._tracker.fetch_issues_by_numbers([num])
                return issues[0] if issues else None

            # 5. Run agent session
            def on_event(evt: AgentEvent) -> None:
                self._event_queue.put_nowait(evt)

            session = await run_agent_session(
                config=cfg,
                workspace_path=workspace.path,
                issue=issue,
                prompt=prompt,
                attempt=attempt,
                on_event=on_event,
                max_turns=cfg.max_turns,
                fetch_issue_state=_fetch_issue_state,
            )

            result.success = True
            result.session = session

            # Run after_run hook (best-effort)
            if cfg.hook_after_run:
                try:
                    await ws_mod.run_hook("after_run", cfg.hook_after_run, workspace.path, cfg.hook_timeout_ms)
                except Exception as exc:
                    logger.warning("after_run_hook_failed issue=%s error=%s", issue.identifier, exc)

        except asyncio.CancelledError:
            result.error = "cancelled"
            logger.info("worker_cancelled issue_id=%s issue_identifier=%s", issue.id, issue.identifier)
            # Best-effort after_run on cancellation
            if cfg.hook_after_run:
                ws_path = ws_mod.workspace_path_for(cfg.workspace_root, issue.identifier)
                if os.path.isdir(ws_path):
                    try:
                        await ws_mod.run_hook("after_run", cfg.hook_after_run, ws_path, cfg.hook_timeout_ms)
                    except Exception:
                        pass
        except Exception as exc:
            result.error = str(exc)
            logger.error(
                "worker_failed issue_id=%s issue_identifier=%s error=%s",
                issue.id, issue.identifier, exc,
            )
            # Run after_run hook (best-effort) if workspace exists
            if cfg.hook_after_run:
                ws_path = ws_mod.workspace_path_for(cfg.workspace_root, issue.identifier)
                if os.path.isdir(ws_path):
                    try:
                        await ws_mod.run_hook("after_run", cfg.hook_after_run, ws_path, cfg.hook_timeout_ms)
                    except Exception:
                        pass

        result.ended_at = _now_utc()
        await self._event_queue.put(result)

    # --- Event processing ---

    async def _process_events(self) -> None:
        """Drain the event queue and apply state mutations."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if isinstance(event, WorkerResult):
                self._handle_worker_exit(event)
            elif isinstance(event, AgentEvent):
                self._handle_agent_event(event)

    def _handle_worker_exit(self, result: WorkerResult) -> None:
        """Handle a worker completion/failure."""
        entry = self._state.running.pop(result.issue_id, None)

        # If this worker was already removed by reconciliation, skip retry
        if entry is None:
            # Check if reconciliation already handled this issue
            # (running entry was popped by _terminate_running)
            return

        # Accumulate runtime
        if entry.started_at:
            elapsed = (_now_utc() - entry.started_at).total_seconds()
            self._state.copilot_totals.seconds_running += elapsed

        # Accumulate tokens from the session
        session = result.session
        delta_in = session.copilot_input_tokens - session.last_reported_input_tokens
        delta_out = session.copilot_output_tokens - session.last_reported_output_tokens
        delta_total = session.copilot_total_tokens - session.last_reported_total_tokens
        self._state.copilot_totals.input_tokens += max(delta_in, 0)
        self._state.copilot_totals.output_tokens += max(delta_out, 0)
        self._state.copilot_totals.total_tokens += max(delta_total, 0)

        if result.success:
            self._state.completed.add(result.issue_id)
            # Schedule continuation retry
            self._schedule_retry(
                result.issue_id,
                attempt=1,
                identifier=result.identifier,
                error=None,
                delay_ms=_CONTINUATION_DELAY_MS,
            )
            logger.info(
                "worker_succeeded issue_id=%s issue_identifier=%s",
                result.issue_id, result.identifier,
            )
        else:
            next_attempt = (entry.retry_attempt or 0) + 1 if entry else 1
            self._schedule_retry(
                result.issue_id,
                attempt=next_attempt,
                identifier=result.identifier,
                error=result.error,
            )
            logger.info(
                "worker_failed_retrying issue_id=%s issue_identifier=%s attempt=%d error=%s",
                result.issue_id, result.identifier, next_attempt, result.error,
            )

        self._notify_observers()

    def _handle_agent_event(self, event: AgentEvent) -> None:
        """Update running entry with agent event data."""
        entry = self._state.running.get(event.issue_id)
        if not entry:
            return

        if event.session_id:
            entry.session.session_id = event.session_id
        if event.thread_id:
            entry.session.thread_id = event.thread_id
        if event.turn_id:
            entry.session.turn_id = event.turn_id
        if event.copilot_pid:
            entry.session.copilot_pid = event.copilot_pid
        if event.event:
            entry.session.last_copilot_event = event.event
        if event.timestamp:
            entry.session.last_copilot_timestamp = event.timestamp
        if event.message:
            entry.session.last_copilot_message = event.message

        # Token accounting – use absolute totals
        if event.usage:
            inp = event.usage.get("input_tokens", 0)
            out = event.usage.get("output_tokens", 0)
            total = event.usage.get("total_tokens", inp + out)
            if inp or out or total:
                entry.session.copilot_input_tokens = inp
                entry.session.copilot_output_tokens = out
                entry.session.copilot_total_tokens = total

        # Rate limits
        if event.rate_limits:
            self._state.copilot_rate_limits = RateLimitInfo(data=event.rate_limits)

    # --- Reconciliation ---

    async def _reconcile(self) -> None:
        """Reconcile running issues: stall detection + state refresh."""
        cfg = self._effective_config()
        if not cfg or not self._tracker:
            return

        await self._reconcile_stalls(cfg)
        await self._reconcile_states(cfg)

    async def _reconcile_stalls(self, cfg: ServiceConfig) -> None:
        """Kill stalled sessions."""
        stall_timeout_ms = cfg.copilot_stall_timeout_ms
        if stall_timeout_ms <= 0:
            return

        now = _now_utc()
        stalled_ids: list[str] = []

        for issue_id, entry in self._state.running.items():
            ref_time = entry.session.last_copilot_timestamp or entry.started_at
            if ref_time:
                elapsed_ms = (now - ref_time).total_seconds() * 1000
                if elapsed_ms > stall_timeout_ms:
                    stalled_ids.append(issue_id)

        for issue_id in stalled_ids:
            entry = self._state.running.get(issue_id)
            identifier = entry.identifier if entry else issue_id
            retry_attempt = (entry.retry_attempt or 0) + 1 if entry else 1
            logger.warning("stall_detected issue_id=%s", issue_id)
            await self._terminate_running(issue_id, cleanup_workspace=False)
            # Schedule retry with exponential backoff (spec §7.3, §8.5)
            self._schedule_retry(
                issue_id,
                attempt=retry_attempt,
                identifier=identifier,
                error="session stalled",
            )

    async def _reconcile_states(self, cfg: ServiceConfig) -> None:
        """Refresh tracker states for running issues and act on changes."""
        if not self._state.running:
            return

        # Collect issue numbers for reconciliation
        numbers: list[int] = []
        id_to_number: dict[str, int] = {}
        for issue_id, entry in self._state.running.items():
            num = _issue_number(entry.identifier)
            if num is not None:
                numbers.append(num)
                id_to_number[issue_id] = num

        if not numbers:
            return

        try:
            refreshed = await self._tracker.fetch_issues_by_numbers(numbers)
        except Exception as exc:
            logger.debug("reconcile_state_refresh_failed error=%s, keep workers running", exc)
            return

        refreshed_by_id: dict[str, Issue] = {i.id: i for i in refreshed}
        # Also index by identifier for matching
        refreshed_by_ident: dict[str, Issue] = {i.identifier: i for i in refreshed}

        for issue_id in list(self._state.running.keys()):
            entry = self._state.running.get(issue_id)
            if not entry:
                continue

            # Find the refreshed issue
            refreshed_issue = refreshed_by_id.get(issue_id)
            if not refreshed_issue:
                refreshed_issue = refreshed_by_ident.get(entry.identifier)

            if not refreshed_issue:
                continue

            state = refreshed_issue.state.lower()

            if state in cfg.terminal_states:
                logger.info("reconcile_terminal issue_id=%s state=%s", issue_id, state)
                await self._terminate_running(issue_id, cleanup_workspace=True)
            elif state in cfg.active_states:
                # Update snapshot
                entry.issue = refreshed_issue
                entry.state = state
            else:
                # Non-active, non-terminal: stop without cleanup, release claim
                logger.info("reconcile_non_active issue_id=%s state=%s", issue_id, state)
                await self._terminate_running(issue_id, cleanup_workspace=False)
                self._state.claimed.discard(issue_id)

    async def _terminate_running(self, issue_id: str, cleanup_workspace: bool) -> None:
        """Terminate a running worker and optionally clean its workspace.

        Note: this does NOT release the claim. Callers that need the claim
        released (e.g. reconciliation with no retry) must do so explicitly.
        Callers that schedule a retry keep the claim via the retry entry.
        """
        entry = self._state.running.pop(issue_id, None)
        if not entry:
            return

        # Cancel worker task
        if entry.worker_task and not entry.worker_task.done():
            entry.worker_task.cancel()
            try:
                await entry.worker_task
            except (asyncio.CancelledError, Exception):
                pass

        # Accumulate runtime
        if entry.started_at:
            elapsed = (_now_utc() - entry.started_at).total_seconds()
            self._state.copilot_totals.seconds_running += elapsed

        # Cleanup workspace if terminal; also release claim for terminal cases
        if cleanup_workspace:
            self._state.claimed.discard(issue_id)
            cfg = self._effective_config()
            if cfg:
                try:
                    await ws_mod.cleanup_workspace(cfg, entry.identifier)
                except Exception as exc:
                    logger.warning("workspace_cleanup_failed issue=%s error=%s", entry.identifier, exc)

    # --- Retry ---

    def _schedule_retry(
        self,
        issue_id: str,
        attempt: int,
        identifier: str,
        error: str | None,
        delay_ms: int | None = None,
    ) -> None:
        """Schedule a retry for an issue."""
        cfg = self._effective_config()

        # Cancel existing retry
        existing = self._state.retry_attempts.pop(issue_id, None)
        if existing and existing.timer_handle:
            existing.timer_handle.cancel()

        # Compute delay
        if delay_ms is None:
            max_backoff = cfg.max_retry_backoff_ms if cfg else 300000
            delay_ms = min(10000 * (2 ** (attempt - 1)), max_backoff)

        due_at = time.monotonic() + delay_ms / 1000.0

        timer = None
        if self._loop and self._running:
            timer = self._loop.call_later(
                delay_ms / 1000.0,
                lambda iid=issue_id: asyncio.ensure_future(self._on_retry(iid)),
            )

        self._state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at,
            timer_handle=timer,
            error=error,
        )

    async def _on_retry(self, issue_id: str) -> None:
        """Handle a retry timer firing."""
        retry = self._state.retry_attempts.pop(issue_id, None)
        if not retry:
            return

        cfg = self._effective_config()
        if not cfg or not self._tracker:
            self._state.claimed.discard(issue_id)
            return

        # Re-validate config after potential reload
        self._check_workflow_reload()
        cfg = self._effective_config()
        if not cfg or not self._tracker:
            self._state.claimed.discard(issue_id)
            return
        errors = cfg.validate_dispatch()
        if errors:
            self._schedule_retry(
                issue_id, retry.attempt + 1, retry.identifier,
                error="config validation failed",
            )
            return

        # Fetch candidates
        try:
            candidates = await self._tracker.fetch_candidate_issues()
        except Exception as exc:
            self._schedule_retry(
                issue_id, retry.attempt + 1, retry.identifier,
                error="retry poll failed",
            )
            return

        # Find the issue
        found = None
        for c in candidates:
            if c.id == issue_id:
                found = c
                break

        if not found:
            # Issue no longer a candidate – release claim
            self._state.claimed.discard(issue_id)
            logger.info("retry_release issue_id=%s (not found in candidates)", issue_id)
            return

        # Check global slots
        if self._available_slots() <= 0:
            self._schedule_retry(
                issue_id, retry.attempt + 1, found.identifier,
                error="no available orchestrator slots",
            )
            return

        # Check per-state slots
        if self._per_state_slots(found.state) <= 0:
            self._schedule_retry(
                issue_id, retry.attempt + 1, found.identifier,
                error="no available per-state slots",
            )
            return

        # Dispatch
        await self._dispatch_issue(found, attempt=retry.attempt)

    # --- Observers ---

    def add_observer(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify_observers(self) -> None:
        for cb in self._observers:
            try:
                cb()
            except Exception:
                pass

    # --- Snapshot ---

    def get_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of current runtime state for monitoring."""
        now = _now_utc()
        running_rows = []
        for entry in self._state.running.values():
            running_rows.append({
                "issue_id": entry.issue_id,
                "issue_identifier": entry.identifier,
                "state": entry.state,
                "session_id": entry.session.session_id,
                "turn_count": entry.session.turn_count,
                "last_event": entry.session.last_copilot_event,
                "last_message": entry.session.last_copilot_message,
                "started_at": entry.started_at.isoformat() if entry.started_at else None,
                "last_event_at": (
                    entry.session.last_copilot_timestamp.isoformat()
                    if entry.session.last_copilot_timestamp else None
                ),
                "tokens": {
                    "input_tokens": entry.session.copilot_input_tokens,
                    "output_tokens": entry.session.copilot_output_tokens,
                    "total_tokens": entry.session.copilot_total_tokens,
                },
            })

        retry_rows = []
        for retry in self._state.retry_attempts.values():
            due_at = datetime.fromtimestamp(
                time.time() + (retry.due_at_ms - time.monotonic()), tz=timezone.utc
            )
            retry_rows.append({
                "issue_id": retry.issue_id,
                "issue_identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at": due_at.isoformat(),
                "error": retry.error,
            })

        # Compute live seconds_running
        active_seconds = sum(
            (now - e.started_at).total_seconds()
            for e in self._state.running.values()
            if e.started_at
        )
        total_seconds = self._state.copilot_totals.seconds_running + active_seconds

        return {
            "generated_at": now.isoformat(),
            "counts": {
                "running": len(self._state.running),
                "retrying": len(self._state.retry_attempts),
            },
            "running": running_rows,
            "retrying": retry_rows,
            "copilot_totals": {
                "input_tokens": self._state.copilot_totals.input_tokens,
                "output_tokens": self._state.copilot_totals.output_tokens,
                "total_tokens": self._state.copilot_totals.total_tokens,
                "seconds_running": round(total_seconds, 1),
            },
            "rate_limits": (
                self._state.copilot_rate_limits.data
                if self._state.copilot_rate_limits else None
            ),
        }

    def get_issue_detail(self, identifier: str) -> dict[str, Any] | None:
        """Return issue-specific runtime detail."""
        # Find in running
        for entry in self._state.running.values():
            if entry.identifier == identifier:
                return {
                    "issue_identifier": entry.identifier,
                    "issue_id": entry.issue_id,
                    "status": "running",
                    "workspace": {
                        "path": ws_mod.workspace_path_for(
                            self._effective_config().workspace_root if self._effective_config() else "",
                            entry.identifier,
                        ),
                    },
                    "running": {
                        "session_id": entry.session.session_id,
                        "turn_count": entry.session.turn_count,
                        "state": entry.state,
                        "started_at": entry.started_at.isoformat() if entry.started_at else None,
                        "last_event": entry.session.last_copilot_event,
                        "last_message": entry.session.last_copilot_message,
                        "last_event_at": (
                            entry.session.last_copilot_timestamp.isoformat()
                            if entry.session.last_copilot_timestamp else None
                        ),
                        "tokens": {
                            "input_tokens": entry.session.copilot_input_tokens,
                            "output_tokens": entry.session.copilot_output_tokens,
                            "total_tokens": entry.session.copilot_total_tokens,
                        },
                    },
                    "retry": None,
                    "last_error": None,
                }

        # Find in retry
        for retry in self._state.retry_attempts.values():
            if retry.identifier == identifier:
                due_at = datetime.fromtimestamp(
                    time.time() + (retry.due_at_ms - time.monotonic()), tz=timezone.utc
                )
                return {
                    "issue_identifier": retry.identifier,
                    "issue_id": retry.issue_id,
                    "status": "retrying",
                    "workspace": {
                        "path": ws_mod.workspace_path_for(
                            self._effective_config().workspace_root if self._effective_config() else "",
                            retry.identifier,
                        ),
                    },
                    "running": None,
                    "retry": {
                        "attempt": retry.attempt,
                        "due_at": due_at.isoformat(),
                        "error": retry.error,
                    },
                    "last_error": retry.error,
                }

        return None
