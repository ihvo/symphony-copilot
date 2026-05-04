"""Tests for prompt rendering (SPEC §12, §17.1)."""

from __future__ import annotations

import pytest

from symphony.errors import TemplateParseError, TemplateRenderError
from symphony.prompt import render_prompt


class TestPromptRendering:
    def test_simple_issue_variables(self):
        template = "Work on {{ issue.identifier }}: {{ issue.title }}"
        result = render_prompt(template, {"identifier": "#42", "title": "Fix bug"})
        assert result == "Work on #42: Fix bug"

    def test_attempt_variable(self):
        template = "{% if attempt %}Retry #{{ attempt }}{% endif %}"
        result = render_prompt(template, {"identifier": "#1", "title": "t"}, attempt=3)
        assert result == "Retry #3"

    def test_attempt_null_first_run(self):
        template = "{% if attempt %}retry{% else %}first{% endif %}"
        result = render_prompt(template, {"identifier": "#1", "title": "t"}, attempt=None)
        assert result == "first"

    def test_labels_iteration(self):
        template = "Labels: {% for l in issue.labels %}{{ l }} {% endfor %}"
        result = render_prompt(
            template, {"labels": ["bug", "p1"], "identifier": "#1", "title": "t"}
        )
        assert "bug" in result
        assert "p1" in result

    def test_blockers_iteration(self):
        template = "{% for b in issue.blocked_by %}{{ b.identifier }} {% endfor %}"
        result = render_prompt(
            template,
            {
                "blocked_by": [{"identifier": "#10", "state": "open"}],
                "identifier": "#1",
                "title": "t",
            },
        )
        assert "#10" in result

    def test_unknown_variable_fails(self):
        template = "{{ unknown_var }}"
        with pytest.raises(TemplateRenderError):
            render_prompt(template, {"identifier": "#1", "title": "t"})

    def test_unknown_nested_variable_fails(self):
        template = "{{ issue.nonexistent_field }}"
        with pytest.raises(TemplateRenderError):
            render_prompt(template, {"identifier": "#1", "title": "t"})

    def test_invalid_template_syntax(self):
        template = "{% if unclosed"
        with pytest.raises(TemplateParseError):
            render_prompt(template, {"identifier": "#1", "title": "t"})

    def test_empty_template_fallback(self):
        result = render_prompt("", {"identifier": "#1", "title": "t"})
        assert result == "You are working on a GitHub issue."

    def test_whitespace_only_template_fallback(self):
        result = render_prompt("   \n  ", {"identifier": "#1", "title": "t"})
        assert result == "You are working on a GitHub issue."

    def test_all_issue_fields(self):
        template = (
            "ID={{ issue.id }} IDENT={{ issue.identifier }} TITLE={{ issue.title }} "
            "STATE={{ issue.state }} URL={{ issue.url }} DESC={{ issue.description }}"
        )
        result = render_prompt(
            template,
            {
                "id": "abc",
                "identifier": "#1",
                "title": "Test",
                "state": "open",
                "url": "https://example.com",
                "description": "A test issue",
            },
        )
        assert "ID=abc" in result
        assert "IDENT=#1" in result
        assert "STATE=open" in result
