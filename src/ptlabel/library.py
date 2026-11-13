"""Saved labels and print history, in one SQLite file.

PTLABEL_DB sets the path (default: ~/.local/share/ptlabel/ptlabel.db; /data/ptlabel.db in
Docker). Jobs are stored as the exact JSON spec, so anything saved or printed can be
reloaded, edited or reprinted. Templates stay as JSON files (see templates.py).
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image

SCHEMA = 2
DDL = """
CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    job         TEXT NOT NULL,          -- Job spec JSON
    origin      TEXT,                   -- manual | claude | template:<name> | history
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    thumb       BLOB                    -- small PNG
);
CREATE TABLE IF NOT EXISTS prints (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    printer     TEXT,                   -- registry key, e.g. e560bt
    model       TEXT,                   -- PT-E560BT
    connection  TEXT,                   -- USB / IP address
    tape_mm     INTEGER,
    labels      INTEGER,
    length_mm   REAL,
    job         TEXT NOT NULL,          -- exactly what was sent
    source      TEXT,                   -- api | cli | reprint
    label_id    INTEGER REFERENCES labels(id) ON DELETE SET NULL,
    template    TEXT,
    vars        TEXT,                   -- JSON: template values / rows
    reprint_of  INTEGER REFERENCES prints(id) ON DELETE SET NULL,
    thumb       BLOB
);
CREATE INDEX IF NOT EXISTS prints_at ON prints(at DESC);
CREATE INDEX IF NOT EXISTS labels_updated ON labels(updated_at DESC);
"""


def default_path() -> Path:
    if (p := os.environ.get("PTLABEL_DB")) and p.lower() not in ("off", "0", "none"):
        return Path(p)
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "ptlabel" / "ptlabel.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def thumbnail(png_or_image, max_w: int = 480, max_h: int = 96) -> bytes:
    im = png_or_image if isinstance(png_or_image, Image.Image) else Image.open(io.BytesIO(png_or_image))
    im = im.convert("RGBA")          # keep the transparent space between rows (full cuts)
    im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class NotFound(KeyError):
    pass


class Library:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(DDL)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:       # v2: soft delete + plain-text search column
                cols = {r[1] for r in db.execute("PRAGMA table_info(labels)")}
                if "deleted_at" not in cols:
                    db.execute("ALTER TABLE labels ADD COLUMN deleted_at TEXT")
                for table in ("labels", "prints"):
                    if "text" not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN text TEXT")
                    for row in db.execute(f"SELECT id, job FROM {table}").fetchall():
                        db.execute(f"UPDATE {table} SET text = ? WHERE id = ?",
                                   (job_text(json.loads(row[1])), row[0]))
            db.execute(f"PRAGMA user_version = {SCHEMA}")
            # deleted labels are kept 30 days so Undo (and mistakes) can be recovered
            db.execute("DELETE FROM labels WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                       ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------- saved labels

    @staticmethod
    def _label(r: sqlite3.Row, full: bool = True) -> dict:
        d = {"id": r["id"], "name": r["name"], "origin": r["origin"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]}
        job = json.loads(r["job"])
        d["labels"] = len(job.get("labels", []))
        if full:
            d["job"] = job
        return d

    @staticmethod
    def _unique(db, name: str, exclude_id: int | None = None) -> str:
        """`name`, or `name (2)`, `name (3)`, ... if another saved label already uses it."""
        taken = {r[0] for r in db.execute("SELECT name FROM labels WHERE deleted_at IS NULL AND id IS NOT ?",
                                          (exclude_id,))}
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def save(self, name: str, job: dict, origin: str | None = None, thumb: bytes | None = None) -> dict:
        now = _now()
        with self._db() as db:
            name = self._unique(db, name)
            cur = db.execute("INSERT INTO labels(name, job, origin, created_at, updated_at, thumb, text) "
                             "VALUES (?,?,?,?,?,?,?)", (name, json.dumps(job), origin, now, now, thumb,
                                                        job_text(job)))
            return self.get(cur.lastrowid, db)

    def update(self, label_id: int, name: str | None = None, job: dict | None = None,
               thumb: bytes | None = None) -> dict:
        with self._db() as db:
            self.get(label_id, db)
            sets, args = ["updated_at = ?"], [_now()]
            if name is not None:
                sets.append("name = ?"); args.append(self._unique(db, name, label_id))
            if job is not None:
                sets.append("job = ?"); args.append(json.dumps(job))
                sets.append("text = ?"); args.append(job_text(job))
            if thumb is not None:
                sets.append("thumb = ?"); args.append(thumb)
            db.execute(f"UPDATE labels SET {', '.join(sets)} WHERE id = ?", (*args, label_id))
            return self.get(label_id, db)

    def get(self, label_id: int, db=None) -> dict:
        if db is None:
            with self._db() as db:
                return self.get(label_id, db)
        r = db.execute("SELECT * FROM labels WHERE id = ? AND deleted_at IS NULL", (label_id,)).fetchone()
        if not r:
            raise NotFound(label_id)
        return self._label(r)

    def labels(self, q: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        sql, args = "SELECT * FROM labels WHERE deleted_at IS NULL", []
        if q:
            sql += r" AND (name LIKE ? ESCAPE '\' OR text LIKE ? ESCAPE '\')"
            args += [_like(q)] * 2
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        with self._db() as db:
            return [self._label(r, full=False) for r in db.execute(sql, (*args, limit, offset))]

    def delete(self, label_id: int) -> None:
        """Soft delete: hidden at once, restorable for 30 days (restore())."""
        with self._db() as db:
            if not db.execute("UPDATE labels SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                              (_now(), label_id)).rowcount:
                raise NotFound(label_id)

    def restore(self, label_id: int) -> dict:
        with self._db() as db:
            if not db.execute("UPDATE labels SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
                              (label_id,)).rowcount:
                raise NotFound(label_id)
            return self.get(label_id, db)

    def thumb(self, table: str, row_id: int) -> bytes | None:
        assert table in ("labels", "prints")
        with self._db() as db:
            r = db.execute(f"SELECT thumb FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not r:
            raise NotFound(row_id)
        return r["thumb"]

    # ------------------------------------------------------------- print history

    def record(self, *, job: dict, ok: bool, error: str | None = None, printer: Any = None,
               result: dict | None = None, source: str = "api", label_id: int | None = None,
               template: str | None = None, vars: Any = None, reprint_of: int | None = None,
               thumb: bytes | None = None) -> int:
        result = result or {}
        m = getattr(printer, "model", None)
        with self._db() as db:
            if label_id is not None and not db.execute("SELECT 1 FROM labels WHERE id = ?", (label_id,)).fetchone():
                label_id = None
            if reprint_of is not None and not db.execute("SELECT 1 FROM prints WHERE id = ?", (reprint_of,)).fetchone():
                reprint_of = None
            cur = db.execute(
                "INSERT INTO prints(at, ok, error, printer, model, connection, tape_mm, labels, length_mm,"
                " job, source, label_id, template, vars, reprint_of, thumb, text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), int(ok), error, getattr(printer, "key", None), m.name if m else None,
                 getattr(printer, "where", None), result.get("tape_mm"), result.get("labels"),
                 result.get("length_mm"), json.dumps(job), source, label_id, template,
                 json.dumps(vars) if vars is not None else None, reprint_of, thumb, job_text(job)))
            return cur.lastrowid

    @staticmethod
    def _print(r: sqlite3.Row, full: bool = False) -> dict:
        d = {k: r[k] for k in ("id", "at", "error", "printer", "model", "connection", "tape_mm",
                               "labels", "length_mm", "source", "label_id", "template", "reprint_of")}
        d["ok"] = bool(r["ok"])
        d["vars"] = json.loads(r["vars"]) if r["vars"] else None
        job = json.loads(r["job"])
        d["summary"] = _summary(job)
        if full:
            d["job"] = job
        return d

    def history(self, q: str | None = None, limit: int = 50, offset: int = 0,
                printer: str | None = None, ok: bool | None = None) -> list[dict]:
        where, args = [], []
        if q:
            where.append(r"(text LIKE ? ESCAPE '\' OR template LIKE ? ESCAPE '\' OR vars LIKE ? ESCAPE '\')")
            args += [_like(q)] * 3
        if printer:
            where.append("printer = ?"); args.append(printer)
        if ok is not None:
            where.append("ok = ?"); args.append(int(ok))
        sql = "SELECT * FROM prints" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY at DESC, id DESC LIMIT ? OFFSET ?"
        with self._db() as db:
            return [self._print(r) for r in db.execute(sql, (*args, limit, offset))]

    def print_entry(self, print_id: int) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()
        if not r:
            raise NotFound(print_id)
        return self._print(r, full=True)

    def stats(self) -> dict:
        with self._db() as db:
            r = db.execute("SELECT COUNT(*) n, COALESCE(SUM(labels),0) labels, "
                           "COALESCE(SUM(length_mm),0) mm FROM prints WHERE ok = 1").fetchone()
            per = db.execute("SELECT printer, tape_mm, COUNT(*) n, COALESCE(SUM(length_mm),0) mm "
                             "FROM prints WHERE ok = 1 GROUP BY printer, tape_mm ORDER BY mm DESC").fetchall()
            saved = db.execute("SELECT COUNT(*) FROM labels WHERE deleted_at IS NULL").fetchone()[0]
        return {"prints": r["n"], "labels_printed": r["labels"], "tape_used_mm": round(r["mm"], 1),
                "saved_labels": saved,
                "by_printer": [{"printer": p["printer"], "tape_mm": p["tape_mm"], "prints": p["n"],
                                "tape_used_mm": round(p["mm"], 1)} for p in per]}


def _like(q: str) -> str:
    """LIKE pattern matching `q` literally (so % and _ aren't wildcards)."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _strings(job: dict, key: str) -> list[str]:
    return [" ".join(str(it[key]).split()) for lab in job.get("labels", [])
            for it in lab.get("items", []) if it.get(key)]


def job_text(job: dict) -> str:
    """Searchable text of a job: what's printed as text, then code/barcode data."""
    return "\n".join(dict.fromkeys(_strings(job, "text") + _strings(job, "data")))


def _summary(job: dict) -> str:
    """Short human text for a job: its text items, else code data, else '(no text)'."""
    words = _strings(job, "text") or _strings(job, "data")
    s = " · ".join(dict.fromkeys(words)) or "(no text)"
    return s if len(s) <= 80 else s[:79] + "…"


def job_thumb(job, tape_mm: int, model) -> bytes | None:
    """Small preview PNG of a Job for the library; None if it doesn't render."""
    from .printer import compile_job, cuts_between
    from .render import preview
    try:
        _, images = compile_job(job, tape_mm, model)
        images = images[:len(job.labels)]          # one set; copies would just repeat it
        return thumbnail(preview(images, tape_mm, job.hires, cuts_between(job, model)[:len(images) - 1], job.margin_mm,
                                 frame=False, model=model, scale=1))
    except Exception:
        return None


def _planned(job, printer) -> dict:
    """What a failed print would have been (labels, length), so history can show it."""
    from .printer import compile_job, label_length_mm
    try:
        tape = job.resolved_tape(12)
        _, images = compile_job(job, tape, printer.model)
        return {"tape_mm": tape, "labels": len(images),
                "length_mm": round(label_length_mm(images, job, printer.model.dpi), 1)}
    except Exception:
        return {"labels": len(job.expanded_labels())}


def print_and_record(lib: "Library | None", printer, job, *, source: str, label_id: int | None = None,
                     template: str | None = None, vars: Any = None, reprint_of: int | None = None) -> dict:
    """printer.print(job), logging success or failure to the history (if `lib`).
    The result gains "history_id"."""
    spec = job.model_dump(mode="json", exclude_none=True)
    try:
        result = printer.print(job)
    except Exception as e:
        if lib:
            lib.record(job=spec, ok=False, error=str(e), printer=printer, result=_planned(job, printer),
                       source=source, label_id=label_id, template=template, vars=vars, reprint_of=reprint_of)
        raise
    if lib:
        thumb = job_thumb(job, result.get("tape_mm") or 12, printer.model)
        result["history_id"] = lib.record(job=spec, ok=True, printer=printer, result=result, source=source,
                                          label_id=label_id, template=template, vars=vars,
                                          reprint_of=reprint_of, thumb=thumb)
    return result


def open_default() -> "Library | None":
    """The library, or None when PTLABEL_DB=off (history disabled)."""
    if os.environ.get("PTLABEL_DB", "").lower() in ("off", "0", "none"):
        return None
    return Library()
