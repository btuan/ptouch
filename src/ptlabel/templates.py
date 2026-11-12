"""Label templates: a Label spec with {{variables}}, stored as JSON files.

A template is filled with one dict of values (one label) or many (a batch job of
half-cut labels — the tape-efficient way to print a series like asset tags).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .spec import Job, Label

VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "templates"


class Variable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    default: str | None = None
    example: str | None = None


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME.pattern)
    description: str = ""
    variables: dict[str, Variable] = Field(default_factory=dict)
    label: dict[str, Any] = Field(description="A Label spec; any string may contain {{var}}.")

    def referenced(self) -> set[str]:
        return set(VAR.findall(json.dumps(self.label)))

    def check(self) -> None:
        """Validate that declared and used variables match and the example renders as a Label."""
        used, declared = self.referenced(), set(self.variables)
        if missing := used - declared:
            raise ValueError(f"template uses undeclared variables: {sorted(missing)}")
        self.fill({k: v.example or v.default or k for k, v in self.variables.items()})

    def fill(self, values: dict[str, Any]) -> Label:
        vals = {k: v.default for k, v in self.variables.items() if v.default is not None}
        vals.update({k: str(v) for k, v in values.items()})
        if missing := self.referenced() - set(vals):
            raise ValueError(f"missing values for: {sorted(missing)}")

        def sub(x):
            if isinstance(x, str):
                return VAR.sub(lambda m: vals[m.group(1)], x)
            if isinstance(x, list):
                return [sub(i) for i in x]
            if isinstance(x, dict):
                return {k: sub(v) for k, v in x.items()}
            return x
        return Label.model_validate(sub(self.label))

    def job(self, rows: list[dict[str, Any]], **job_opts) -> Job:
        return Job(labels=[self.fill(r) for r in rows], **job_opts)


class TemplateStore:
    def __init__(self, root: Path | str = DEFAULT_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not NAME.match(name):
            raise ValueError(f"invalid template name {name!r}")
        return self.root / f"{name}.json"

    def list(self) -> list[Template]:
        return [self.get(p.stem) for p in sorted(self.root.glob("*.json"))]

    def get(self, name: str) -> Template:
        p = self._path(name)
        if not p.is_file():
            raise KeyError(name)
        return Template.model_validate_json(p.read_text())

    def put(self, t: Template) -> Template:
        t.check()
        self._path(t.name).write_text(t.model_dump_json(indent=2, exclude_defaults=False) + "\n")
        return t

    def delete(self, name: str) -> None:
        p = self._path(name)
        if not p.is_file():
            raise KeyError(name)
        p.unlink()
