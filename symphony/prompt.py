"""Strict prompt template rendering."""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError

from symphony.errors import TemplateParseError, TemplateRenderError

_DEFAULT_PROMPT = "You are working on a GitHub issue."

_env = Environment(undefined=StrictUndefined)


def render_prompt(
    template_str: str,
    issue: dict[str, Any],
    attempt: int | None = None,
) -> str:
    """Render a prompt template with ``issue`` and ``attempt`` variables.

    Uses Jinja2 with :class:`StrictUndefined` so unknown variables and
    filters cause an immediate error rather than silently producing empty
    strings.

    If *template_str* is empty, falls back to a minimal default prompt.
    """
    if not template_str.strip():
        return _DEFAULT_PROMPT

    try:
        tmpl = _env.from_string(template_str)
    except TemplateSyntaxError as exc:
        raise TemplateParseError(str(exc)) from exc

    try:
        return tmpl.render(issue=issue, attempt=attempt)
    except UndefinedError as exc:
        raise TemplateRenderError(str(exc)) from exc
    except Exception as exc:
        raise TemplateRenderError(str(exc)) from exc
