"""Dev mode components — mock tracker and HTTP control API routes."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("symphony.dev")


class MockTracker:
    """In-memory mock GitHub Issues tracker with HTTP control API."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.errors: dict[str, tuple[int, str]] = {}
        self._next_id = 1

    def add_issue(
        self,
        number: int | None = None,
        title: str = "Dev issue",
        state: str = "open",
        labels: list[str] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        """Add or update an issue."""
        if number is None:
            number = self._next_id
            self._next_id += 1
        elif number >= self._next_id:
            self._next_id = number + 1

        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": number * 1000,
            "node_id": f"DEV_{number}",
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "html_url": f"http://localhost/dev/issues/{number}",
            "labels": [{"name": lbl} for lbl in (labels or [])],
            "created_at": now,
            "updated_at": now,
        }
        self.issues[number] = entry
        return entry

    def set_state(self, number: int, state: str) -> bool:
        if number not in self.issues:
            return False
        self.issues[number]["state"] = state
        self.issues[number]["updated_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def remove_issue(self, number: int) -> bool:
        return self.issues.pop(number, None) is not None

    def seed(self, count: int) -> list[dict[str, Any]]:
        """Generate N synthetic issues."""
        created = []
        for i in range(count):
            issue = self.add_issue(
                title=f"Synthetic issue {self._next_id}",
                state="open",
                labels=[f"priority/{(i % 4) + 1}"],
                body=f"Auto-generated dev issue #{self._next_id}",
            )
            created.append(issue)
        return created

    def list_issues(self, state: str | None = None) -> list[dict[str, Any]]:
        issues = sorted(self.issues.values(), key=lambda x: x["number"])
        if state:
            issues = [i for i in issues if i["state"].lower() == state.lower()]
        return issues

    def get_issue(self, number: int) -> dict[str, Any] | None:
        return self.issues.get(number)

    def inject_error(self, key: str, status: int, body: str = "error") -> None:
        self.errors[key] = (status, body)

    def clear_errors(self) -> None:
        self.errors.clear()


def mount_dev_routes(app: FastAPI, tracker: MockTracker) -> None:
    """Mount mock GitHub API routes and control API on the FastAPI app."""

    # --- GitHub API compatible routes under /_dev/github/ prefix ---

    @app.get("/_dev/github/repos/{owner}/{repo}/issues")
    async def github_list_issues(owner: str, repo: str, request: Request):
        # Check for injected errors
        if "list" in tracker.errors:
            status, body = tracker.errors["list"]
            return JSONResponse({"message": body}, status_code=status)

        params = dict(request.query_params)
        state = params.get("state", "open")
        per_page = int(params.get("per_page", "50"))
        page = int(params.get("page", "1"))

        issues = tracker.list_issues(state)
        start = (page - 1) * per_page
        page_data = issues[start : start + per_page]
        return JSONResponse(page_data)

    @app.get("/_dev/github/repos/{owner}/{repo}/issues/{number}")
    async def github_get_issue(owner: str, repo: str, number: int):
        if f"issue:{number}" in tracker.errors:
            status, body = tracker.errors[f"issue:{number}"]
            return JSONResponse({"message": body}, status_code=status)

        issue = tracker.get_issue(number)
        if issue is None:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        return JSONResponse(issue)

    # --- Control API (CLI sidecar talks to these) ---

    @app.post("/dev/issues")
    async def dev_create_issue(request: Request):
        data = await request.json()
        issue = tracker.add_issue(
            number=data.get("number"),
            title=data.get("title", "Untitled"),
            state=data.get("state", "open"),
            labels=data.get("labels"),
            body=data.get("body", ""),
        )
        return JSONResponse(issue, status_code=201)

    @app.patch("/dev/issues/{number}")
    async def dev_update_issue(number: int, request: Request):
        data = await request.json()
        if number not in tracker.issues:
            return JSONResponse({"error": "not found"}, status_code=404)
        if "state" in data:
            tracker.set_state(number, data["state"])
        if "title" in data:
            tracker.issues[number]["title"] = data["title"]
        if "labels" in data:
            tracker.issues[number]["labels"] = [
                {"name": lbl} for lbl in data["labels"]
            ]
        return JSONResponse(tracker.issues[number])

    @app.delete("/dev/issues/{number}")
    async def dev_delete_issue(number: int):
        if tracker.remove_issue(number):
            return JSONResponse({"deleted": True})
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/dev/issues")
    async def dev_list_all_issues():
        return JSONResponse(tracker.list_issues())

    @app.post("/dev/issues/seed")
    async def dev_seed_issues(request: Request):
        data = await request.json()
        count = data.get("count", 5)
        created = tracker.seed(count)
        return JSONResponse({"created": len(created), "issues": created})

    @app.post("/dev/errors")
    async def dev_inject_error(request: Request):
        data = await request.json()
        tracker.inject_error(data["key"], data["status"], data.get("body", "error"))
        return JSONResponse({"injected": True})

    @app.delete("/dev/errors")
    async def dev_clear_errors():
        tracker.clear_errors()
        return JSONResponse({"cleared": True})


def generate_instance_id() -> str:
    """Generate a short random instance ID."""
    return uuid.uuid4().hex[:8]


def dev_workspace_root(base_root: str, instance_id: str) -> str:
    """Compute isolated workspace root for a dev instance."""
    return os.path.join(base_root, f"_dev_{instance_id}")


def write_port_file(workspace_root: str, port: int) -> None:
    """Write the actual port to a discovery file."""
    os.makedirs(workspace_root, exist_ok=True)
    port_file = os.path.join(workspace_root, ".symphony-dev.port")
    with open(port_file, "w") as f:
        f.write(str(port))


def write_pid_file(workspace_root: str) -> None:
    """Write PID lock file. Raises if another instance is alive."""
    os.makedirs(workspace_root, exist_ok=True)
    pid_file = os.path.join(workspace_root, ".symphony-dev.pid")

    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            # Check if process is still alive
            os.kill(old_pid, 0)
            raise RuntimeError(
                f"Another dev instance is already running (PID {old_pid}). "
                f"Use a different --instance name or stop the existing instance."
            )
        except (OSError, ValueError):
            pass  # Process is dead or file is corrupt — safe to overwrite

    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pid_file(workspace_root: str) -> None:
    """Remove PID lock file on shutdown."""
    pid_file = os.path.join(workspace_root, ".symphony-dev.pid")
    try:
        os.unlink(pid_file)
    except OSError:
        pass
