"""Read-only lookups the label designer can use: saved labels, templates, print history.

Each tool returns JSON text for Claude. Nothing here saves, deletes or prints; the person
does that from the UI. Results are kept small (summaries first, full specs on request).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from .library import Library, NotFound
from .templates import TemplateStore

SPEC_LIMIT = 30_000     # characters of JSON per tool result


def _clip(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= SPEC_LIMIT else s[:SPEC_LIMIT] + ' …(truncated)'


LOOKUP_TOOLS = [
    {
        "name": "search_saved_labels",
        "description": ("Search the person's saved labels by name or printed text (empty query = most recent). "
                        "Use when they refer to existing labels ('like my spice jars', 'the rack tags')."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 25}}},
    },
    {
        "name": "get_saved_label",
        "description": "The full Job spec of one saved label, by id from search_saved_labels.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "list_templates",
        "description": "Reusable templates: name, description and the variables each one takes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_template",
        "description": "One template's full spec (label with {{variables}}), by name.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "fill_template",
        "description": ("Fill a template with values to get a ready Job: one label per row. Use for series "
                        "('asset-tag for A-200 to A-210'). Check the result with render_preview."),
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "object"}, "description": "One object of variable values per label."}},
            "required": ["name", "rows"]},
    },
    {
        "name": "search_print_history",
        "description": ("Past prints, newest first: what was printed, when, on which printer, success or error. "
                        "Use for 'the labels I printed last week' or 'same as last time'."),
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 25}}},
    },
    {
        "name": "get_print",
        "description": "The exact Job spec of one past print, by id from search_print_history.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
]

STATUS = {   # progress text for the UI
    "search_saved_labels": "Looking through your saved labels…",
    "get_saved_label": "Opening a saved label…",
    "list_templates": "Checking your templates…",
    "get_template": "Opening a template…",
    "fill_template": "Filling in a template…",
    "search_print_history": "Looking at past prints…",
    "get_print": "Opening a past print…",
}


class Lookups:
    """Runs the lookup tools against a library and template store (either may be None)."""

    def __init__(self, lib: Library | None, templates: TemplateStore | None):
        self.lib, self.templates = lib, templates

    @property
    def tools(self) -> list[dict]:
        names = set()
        if self.lib:
            names |= {"search_saved_labels", "get_saved_label", "search_print_history", "get_print"}
        if self.templates:
            names |= {"list_templates", "get_template", "fill_template"}
        return [t for t in LOOKUP_TOOLS if t["name"] in names]

    def handles(self, name: str) -> bool:
        return any(t["name"] == name for t in self.tools)

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """(result text, is_error)."""
        fn: Callable[[dict], Any] = getattr(self, f"_{name}")
        try:
            return _clip(fn(args or {})), False
        except (NotFound, KeyError) as e:
            return f"not found: {e}", True
        except (ValidationError, ValueError, TypeError) as e:
            return f"invalid: {e}", True

    # saved labels
    def _search_saved_labels(self, a):
        rows = self.lib.labels(a.get("query") or None, limit=min(int(a.get("limit") or 10), 25))
        for r in rows:
            r["text"] = _job_text(self.lib.get(r["id"])["job"])
        return rows

    def _get_saved_label(self, a):
        r = self.lib.get(int(a["id"]))
        return {"id": r["id"], "name": r["name"], "job": r["job"]}

    # templates
    def _list_templates(self, a):
        return [{"name": t.name, "description": t.description,
                 "variables": {k: v.model_dump(exclude_none=True) for k, v in t.variables.items()}}
                for t in self.templates.list()]

    def _get_template(self, a):
        return self.templates.get(str(a["name"])).model_dump(exclude_none=True)

    def _fill_template(self, a):
        t = self.templates.get(str(a["name"]))
        rows = a.get("rows") or [{}]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("rows must be a list of objects")
        if len(rows) > 200:
            raise ValueError("at most 200 rows per job")
        return t.job(rows).model_dump(mode="json", exclude_none=True)

    # history
    def _search_print_history(self, a):
        return self.lib.history(a.get("query") or None, limit=min(int(a.get("limit") or 10), 25))

    def _get_print(self, a):
        e = self.lib.print_entry(int(a["id"]))
        return {k: e[k] for k in ("id", "at", "ok", "error", "model", "tape_mm", "labels", "template", "vars", "job")}


def _job_text(job: dict) -> str:
    from .library import job_text
    return job_text(job).replace("\n", " · ")[:300]
