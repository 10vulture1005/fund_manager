"""Structured event logging + JSON logging + optional Telegram alerts (spec 3.5).

- `EventLogger` writes one `events` table row per decision with the full
  feature snapshot (ts, algo, kind, symbol, message, features JSON).
- `setup_logging` wires stdlib logging to emit one JSON object per line.
- Telegram notifications via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars,
  fail-soft (network errors never break trading).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .state import DeltaSchema, now_iso

_JSON_FIELDS = ("features",)


class EventLogger:
    """Writes EventNote-flavoured rows into `{prefix}events` (schema per spec
    3.5: ts, algo, kind, symbol, message, features-json)."""

    def __init__(self, db: DeltaSchema, algo: str = "fm") -> None:
        self.db = db
        self.algo = algo

    def log(
        self,
        kind: str,
        message: str,
        symbol: str | None = None,
        features: dict[str, Any] | None = None,
        algo: str | None = None,
        ts: datetime | None = None,
    ) -> int:
        """Insert an event; returns its rowid. Never raises on logging failure
        (besides sqlite errors, which are programmer errors — those raise)."""
        ts_s = (ts or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
        cur = self.db.conn.execute(
            f"INSERT INTO {self.db.table_prefix}events"
            " (ts, algo, kind, symbol, message, features) VALUES (?,?,?,?,?,?)",
            (
                ts_s,
                algo or self.algo,
                kind,
                symbol,
                message,
                json.dumps(features or {}, separators=(",", ":"), default=str),
            ),
        )
        self.db.conn.commit()
        return int(cur.lastrowid or 0)

    def log_note(self, note: Any) -> int:
        """Log a `core.models.EventNote` directly."""
        return self.log(
            kind=note.kind if isinstance(note.kind, str) else note.kind.value,
            message=note.message,
            symbol=note.symbol,
            features=note.features,
            algo=note.algo,
            ts=note.ts,
        )

    def recent(self, limit: int = 100, kind: str | None = None,
               symbol: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT rowid AS id, * FROM {self.db.table_prefix}events"
        conds, params = [], []
        if kind:
            conds.append("kind = ?")
            params.append(kind)
        if symbol:
            conds.append("symbol = ?")
            params.append(symbol)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        for row in rows:
            row["features"] = json.loads(row.get("features") or "{}")
        return rows


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime,)):
        return obj.isoformat(timespec="milliseconds")
    if hasattr(obj, "model_dump"):  # pydantic
        return obj.model_dump(mode="json")
    return str(obj)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts / level / msg + str(record).__dict__ extras
    minus the noise (spec 3.5)."""

    IGNORED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self.IGNORED or key in _JSON_FIELDS or key == "stack_print":
                continue
            try:
                json.dumps(value, default=_json_default)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = str(value)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, separators=(",", ":"), default=_json_default)


def setup_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
) -> None:
    """Install the JSON-lines handler on the root logger (idempotent-ish)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler: logging.Handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
        root.setLevel(level)


async def telegram_notify(text: str) -> bool:
    """Optional Telegram alert via env vars — fail-soft (spec 3.5)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            )
            return bool(resp.status_code == 200 and resp.json().get("ok"))
    except (httpx.HTTPError, ValueError, KeyError):
        return False
