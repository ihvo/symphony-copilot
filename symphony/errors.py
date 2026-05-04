"""Typed error classes for Symphony."""

from __future__ import annotations


class SymphonyError(Exception):
    """Base error for all Symphony errors."""

    code: str = "symphony_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


# --- Workflow / Config errors ---


class MissingWorkflowFileError(SymphonyError):
    code = "missing_workflow_file"

    def __init__(self, path: str) -> None:
        super().__init__(f"Workflow file not found: {path}")
        self.path = path


class WorkflowParseError(SymphonyError):
    code = "workflow_parse_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"Failed to parse workflow file: {detail}")


class WorkflowFrontMatterNotAMapError(SymphonyError):
    code = "workflow_front_matter_not_a_map"

    def __init__(self) -> None:
        super().__init__("Workflow YAML front matter must be a mapping (dict), not a scalar or list")


class TemplateParseError(SymphonyError):
    code = "template_parse_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"Template parse error: {detail}")


class TemplateRenderError(SymphonyError):
    code = "template_render_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"Template render error: {detail}")


# --- Config validation errors ---


class ConfigValidationError(SymphonyError):
    code = "config_validation_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"Config validation failed: {detail}")


# --- Tracker errors ---


class UnsupportedTrackerKindError(SymphonyError):
    code = "unsupported_tracker_kind"

    def __init__(self, kind: str) -> None:
        super().__init__(f"Unsupported tracker kind: {kind!r}")


class MissingTrackerApiKeyError(SymphonyError):
    code = "missing_tracker_api_key"

    def __init__(self) -> None:
        super().__init__("Tracker API key is missing after $VAR resolution")


class MissingTrackerRepoError(SymphonyError):
    code = "missing_tracker_repo"

    def __init__(self) -> None:
        super().__init__("tracker.repo is required when tracker.kind is 'github'")


class GitHubApiRequestError(SymphonyError):
    code = "github_api_request"

    def __init__(self, detail: str) -> None:
        super().__init__(f"GitHub API request failed: {detail}")


class GitHubApiStatusError(SymphonyError):
    code = "github_api_status"

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"GitHub API returned HTTP {status}: {body}")
        self.status = status


class GitHubApiErrorsError(SymphonyError):
    code = "github_api_errors"

    def __init__(self, errors: list) -> None:
        super().__init__(f"GitHub API returned errors: {errors}")
        self.errors = errors


class GitHubUnknownPayloadError(SymphonyError):
    code = "github_unknown_payload"

    def __init__(self, detail: str) -> None:
        super().__init__(f"GitHub API returned unknown payload: {detail}")


class GitHubMissingPaginationError(SymphonyError):
    code = "github_missing_pagination"

    def __init__(self) -> None:
        super().__init__("GitHub API pagination integrity error")


# --- Workspace errors ---


class WorkspaceError(SymphonyError):
    code = "workspace_error"


class InvalidWorkspacePathError(WorkspaceError):
    code = "invalid_workspace_path"

    def __init__(self, path: str, root: str) -> None:
        super().__init__(f"Workspace path {path!r} is outside root {root!r}")


class HookError(SymphonyError):
    code = "hook_error"

    def __init__(self, hook_name: str, detail: str) -> None:
        super().__init__(f"Hook '{hook_name}' failed: {detail}")
        self.hook_name = hook_name


class HookTimeoutError(HookError):
    code = "hook_timeout"

    def __init__(self, hook_name: str, timeout_ms: int) -> None:
        super().__init__(hook_name, f"timed out after {timeout_ms}ms")
        self.timeout_ms = timeout_ms


# --- Agent / Copilot errors ---


class AgentError(SymphonyError):
    code = "agent_error"


class AgentNotFoundError(AgentError):
    """The agent binary/SDK was not found."""

    code = "agent_not_found"

    def __init__(self, harness: str, detail: str = "") -> None:
        self.harness = harness
        super().__init__(f"{harness} agent not found: {detail}")


class AgentStartupError(AgentError):
    """The agent subprocess failed to start or connect."""

    code = "agent_startup_failed"

    def __init__(self, harness: str, detail: str = "") -> None:
        self.harness = harness
        super().__init__(f"{harness} agent startup failed: {detail}")


class CopilotNotFoundError(AgentError):
    code = "copilot_not_found"

    def __init__(self, command: str) -> None:
        super().__init__(f"Copilot command not found: {command!r}")


class InvalidWorkspaceCwdError(AgentError):
    code = "invalid_workspace_cwd"

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Expected cwd {expected!r}, got {actual!r}")


class ResponseTimeoutError(AgentError):
    code = "response_timeout"


class TurnTimeoutError(AgentError):
    code = "turn_timeout"

    def __init__(self) -> None:
        super().__init__("Coding agent turn timed out")


class PortExitError(AgentError):
    code = "port_exit"

    def __init__(self, exit_code: int | None) -> None:
        super().__init__(f"Agent subprocess exited with code {exit_code}")
        self.exit_code = exit_code


class ResponseError(AgentError):
    code = "response_error"


class TurnFailedError(AgentError):
    code = "turn_failed"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"Agent turn failed: {detail}")


class TurnCancelledError(AgentError):
    code = "turn_cancelled"

    def __init__(self) -> None:
        super().__init__("Agent turn was cancelled")


class TurnInputRequiredError(AgentError):
    code = "turn_input_required"

    def __init__(self) -> None:
        super().__init__("Agent requested user input (not supported in automated mode)")


# --- Observability errors ---


class SnapshotTimeoutError(SymphonyError):
    code = "snapshot_timeout"

    def __init__(self) -> None:
        super().__init__("Runtime snapshot timed out")


class SnapshotUnavailableError(SymphonyError):
    code = "snapshot_unavailable"

    def __init__(self) -> None:
        super().__init__("Runtime snapshot unavailable")
