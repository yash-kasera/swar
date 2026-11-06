"""Per-request context.

Local mode (default): one workspace on disk, keys from .env.
Hosted mode (SWAR_HOSTED=1): every browser gets its own scratch workspace on the server, keyed by a
random session id cookie. The browser keeps the master copy of its work in IndexedDB and restores it
whenever the server's scratch copy is gone (free hosts wipe disks on restart). API keys come only
from the visitor's own browser (cookies set from localStorage) and are never stored on the server.
"""
from __future__ import annotations

import contextvars
import os
import re
import threading
from pathlib import Path

HOSTED = os.environ.get("SWAR_HOSTED", "").lower() in ("1", "true", "yes")
DATA = Path(os.environ.get("SWAR_PROJECTS") or Path(__file__).resolve().parent.parent / "projects")
SESSIONS = DATA / "sessions"
SID_RE = re.compile(r"^[a-zA-Z0-9_-]{16,64}$")

_root: contextvars.ContextVar[Path] = contextvars.ContextVar("root", default=DATA)
_keys: contextvars.ContextVar[dict] = contextvars.ContextVar("keys", default={})


def root() -> Path:
    return _root.get()


def keys() -> dict:
    return _keys.get()


def key(name: str) -> str | None:
    """name: 'gnani' | 'gemini' | 'model'. Hosted mode never falls back to the server's own keys."""
    v = _keys.get().get(name)
    if v:
        return v
    if HOSTED:
        return None if name != "model" else os.environ.get("GEMINI_MODEL")
    return os.environ.get({"gnani": "GNANI_API_KEY", "gemini": "GEMINI_API_KEY", "model": "GEMINI_MODEL"}[name])


def bind(cookies: dict) -> None:
    """Set workspace + keys for this request from its cookies."""
    k = {"gnani": cookies.get("swar_gk", "").strip(), "gemini": cookies.get("swar_mk", "").strip(),
         "model": cookies.get("swar_model", "").strip()}
    _keys.set({n: v for n, v in k.items() if v and len(v) < 400 and not any(c.isspace() for c in v)})
    if HOSTED:
        sid = cookies.get("swar_sid", "")
        if not SID_RE.match(sid):
            raise PermissionError("Missing session. Reload the page.")
        d = SESSIONS / sid
        d.mkdir(parents=True, exist_ok=True)
        _root.set(d)
    else:
        _root.set(DATA)


def spawn(fn, *args) -> None:
    """Run fn in a background thread that keeps this request's workspace and keys."""
    c = contextvars.copy_context()
    threading.Thread(target=c.run, args=(fn, *args), daemon=True).start()
