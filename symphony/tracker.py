"""GitHub Issues tracker client."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from symphony.errors import (
    GitHubApiErrorsError,
    GitHubApiRequestError,
    GitHubApiStatusError,
    GitHubUnknownPayloadError,
    MissingTrackerApiKeyError,
    MissingTrackerRepoError,
    UnsupportedTrackerKindError,
)
from symphony.models import BlockerRef, Issue

logger = logging.getLogger("symphony.tracker")

_PAGE_SIZE = 50
_NETWORK_TIMEOUT = 30


def _parse_iso(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        if isinstance(val, str):
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    return None


def _parse_priority(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _normalize_issue(node: dict[str, Any]) -> Issue:
    """Normalize a GitHub Issues REST/GraphQL node to the domain Issue model."""
    labels_raw = node.get("labels") or []
    if isinstance(labels_raw, list):
        labels = []
        for lbl in labels_raw:
            if isinstance(lbl, dict):
                labels.append(str(lbl.get("name", "")).lower())
            elif isinstance(lbl, str):
                labels.append(lbl.lower())
    else:
        labels = []

    # Priority: look in labels for priority/N patterns
    priority = _parse_priority(node.get("priority"))
    if priority is None:
        for lbl in labels:
            if lbl.startswith("priority/"):
                priority = _parse_priority(lbl.split("/", 1)[1])
                if priority is not None:
                    break

    return Issue(
        id=str(node.get("node_id") or node.get("id", "")),
        identifier=f"#{node.get('number', '')}",
        title=str(node.get("title", "")),
        description=node.get("body"),
        priority=priority,
        state=str(node.get("state", "")).lower(),
        branch_name=None,
        url=node.get("html_url"),
        labels=labels,
        blocked_by=[],
        created_at=_parse_iso(node.get("created_at")),
        updated_at=_parse_iso(node.get("updated_at")),
    )


class GitHubTrackerClient:
    """Issue tracker client for GitHub Issues (REST API)."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        repo: str,
        active_states: list[str] | None = None,
        terminal_states: list[str] | None = None,
    ) -> None:
        if not api_key:
            raise MissingTrackerApiKeyError()
        if not repo:
            raise MissingTrackerRepoError()
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._repo = repo
        self._active_states = [s.lower() for s in (active_states or ["open"])]
        self._terminal_states = [s.lower() for s in (terminal_states or ["closed"])]
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=_NETWORK_TIMEOUT)
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=timeout,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def update_config(
        self,
        endpoint: str,
        api_key: str,
        repo: str,
        active_states: list[str],
        terminal_states: list[str],
    ) -> None:
        """Update config for dynamic reload."""
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._repo = repo
        self._active_states = [s.lower() for s in active_states]
        self._terminal_states = [s.lower() for s in terminal_states]
        # Recreate session to pick up new auth
        if self._session and not self._session.closed:
            # Will be recreated on next use
            import asyncio
            asyncio.ensure_future(self._session.close())
            self._session = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        session = await self._get_session()
        try:
            async with session.request(method, url, **kwargs) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise GitHubApiStatusError(resp.status, body[:500])
                try:
                    import json
                    return json.loads(body)
                except Exception:
                    raise GitHubUnknownPayloadError(body[:200])
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise GitHubApiRequestError(str(exc)) from exc

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Fetch issues in active states with pagination."""
        all_issues: list[Issue] = []
        for state in self._active_states:
            page = 1
            while True:
                url = (
                    f"{self._endpoint}/repos/{self._repo}/issues"
                    f"?state={state}&per_page={_PAGE_SIZE}&page={page}"
                    f"&sort=created&direction=asc"
                )
                data = await self._request("GET", url)
                if not isinstance(data, list):
                    raise GitHubUnknownPayloadError("Expected list of issues")
                for node in data:
                    # Skip pull requests (GitHub returns PRs as issues)
                    if node.get("pull_request"):
                        continue
                    all_issues.append(_normalize_issue(node))
                if len(data) < _PAGE_SIZE:
                    break
                page += 1
        return all_issues

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Fetch issues in specific states (used for startup terminal cleanup)."""
        if not state_names:
            return []
        all_issues: list[Issue] = []
        for state in state_names:
            page = 1
            while True:
                url = (
                    f"{self._endpoint}/repos/{self._repo}/issues"
                    f"?state={state.lower()}&per_page={_PAGE_SIZE}&page={page}"
                    f"&sort=created&direction=asc"
                )
                data = await self._request("GET", url)
                if not isinstance(data, list):
                    break
                for node in data:
                    if node.get("pull_request"):
                        continue
                    all_issues.append(_normalize_issue(node))
                if len(data) < _PAGE_SIZE:
                    break
                page += 1
        return all_issues

    async def fetch_issue_states_by_ids(
        self,
        issue_ids: list[str],
        id_to_number: dict[str, int] | None = None,
    ) -> list[Issue]:
        """Fetch current state for specific issues.

        Since GitHub REST API uses issue numbers (not node IDs), callers
        SHOULD pass *id_to_number* mapping ``{issue_id: number}``.  If the
        mapping is absent, numbers are extracted from identifiers stored on
        the returned issues.
        """
        if not issue_ids:
            return []
        if not id_to_number:
            id_to_number = {}

        numbers_to_fetch: list[int] = []
        for iid in issue_ids:
            num = id_to_number.get(iid)
            if num is not None:
                numbers_to_fetch.append(num)

        if not numbers_to_fetch:
            return []

        return await self.fetch_issues_by_numbers(numbers_to_fetch)

    async def fetch_issue_by_number(self, number: int) -> Issue:
        """Fetch a single issue by number."""
        url = f"{self._endpoint}/repos/{self._repo}/issues/{number}"
        data = await self._request("GET", url)
        if not isinstance(data, dict):
            raise GitHubUnknownPayloadError("Expected issue object")
        return _normalize_issue(data)

    async def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]:
        """Fetch issues by their numbers for reconciliation."""
        results: list[Issue] = []
        for num in numbers:
            try:
                issue = await self.fetch_issue_by_number(num)
                results.append(issue)
            except Exception as exc:
                logger.warning("Failed to fetch issue #%d: %s", num, exc)
        return results
