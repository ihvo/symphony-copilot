"""WORKFLOW.md loader – parses YAML front matter and prompt body."""

from __future__ import annotations

import os

import yaml

from symphony.errors import (
    MissingWorkflowFileError,
    WorkflowFrontMatterNotAMapError,
    WorkflowParseError,
)
from symphony.models import WorkflowDefinition

_FRONT_MATTER_DELIMITER = "---"


def resolve_workflow_path(explicit_path: str | None = None) -> str:
    """Resolve the workflow file path.

    Precedence:
    1. Explicit path (CLI / runtime setting).
    2. Default: ``WORKFLOW.md`` in the current working directory.
    """
    if explicit_path:
        return os.path.abspath(explicit_path)
    return os.path.abspath("WORKFLOW.md")


def load_workflow(path: str) -> WorkflowDefinition:
    """Load and parse a ``WORKFLOW.md`` file.

    Returns a :class:`WorkflowDefinition` with *config* (front matter map)
    and *prompt_template* (trimmed body).

    Raises
    ------
    MissingWorkflowFileError
        File does not exist or cannot be read.
    WorkflowParseError
        YAML cannot be decoded.
    WorkflowFrontMatterNotAMapError
        Decoded YAML is not a mapping.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raise MissingWorkflowFileError(path) from None
    except OSError as exc:
        raise MissingWorkflowFileError(path) from exc

    return _parse_workflow(raw)


def _parse_workflow(raw: str) -> WorkflowDefinition:
    lines = raw.split("\n")
    config: dict = {}
    body_start = 0

    if lines and lines[0].strip() == _FRONT_MATTER_DELIMITER:
        end_idx: int | None = None
        for i in range(1, len(lines)):
            if lines[i].strip() == _FRONT_MATTER_DELIMITER:
                end_idx = i
                break
        if end_idx is not None:
            yaml_block = "\n".join(lines[1:end_idx])
            try:
                parsed = yaml.safe_load(yaml_block)
            except yaml.YAMLError as exc:
                raise WorkflowParseError(str(exc)) from exc
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict):
                raise WorkflowFrontMatterNotAMapError()
            config = parsed
            body_start = end_idx + 1

    prompt_template = "\n".join(lines[body_start:]).strip()
    return WorkflowDefinition(config=config, prompt_template=prompt_template)


def get_workflow_mtime(path: str) -> float | None:
    """Return mtime of the workflow file, or None if it doesn't exist."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None
