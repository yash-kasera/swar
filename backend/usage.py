"""Local usage meter: what Swar has sent to Gnani and the LLM (Gnani publishes no per-call credit costs)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import ctx


def _file() -> Path:
    return ctx.root() / "_usage.json"
FIELDS = ("tts_chars", "tts_requests", "clone_requests", "stt_seconds", "stt_requests", "llm_requests")
_lock = threading.Lock()


def _read() -> dict:
    try:
        d = json.loads(_file().read_text("utf-8"))
    except (OSError, ValueError):
        d = {}
    for f in FIELDS:
        d.setdefault(f, 0)
    d.setdefault("since", time.time())
    return d


def add(**kw) -> None:
    with _lock:
        d = _read()
        for k, v in kw.items():
            d[k] = round(d.get(k, 0) + v, 2)
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(json.dumps(d), "utf-8")


def get() -> dict:
    with _lock:
        return _read()


def reset() -> dict:
    with _lock:
        d = {f: 0 for f in FIELDS} | {"since": time.time()}
        _file().write_text(json.dumps(d), "utf-8")
        return d
