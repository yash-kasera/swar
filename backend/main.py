"""Swar API.

Data model
  projects/<pid>/project.json      story/series: name, shared cast + voice clones, episode list
  projects/<pid>/episodes/<eid>.json   one episode: document, chat history, script, job
  projects/<pid>/episodes/<eid>.txt    converted plain text of the episode's document
  projects/<pid>/clones/<cid>.json     speaker embedding for a cloned voice
  projects/<pid>/audio/*.wav           synthesized lines (cached by content hash) + clone samples
  projects/<pid>/episodes/<eid>.versions.json   script snapshots for undo / history
  projects/_voice_samples/*.wav        one cached preview per stock Timbre voice
  projects/_usage.json                 local usage meter
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
ENV.touch(exist_ok=True)
load_dotenv(ENV)

from . import converter, gnani, llm, usage  # noqa: E402
from .catalog import LANGUAGES, PACE, TAGS, VOICES  # noqa: E402

from . import ctx  # noqa: E402

SAMPLES = ctx.DATA / "_voice_samples"   # shared cache of stock-voice previews (not user data)
SAMPLES.mkdir(parents=True, exist_ok=True)
ID_RE = re.compile(r"^[a-f0-9]{8,12}$")
LINE_GAP, SCENE_GAP = 0.35, 1.0
BUSY = ("casting", "scripting", "voicing")
_locks: dict[str, threading.RLock] = {}

app = FastAPI(title="Swar")


@app.middleware("http")
async def bind_session(request, call_next):
    """Attach this browser's workspace + API keys (from its cookies) to the request."""
    from fastapi.responses import JSONResponse

    if request.url.path.startswith(("/api/", "/media/")):
        try:
            ctx.bind(request.cookies)
        except PermissionError as e:
            return JSONResponse({"detail": str(e)}, status_code=401)
    return await call_next(request)


# ================================================================== browser-backed workspace sync (hosted mode)
# The browser mirrors every file of its workspace in IndexedDB. If the server's scratch copy is gone
# (restart / cleanup), the browser uploads what's missing and carries on. Nothing is stored for good here.
SYNC_SKIP = {".tmp"}
MAX_SESSION_BYTES = 300 * 1024 * 1024


def _rel_ok(rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/") or not re.match(r"^[\w\-./]+$", rel):
        raise HTTPException(400, "Bad path")
    p = (ctx.root() / rel).resolve()
    if ctx.root().resolve() not in p.parents:
        raise HTTPException(400, "Bad path")
    return p


def _ver(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


@app.get("/api/sync/manifest")
def sync_manifest():
    base = ctx.root()
    files = {}
    for p in base.rglob("*"):
        if p.is_file() and p.suffix not in SYNC_SKIP:
            files[p.relative_to(base).as_posix()] = _ver(p)
    return {"files": files, "hosted": ctx.HOSTED}


class MissingReq(BaseModel):
    paths: list[str]


@app.post("/api/sync/missing")
def sync_missing(body: MissingReq):
    out = []
    for rel in body.paths[:20000]:
        try:
            if not _rel_ok(rel).exists():
                out.append(rel)
        except HTTPException:
            continue
    return {"missing": out}


@app.get("/api/sync/file")
def sync_get(path: str):
    p = _rel_ok(path)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"X-Swar-Version": _ver(p)})


@app.put("/api/sync/file")
async def sync_put(path: str, request: Request):
    if not ctx.HOSTED:
        raise HTTPException(403, "Sync uploads are only used by the hosted version")
    p = _rel_ok(path)
    data = await request.body()
    used = sum(f.stat().st_size for f in ctx.root().rglob("*") if f.is_file())
    if used + len(data) > MAX_SESSION_BYTES:
        raise HTTPException(413, "This browser's workspace is over 300 MB. Delete an old story first.")
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():  # never overwrite newer server state during a restore
        p.write_bytes(data)
    return {"version": _ver(p)}


def _cleanup_sessions():
    """Hosted: drop scratch workspaces idle for 12 h (the browser still has its copy)."""
    while True:
        try:
            if ctx.SESSIONS.exists():
                now = time.time()
                for d in ctx.SESSIONS.iterdir():
                    newest = max((f.stat().st_mtime for f in d.rglob("*") if f.is_file()), default=d.stat().st_mtime)
                    if now - newest > 12 * 3600:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        time.sleep(1800)


if ctx.HOSTED:
    threading.Thread(target=_cleanup_sessions, daemon=True).start()


# ================================================================== storage
def _new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


def _lock(pid: str) -> threading.RLock:
    return _locks.setdefault(pid, threading.RLock())


def _pdir(pid: str) -> Path:
    if not ID_RE.match(pid) or not (ctx.root() / pid / "project.json").exists():
        raise HTTPException(404, "Project not found")
    return ctx.root() / pid


def _write(path: Path, data: dict) -> None:
    data["updated"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _load_project(pid: str) -> dict:
    d = _pdir(pid)
    p = json.loads((d / "project.json").read_text("utf-8"))
    if "episodes" not in p:
        p = _migrate_v1(d, p)
    return p


def _save_project(p: dict) -> None:
    _write(ctx.root() / p["id"] / "project.json", p)


def _epath(pid: str, eid: str) -> Path:
    f = _pdir(pid) / "episodes" / f"{eid}.json"
    if not ID_RE.match(eid) or not f.exists():
        raise HTTPException(404, "Episode not found")
    return f


def _load_ep(pid: str, eid: str) -> dict:
    return json.loads(_epath(pid, eid).read_text("utf-8"))


def _save_ep(pid: str, ep: dict) -> None:
    _write(ctx.root() / pid / "episodes" / f"{ep['id']}.json", ep)


def _blank_job() -> dict:
    return {"status": "idle", "stage": "", "done": 0, "total": 0, "error": None}


def _new_episode(pid: str, name: str) -> dict:
    ep = {"id": _new_id(), "name": name, "created": time.time(), "document": None, "history": [],
          "script": None, "cast_confirmed": False, "job": _blank_job()}
    (ctx.root() / pid / "episodes").mkdir(parents=True, exist_ok=True)
    _save_ep(pid, ep)
    return ep


def _narrator() -> dict:
    return {"id": _new_id(8), "name": "Narrator", "gender": "male", "description": "Storyteller", "voice": "Yashraj"}


def _migrate_v1(d: Path, old: dict) -> dict:
    """Upgrade a single-story project (first version of the app) into project + Episode 1."""
    eid = _new_id()
    cast = []
    for c in (old.get("script") or {}).get("characters", []):
        v = c.get("voice") if c.get("voice") in VOICES else "Yashraj"
        cast.append({"id": _new_id(8), "name": c["name"], "gender": VOICES[v][0], "description": c.get("description", ""), "voice": v})
    if not any(c["name"].lower() == "narrator" for c in cast):
        cast.insert(0, _narrator())
    script = old.get("script")
    if script:
        script.pop("characters", None)
        for s in script.get("scenes", []):
            for ln in s.get("lines", []):
                ln.pop("voice", None)
                if ln.get("audio"):
                    ln["audio_key"] = ln["audio"].rsplit("/", 1)[-1][:-4]
    ep = {"id": eid, "name": "Episode 1", "created": old.get("created", time.time()), "document": old.get("document"),
          "history": old.get("history", []), "script": script, "cast_confirmed": True, "job": old.get("job") or _blank_job()}
    if ep["job"]["status"] in BUSY:
        ep["job"] = _blank_job()
    (d / "episodes").mkdir(exist_ok=True)
    if (d / "source.txt").exists():
        (d / "source.txt").replace(d / "episodes" / f"{eid}.txt")
    _write(d / "episodes" / f"{eid}.json", ep)
    p = {"id": old["id"], "name": old["name"], "created": old.get("created", time.time()), "cast": cast, "clones": {},
         "episodes": [{"id": eid, "name": "Episode 1", "created": ep["created"]}]}
    _write(d / "project.json", p)
    return p


def _set_job(pid: str, eid: str, **kw) -> None:
    with _lock(pid):
        ep = _load_ep(pid, eid)
        ep["job"].update(kw)
        _save_ep(pid, ep)


# ================================================================== cast + voices
def _find_char(cast: list[dict], name: str) -> dict | None:
    n = (name or "").strip().lower()
    return next((c for c in cast if c["name"].lower() == n), None)


def _narrator_of(cast: list[dict]) -> dict:
    return _find_char(cast, "Narrator") or cast[0]


def _pick_voice(gender: str, suggested: str, cast: list[dict]) -> str:
    used = {c["voice"] for c in cast}
    if suggested in VOICES and suggested not in used and (gender == "unknown" or VOICES[suggested][0] == gender):
        return suggested
    same = [v for v, meta in VOICES.items() if gender in ("unknown", meta[0])]
    for v in same:
        if v not in used:
            return v
    return same[len(cast) % len(same)] if same else "Yashraj"


def _add_char(p: dict, name: str, gender: str, description: str, suggested: str = "") -> dict:
    c = {"id": _new_id(8), "name": name.strip()[:40], "gender": gender if gender in llm.GENDERS else "unknown",
         "description": description.strip()[:300], "voice": _pick_voice(gender, suggested, p["cast"])}
    p["cast"].append(c)
    return c


def _voice_label(p: dict, voice: str) -> str:
    if voice.startswith("clone:"):
        cl = p["clones"].get(voice[6:])
        return f"{cl['name']} (cloned)" if cl else "missing clone"
    return voice


def _voice_spec(pid: str, p: dict, speaker: str) -> tuple[str, dict | None]:
    """Resolve a speaker to (voice key, embedding or None)."""
    c = _find_char(p["cast"], speaker) or _narrator_of(p["cast"])
    v = c["voice"]
    if v.startswith("clone:"):
        f = ctx.root() / pid / "clones" / f"{v[6:]}.json"
        if f.exists():
            return v, json.loads(f.read_text("utf-8"))
        v = "Yashraj"
    return v, None


_WORD_EDGE = r"[\wऀ-෿]"


def apply_lexicon(text: str, lexicon: list[dict]) -> str:
    """Pronunciation dictionary: whole-word, case-insensitive replacements applied right before TTS."""
    for item in sorted(lexicon or [], key=lambda x: -len(x.get("from", ""))):
        src, dst = item.get("from", "").strip(), item.get("to", "").strip()
        if src and dst:
            text = re.sub(rf"(?<!{_WORD_EDGE}){re.escape(src)}(?!{_WORD_EDGE})", lambda m: dst, text, flags=re.I)
    return text


def _audio_key(spoken: str, ln: dict, voice: str) -> str:
    raw = json.dumps([spoken, voice, ln["language"], ln["pace"]], ensure_ascii=False).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _base_of(ln: dict) -> str | None:
    return ln.get("audio_base") or ln.get("audio_key")


def _sample_text(gender: str, who: str) -> str:
    verb = "सुनाऊँगी" if gender == "female" else "सुनाऊँगा"
    return f"नमस्ते! मैं {who} हूँ। आपकी story को मैं अपनी आवाज़ में, पूरे feel के साथ {verb}।"


# ================================================================== request models
class Named(BaseModel):
    name: str


class GenerateReq(BaseModel):
    instructions: str = ""
    fresh: bool = False


class CharReq(BaseModel):
    name: str | None = None
    gender: str | None = None
    description: str | None = None
    voice: str | None = None


class LineEdit(BaseModel):
    text: str | None = None
    language: str | None = None
    pace: str | None = None
    speaker: str | None = None
    pause_after: float | None = None
    take: int | None = None


class SceneEdit(BaseModel):
    pause_after: float | None = None
    title: str | None = None
    visual: str | None = None
    mood: str | None = None
    sound_cue: str | None = None


class NewScene(BaseModel):
    after: int = -1
    title: str = "New scene"
    visual: str = ""
    mood: str = ""


class NewLine(BaseModel):
    after: int = -1
    speaker: str
    text: str


class Move(BaseModel):
    dir: int


class PasteReq(BaseModel):
    text: str
    title: str = "Pasted script"


class LexiconReq(BaseModel):
    items: list[dict]


class SettingsReq(BaseModel):
    gnani_api_key: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str | None = None


# ================================================================== routes: meta / settings / usage
@app.get("/api/meta")
def meta():
    return {"voices": VOICES, "languages": LANGUAGES, "tags": TAGS, "paces": list(PACE),
            "llm": ctx.key("model") or "gemini-3.5-flash", "hosted": ctx.HOSTED, "line_gap": LINE_GAP, "scene_gap": SCENE_GAP,
            "keys": _keys_status()}


def _mask(v: str | None) -> str:
    return f"{v[:5]}…{v[-4:]}" if v and len(v) > 12 else ("set" if v else "")


def _keys_status() -> dict:
    return {"gnani": _mask(ctx.key("gnani")), "gemini": _mask(ctx.key("gemini")),
            "gemini_model": ctx.key("model") or "gemini-3.5-flash", "hosted": ctx.HOSTED}


@app.get("/api/settings")
def get_settings():
    return _keys_status() | {"usage": usage.get()}


@app.put("/api/settings")
def put_settings(body: SettingsReq):
    """Save keys to the local .env and apply them immediately. Keys are never sent back in full."""
    for env, val in (("GNANI_API_KEY", body.gnani_api_key), ("GEMINI_API_KEY", body.gemini_api_key),
                     ("GEMINI_MODEL", body.gemini_model)):
        if val is None:
            continue
        val = val.strip()
        if not val or any(c.isspace() for c in val) or len(val) > 400:
            raise HTTPException(400, f"{env} looks invalid")
        if not ctx.HOSTED:  # hosted: keys live in the visitor's browser only, never on the server
            set_key(str(ENV), env, val, quote_mode="never")
            os.environ[env] = val
    return _keys_status()


@app.post("/api/settings/test")
def test_settings():
    """Check both keys with cheap calls: Gemini lists models (free); Gnani renders a cached 1-word preview."""
    out = {}
    k = ctx.key("gemini")
    if not k:
        out["gemini"] = {"ok": False, "message": "No key"}
    else:
        try:
            r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": k}, timeout=20)
            out["gemini"] = {"ok": r.status_code == 200, "message": "Connected" if r.status_code == 200 else f"Error {r.status_code}"}
        except httpx.HTTPError as e:
            out["gemini"] = {"ok": False, "message": str(e)[:120]}
    try:
        gnani.tts("नमस्ते", "Shivani", "hi-IN")
        out["gnani"] = {"ok": True, "message": "Connected"}
    except gnani.GnaniError as e:
        out["gnani"] = {"ok": False, "message": str(e)[:160]}
    return out


@app.post("/api/usage/reset")
def reset_usage():
    return usage.reset()


def _project_view(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k != "clones"}
    out.setdefault("lexicon", [])
    out["clones"] = [{"id": c["id"], "name": c["name"]} for c in p["clones"].values()]
    out["cast"] = [c | {"voice_label": _voice_label(p, c["voice"])} for c in p["cast"]]
    eps = []
    for e in p["episodes"]:
        try:
            ep = _load_ep(p["id"], e["id"])
            eps.append({"id": e["id"], "name": ep["name"], "status": ep["job"]["status"], "has_script": bool(ep["script"])})
        except HTTPException:
            continue
    out["episodes"] = eps
    return out


@app.get("/api/projects")
def list_projects():
    out = []
    for d in ctx.root().iterdir():
        if (d / "project.json").exists():
            try:
                out.append(_project_view(_load_project(d.name)))
            except Exception:
                continue
    return sorted(out, key=lambda x: -x["updated"])


@app.post("/api/projects")
def create_project(body: Named):
    pid = _new_id()
    (ctx.root() / pid / "audio").mkdir(parents=True)
    p = {"id": pid, "name": body.name.strip()[:80] or "Untitled story", "created": time.time(), "cast": [_narrator()],
         "clones": {}, "episodes": []}
    _save_project(p)
    ep = _new_episode(pid, "Episode 1")
    p["episodes"].append({"id": ep["id"], "name": ep["name"], "created": ep["created"]})
    _save_project(p)
    return _project_view(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _project_view(_load_project(pid))


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: Named):
    with _lock(pid):
        p = _load_project(pid)
        p["name"] = body.name.strip()[:80] or p["name"]
        _save_project(p)
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    shutil.rmtree(_pdir(pid))
    return {"ok": True}


# ================================================================== routes: episodes
@app.post("/api/projects/{pid}/episodes")
def create_episode(pid: str, body: Named):
    with _lock(pid):
        p = _load_project(pid)
        ep = _new_episode(pid, body.name.strip()[:80] or f"Episode {len(p['episodes']) + 1}")
        p["episodes"].append({"id": ep["id"], "name": ep["name"], "created": ep["created"]})
        _save_project(p)
    return ep


@app.patch("/api/projects/{pid}/episodes/{eid}")
def rename_episode(pid: str, eid: str, body: Named):
    with _lock(pid):
        ep = _load_ep(pid, eid)
        ep["name"] = body.name.strip()[:80] or ep["name"]
        _save_ep(pid, ep)
        p = _load_project(pid)
        for e in p["episodes"]:
            if e["id"] == eid:
                e["name"] = ep["name"]
        _save_project(p)
    return {"ok": True}


@app.delete("/api/projects/{pid}/episodes/{eid}")
def delete_episode(pid: str, eid: str):
    with _lock(pid):
        f = _epath(pid, eid)
        f.unlink()
        f.with_suffix(".txt").unlink(missing_ok=True)
        p = _load_project(pid)
        p["episodes"] = [e for e in p["episodes"] if e["id"] != eid]
        _save_project(p)
    return {"ok": True}


def _episode_view(pid: str, ep: dict) -> dict:
    p = _load_project(pid)
    out = json.loads(json.dumps(ep))
    if out.get("document"):
        out["document"] = {k: v for k, v in out["document"].items() if k != "text"}
    stale = 0
    for si, s in enumerate((out.get("script") or {}).get("scenes", [])):
        for li, ln in enumerate(s["lines"]):
            c = _find_char(p["cast"], ln["speaker"]) or _narrator_of(p["cast"])
            voice, _ = _voice_spec(pid, p, ln["speaker"])
            spoken = apply_lexicon(ln["text"], p.get("lexicon", []))
            ln["char_id"] = c["id"]
            ln["voice_label"] = _voice_label(p, voice)
            ln["spoken"] = spoken if spoken != ln["text"] else None
            ln["stale"] = not ln.get("audio") or _base_of(ln) != _audio_key(spoken, ln, voice)
            ln["download"] = f"/api/projects/{pid}/episodes/{ep['id']}/lines/{si}/{li}.wav"
            ln["takes"] = [t | {"audio": f"/media/{pid}/{t['key']}.wav"} for t in ln.get("takes", [])]
            stale += ln["stale"]
    out["stale_lines"] = stale
    out["versions"] = len(_load_versions(pid, ep["id"]))
    return out


@app.get("/api/projects/{pid}/episodes/{eid}")
def get_episode(pid: str, eid: str):
    return _episode_view(pid, _load_ep(pid, eid))


@app.get("/api/projects/{pid}/episodes/{eid}/source")
def get_source(pid: str, eid: str):
    f = _epath(pid, eid).with_suffix(".txt")
    if not f.exists():
        raise HTTPException(404, "No document")
    return Response(f.read_text("utf-8"), media_type="text/plain; charset=utf-8")


@app.post("/api/projects/{pid}/episodes/{eid}/document")
async def upload_document(pid: str, eid: str, file: UploadFile = File(...)):
    _epath(pid, eid)
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 15 MB)")
    try:
        text = converter.convert(file.filename or "document.txt", raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read this file: {e}")
    return _set_document(pid, eid, file.filename or "document.txt", text)


@app.post("/api/projects/{pid}/episodes/{eid}/document-text")
def paste_document(pid: str, eid: str, body: PasteReq):
    text = converter._normalise(body.text or "")
    if len(text) < 10:
        raise HTTPException(400, "Paste at least a sentence or two")
    if len(text) > 200_000:
        raise HTTPException(413, "That's too long to paste (200k characters max)")
    title = re.sub(r"[^\w\- ]", "", body.title).strip()[:60] or "Pasted script"
    return _set_document(pid, eid, f"{title}.txt", text)


def _set_document(pid: str, eid: str, filename: str, text: str) -> dict:
    f = _epath(pid, eid)
    file = type("F", (), {"filename": filename})
    with _lock(pid):
        ep = _load_ep(pid, eid)
        if ep["job"]["status"] in BUSY:
            raise HTTPException(409, "Wait for the current step to finish")
        f.with_suffix(".txt").write_text(text, "utf-8")
        ep["document"] = {"filename": file.filename, "chars": len(text), "words": len(text.split()), "preview": text[:600]}
        ep["cast_confirmed"] = False
        ep["job"] = {"status": "casting", "stage": "Finding the characters in your story", "done": 0, "total": 0, "error": None}
        _save_ep(pid, ep)
        p = _load_project(pid)
        if p["name"] in ("Untitled story", "Untitled project") and file.filename:
            p["name"] = Path(file.filename).stem.replace("-", " ").replace("_", " ").title()[:80]
            _save_project(p)
    ctx.spawn(_cast_job, pid, eid)
    return {"ok": True}


def _cast_job(pid: str, eid: str) -> None:
    try:
        story = _epath(pid, eid).with_suffix(".txt").read_text("utf-8")
        found = llm.detect_cast(story, _load_project(pid)["cast"])
        with _lock(pid):
            p = _load_project(pid)
            for c in found:
                existing = _find_char(p["cast"], c["name"])
                if existing:
                    if existing["gender"] == "unknown" and c["gender"] != "unknown":
                        existing["gender"] = c["gender"]
                    existing["description"] = existing["description"] or c["description"]
                else:
                    _add_char(p, c["name"], c["gender"], c["description"], c["suggested_voice"])
            _save_project(p)
            ep = _load_ep(pid, eid)
            ep["episode_cast"] = [c["name"] for c in found]
            ep["job"] = _blank_job() | {"status": "cast_ready", "stage": "Cast ready"}
            _save_ep(pid, ep)
    except Exception as e:
        _set_job(pid, eid, status="error", stage="Failed", error=f"Cast detection failed: {str(e)[:400]}")


# ================================================================== routes: cast, samples, clones
@app.post("/api/projects/{pid}/cast")
def add_character(pid: str, body: CharReq):
    if not (body.name or "").strip():
        raise HTTPException(400, "Name is required")
    with _lock(pid):
        p = _load_project(pid)
        if _find_char(p["cast"], body.name):
            raise HTTPException(400, "A character with this name already exists")
        c = _add_char(p, body.name, body.gender or "unknown", body.description or "")
        if body.voice:
            _apply_voice(p, c, body.voice)
        _save_project(p)
    return _project_view(p)


def _apply_voice(p: dict, c: dict, voice: str) -> None:
    if voice.startswith("clone:"):
        if voice[6:] not in p["clones"]:
            raise HTTPException(400, "Unknown cloned voice")
    elif voice not in VOICES:
        raise HTTPException(400, "Unknown voice")
    c["voice"] = voice


@app.put("/api/projects/{pid}/cast/{cid}")
def update_character(pid: str, cid: str, body: CharReq):
    with _lock(pid):
        p = _load_project(pid)
        c = next((x for x in p["cast"] if x["id"] == cid), None)
        if not c:
            raise HTTPException(404, "Character not found")
        if body.name is not None and body.name.strip() and body.name.strip() != c["name"]:
            new = body.name.strip()[:40]
            if _find_char(p["cast"], new):
                raise HTTPException(400, "A character with this name already exists")
            _rename_speaker(pid, p, c["name"], new)
            c["name"] = new
        if body.gender is not None:
            c["gender"] = body.gender if body.gender in llm.GENDERS else "unknown"
        if body.description is not None:
            c["description"] = body.description.strip()[:300]
        if body.voice is not None:
            _apply_voice(p, c, body.voice)
        _save_project(p)
    return _project_view(p)


def _rename_speaker(pid: str, p: dict, old: str, new: str) -> None:
    for e in p["episodes"]:
        ep = _load_ep(pid, e["id"])
        changed = False
        for s in (ep.get("script") or {}).get("scenes", []):
            for ln in s["lines"]:
                if ln["speaker"].lower() == old.lower():
                    ln["speaker"] = new
                    changed = True
        if changed:
            _save_ep(pid, ep)


@app.delete("/api/projects/{pid}/cast/{cid}")
def delete_character(pid: str, cid: str):
    with _lock(pid):
        p = _load_project(pid)
        c = next((x for x in p["cast"] if x["id"] == cid), None)
        if not c:
            raise HTTPException(404, "Character not found")
        if c["name"].lower() == "narrator":
            raise HTTPException(400, "The Narrator is always part of the cast")
        p["cast"] = [x for x in p["cast"] if x["id"] != cid]
        _save_project(p)
    return _project_view(p)


@app.get("/api/voice-sample/{voice}.wav")
def voice_sample(voice: str):
    if voice not in VOICES:
        raise HTTPException(404)
    f = SAMPLES / f"{voice}.wav"
    if not f.exists():
        try:
            f.write_bytes(gnani.tts(_sample_text(VOICES[voice][0], voice), voice, "hi-en"))
        except gnani.GnaniError as e:
            raise HTTPException(502, str(e))
    return FileResponse(f, media_type="audio/wav")


@app.post("/api/projects/{pid}/clones")
async def create_clone(pid: str, file: UploadFile = File(...), name: str = Form(""), char_id: str = Form("")):
    d = _pdir(pid)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty audio file")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Reference audio must be under 10 MB (5-30 seconds is ideal)")
    fname = file.filename or "reference.wav"
    if not fname.lower().endswith((".wav", ".mp3")):
        raise HTTPException(400, "Upload a WAV or MP3 file with 5-30 seconds of clean speech")
    try:
        emb = gnani.clone_embedding(raw, fname)
    except gnani.GnaniError as e:
        raise HTTPException(502, str(e))
    cid = _new_id(8)
    (d / "clones").mkdir(exist_ok=True)
    (d / "clones" / f"{cid}.json").write_text(json.dumps(emb), "utf-8")
    label = (name or Path(fname).stem).strip()[:40] or "My voice"
    with _lock(pid):
        p = _load_project(pid)
        p["clones"][cid] = {"id": cid, "name": label}
        if char_id:
            c = next((x for x in p["cast"] if x["id"] == char_id), None)
            if c:
                c["voice"] = f"clone:{cid}"
        _save_project(p)
    try:  # pre-render the preview so the first click plays instantly
        clone_sample(pid, cid)
    except HTTPException:
        pass
    return _project_view(p)


@app.get("/api/projects/{pid}/clones/{cid}/sample.wav")
def clone_sample(pid: str, cid: str):
    d = _pdir(pid)
    ef = d / "clones" / f"{cid}.json"
    if not ID_RE.match(cid) or not ef.exists():
        raise HTTPException(404)
    f = d / "audio" / f"clone_{cid}.wav"
    if not f.exists():
        try:
            f.write_bytes(gnani.tts(_sample_text("unknown", "आपकी cloned आवाज़"), "", "hi-en",
                                    embedding=json.loads(ef.read_text("utf-8"))))
        except gnani.GnaniError as e:
            raise HTTPException(502, str(e))
    return FileResponse(f, media_type="audio/wav")


@app.post("/api/stt")
async def speech_to_text(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty recording")
    try:
        return gnani.stt(raw, file.filename or "speech.wav")
    except gnani.GnaniError as e:
        raise HTTPException(502, str(e))


# ================================================================== routes: generation
@app.post("/api/projects/{pid}/episodes/{eid}/generate")
def generate(pid: str, eid: str, body: GenerateReq):
    with _lock(pid):
        ep = _load_ep(pid, eid)
        if not ep.get("document"):
            raise HTTPException(400, "Attach a story or script first")
        if ep["job"]["status"] in BUSY:
            raise HTTPException(409, "A step is already running")
        if body.instructions.strip():
            ep["history"].append({"role": "user", "text": body.instructions.strip(), "at": time.time()})
        ep["cast_confirmed"] = True
        ep["job"] = {"status": "scripting", "stage": "Writing scenes and dialogue", "done": 0, "total": 0, "error": None}
        _save_ep(pid, ep)
    ctx.spawn(_pipeline, pid, eid, body.fresh)
    return {"ok": True}


def _pipeline(pid: str, eid: str, fresh: bool) -> None:
    try:
        ep = _load_ep(pid, eid)
        p = _load_project(pid)
        story = _epath(pid, eid).with_suffix(".txt").read_text("utf-8")
        instructions = [h["text"] for h in ep["history"] if h["role"] == "user"]
        previous = None if fresh or not ep.get("script") else ep["script"]
        script, model = llm.write_script(story, instructions, p["cast"], previous)
        script["model"] = model
        with _lock(pid):
            p = _load_project(pid)
            for nc in script.pop("new_characters", None) or []:
                if nc.get("name") and not _find_char(p["cast"], nc["name"]):
                    _add_char(p, nc["name"], nc.get("gender", "unknown"), nc.get("description", ""))
            _save_project(p)
            narrator = _narrator_of(p["cast"])["name"]
            for si, s in enumerate(script["scenes"]):
                if previous and si < len(previous["scenes"]) and "pause_after" in previous["scenes"][si]:
                    s["pause_after"] = previous["scenes"][si]["pause_after"]
                for li, ln in enumerate(s["lines"]):
                    c = _find_char(p["cast"], ln["speaker"])
                    ln["speaker"] = c["name"] if c else narrator
                    try:
                        old = previous["scenes"][si]["lines"][li] if previous else None
                    except (IndexError, KeyError):
                        old = None
                    if old and "pause_after" in old:
                        ln["pause_after"] = old["pause_after"]
            ep = _load_ep(pid, eid)
            if ep.get("script"):
                last = next((h["text"] for h in reversed(ep["history"]) if h["role"] == "user"), "")
                _snapshot(pid, ep, f"Before revision: {last[:60]}" if last else "Before revision")
            ep["script"] = script
            n_lines = sum(len(s["lines"]) for s in script["scenes"])
            ep["history"].append({"role": "assistant", "at": time.time(),
                                  "text": f"{'Revised' if previous else 'Wrote'} “{script['title']}”: "
                                          f"{len(script['scenes'])} scenes, {n_lines} spoken lines."})
            _save_ep(pid, ep)
        _voice_lines(pid, eid)
        _set_job(pid, eid, status="done", stage="Ready")
    except Exception as e:
        _set_job(pid, eid, status="error", stage="Failed", error=str(e)[:500])


def _voice_lines(pid: str, eid: str, only: set[tuple[int, int]] | None = None) -> None:
    """Synthesize every line whose audio is missing or out of date (text, voice, language or pace changed)."""
    d = ctx.root() / pid
    ep, p = _load_ep(pid, eid), _load_project(pid)
    todo = []
    for si, s in enumerate(ep["script"]["scenes"]):
        for li, ln in enumerate(s["lines"]):
            if only is not None and (si, li) not in only:
                continue
            voice, emb = _voice_spec(pid, p, ln["speaker"])
            spoken = apply_lexicon(ln["text"], p.get("lexicon", []))
            key = _audio_key(spoken, ln, voice)
            if _base_of(ln) != key or not ln.get("audio") or not (d / "audio" / f"{ln.get('audio_key', key)}.wav").exists():
                todo.append((si, li, key, voice, emb, spoken))
    _set_job(pid, eid, status="voicing", stage="Performing every line with Gnani Timbre", done=0, total=len(todo))
    for n, (si, li, key, voice, emb, spoken) in enumerate(todo):
        ln = ep["script"]["scenes"][si]["lines"][li]
        f = d / "audio" / f"{key}.wav"
        if not f.exists():
            f.write_bytes(gnani.tts(spoken, voice, ln["language"], PACE[ln["pace"]], embedding=emb))
        dur = round(gnani.wav_duration(str(f)), 2)
        with _lock(pid):
            cur = _load_ep(pid, eid)
            try:
                tgt = cur["script"]["scenes"][si]["lines"][li]
            except (IndexError, TypeError):
                return  # script was replaced mid-run
            tgt.update(audio=f"/media/{pid}/{key}.wav", audio_key=key, audio_base=key, duration=dur,
                       takes=[{"key": key, "duration": dur}])
            cur["job"]["done"] = n + 1
            _save_ep(pid, cur)


def _start_voicing(pid: str, eid: str, only=None, before=None) -> None:
    def work():
        try:
            if before:
                before()
            _voice_lines(pid, eid, only)
            _set_job(pid, eid, status="done", stage="Ready")
        except Exception as e:
            _set_job(pid, eid, status="error", stage="Failed", error=str(e)[:500])

    ctx.spawn(work)


@app.post("/api/projects/{pid}/episodes/{eid}/revoice")
def revoice(pid: str, eid: str):
    with _lock(pid):
        ep = _load_ep(pid, eid)
        if not ep.get("script"):
            raise HTTPException(400, "Nothing to voice yet")
        if ep["job"]["status"] in BUSY:
            raise HTTPException(409, "A step is already running")
        ep["job"] = {"status": "voicing", "stage": "Re-voicing changed lines", "done": 0, "total": 0, "error": None}
        _save_ep(pid, ep)
    _start_voicing(pid, eid)
    return {"ok": True}


@app.patch("/api/projects/{pid}/episodes/{eid}/lines/{si}/{li}")
def edit_line(pid: str, eid: str, si: int, li: int, body: LineEdit):
    with _lock(pid):
        ep = _load_ep(pid, eid)
        if ep["job"]["status"] in BUSY:
            raise HTTPException(409, "Wait for the current step to finish")
        try:
            ln = ep["script"]["scenes"][si]["lines"][li]
        except (IndexError, TypeError, KeyError):
            raise HTTPException(404, "Line not found")
        text_changed = audio_changed = False
        if body.take is not None:
            takes = ln.get("takes") or []
            if not 0 <= body.take < len(takes):
                raise HTTPException(404, "Take not found")
            t = takes[body.take]
            ln.update(audio=f"/media/{pid}/{t['key']}.wav", audio_key=t["key"], duration=t["duration"])
        if body.text is not None or body.speaker is not None or body.language is not None or body.pace is not None:
            _snapshot(pid, ep, "Before editing a line")
        if body.speaker is not None and body.speaker != ln["speaker"]:
            c = _find_char(_load_project(pid)["cast"], body.speaker)
            if not c:
                raise HTTPException(400, "Unknown character")
            ln["speaker"], audio_changed = c["name"], True
        if body.text is not None:
            new = llm.clean_text(body.text)
            if not llm.speakable(new):
                raise HTTPException(400, "Line needs some words, not only tags")
            text_changed = new != ln["text"]
            audio_changed = audio_changed or text_changed
            ln["text"] = new
        if body.language is not None and body.language != ln["language"]:
            if body.language not in LANGUAGES:
                raise HTTPException(400, "Unknown language")
            ln["language"], audio_changed = body.language, True
        if body.pace is not None and body.pace != ln["pace"]:
            if body.pace not in PACE:
                raise HTTPException(400, "Unknown pace")
            ln["pace"], audio_changed = body.pace, True
        if body.pause_after is not None:
            ln["pause_after"] = round(min(max(body.pause_after, 0.0), 30.0), 2)
        if audio_changed:
            ep["job"] = {"status": "voicing", "stage": "Re-voicing line", "done": 0, "total": 1, "error": None}
        _save_ep(pid, ep)

    if audio_changed:
        def before():
            if text_changed:
                roman = llm.romanize(ln["text"])
                with _lock(pid):
                    cur = _load_ep(pid, eid)
                    cur["script"]["scenes"][si]["lines"][li]["text_roman"] = roman
                    _save_ep(pid, cur)
        _start_voicing(pid, eid, {(si, li)}, before)
    return {"ok": True}


@app.patch("/api/projects/{pid}/episodes/{eid}/scenes/{si}")
def edit_scene(pid: str, eid: str, si: int, body: SceneEdit):
    with _lock(pid):
        ep = _load_ep(pid, eid)
        try:
            s = ep["script"]["scenes"][si]
        except (IndexError, TypeError, KeyError):
            raise HTTPException(404, "Scene not found")
        if any(v is not None for v in (body.title, body.visual, body.mood, body.sound_cue)):
            _snapshot(pid, ep, f"Before editing scene {si + 1}")
            for k in ("title", "visual", "mood", "sound_cue"):
                v = getattr(body, k)
                if v is not None:
                    s[k] = v.strip()[:600]
        if body.pause_after is not None:
            s["pause_after"] = round(min(max(body.pause_after, 0.0), 30.0), 2)
        _save_ep(pid, ep)
    return {"ok": True}


# ================================================================== storyboard editing
def _editable(pid: str, eid: str) -> dict:
    ep = _load_ep(pid, eid)
    if ep["job"]["status"] in BUSY:
        raise HTTPException(409, "Wait for the current step to finish")
    if not ep.get("script"):
        raise HTTPException(400, "Generate the script first")
    return ep


@app.post("/api/projects/{pid}/episodes/{eid}/scenes")
def add_scene(pid: str, eid: str, body: NewScene):
    with _lock(pid):
        ep = _editable(pid, eid)
        scenes = ep["script"]["scenes"]
        _snapshot(pid, ep, "Before adding a scene")
        pos = max(0, min(body.after + 1, len(scenes)))
        scenes.insert(pos, {"title": body.title.strip()[:120] or "New scene", "visual": body.visual.strip()[:600],
                            "mood": body.mood.strip()[:60] or "—", "sound_cue": "", "lines": []})
        _save_ep(pid, ep)
    return {"ok": True, "index": pos}


@app.delete("/api/projects/{pid}/episodes/{eid}/scenes/{si}")
def delete_scene(pid: str, eid: str, si: int):
    with _lock(pid):
        ep = _editable(pid, eid)
        scenes = ep["script"]["scenes"]
        if not 0 <= si < len(scenes):
            raise HTTPException(404, "Scene not found")
        if len(scenes) == 1:
            raise HTTPException(400, "An episode needs at least one scene")
        _snapshot(pid, ep, f"Before deleting scene {si + 1}")
        scenes.pop(si)
        _save_ep(pid, ep)
    return {"ok": True}


@app.post("/api/projects/{pid}/episodes/{eid}/scenes/{si}/move")
def move_scene(pid: str, eid: str, si: int, body: Move):
    with _lock(pid):
        ep = _editable(pid, eid)
        scenes, to = ep["script"]["scenes"], si + (1 if body.dir > 0 else -1)
        if not (0 <= si < len(scenes) and 0 <= to < len(scenes)):
            raise HTTPException(400, "Can't move further")
        _snapshot(pid, ep, f"Before moving scene {si + 1}")
        scenes[si], scenes[to] = scenes[to], scenes[si]
        _save_ep(pid, ep)
    return {"ok": True}


@app.post("/api/projects/{pid}/episodes/{eid}/scenes/{si}/lines")
def add_line(pid: str, eid: str, si: int, body: NewLine):
    if not llm.speakable(llm.clean_text(body.text)):
        raise HTTPException(400, "Type what the character says")
    with _lock(pid):
        ep = _editable(pid, eid)
        if not 0 <= si < len(ep["script"]["scenes"]):
            raise HTTPException(404, "Scene not found")
        c = _find_char(_load_project(pid)["cast"], body.speaker)
        if not c:
            raise HTTPException(400, "Unknown character")
        ep["job"] = {"status": "voicing", "stage": "Preparing and voicing the new line", "done": 0, "total": 1, "error": None}
        _save_ep(pid, ep)
    prepared = llm.prepare_line(body.text, c["name"])  # outside the lock: it calls the LLM
    with _lock(pid):
        ep = _load_ep(pid, eid)
        _snapshot(pid, ep, f"Before adding a line to scene {si + 1}")
        lines = ep["script"]["scenes"][si]["lines"]
        pos = max(0, min(body.after + 1, len(lines)))
        lines.insert(pos, {"speaker": c["name"], "pace": "normal", **prepared})
        _save_ep(pid, ep)
    _start_voicing(pid, eid, {(si, pos)})
    return {"ok": True, "index": pos}


@app.delete("/api/projects/{pid}/episodes/{eid}/lines/{si}/{li}")
def delete_line(pid: str, eid: str, si: int, li: int):
    with _lock(pid):
        ep = _editable(pid, eid)
        try:
            lines = ep["script"]["scenes"][si]["lines"]
            lines[li]
        except (IndexError, KeyError):
            raise HTTPException(404, "Line not found")
        _snapshot(pid, ep, f"Before deleting a line in scene {si + 1}")
        lines.pop(li)
        _save_ep(pid, ep)
    return {"ok": True}


@app.post("/api/projects/{pid}/episodes/{eid}/lines/{si}/{li}/move")
def move_line(pid: str, eid: str, si: int, li: int, body: Move):
    with _lock(pid):
        ep = _editable(pid, eid)
        scenes = ep["script"]["scenes"]
        try:
            ln = scenes[si]["lines"][li]
        except (IndexError, KeyError):
            raise HTTPException(404, "Line not found")
        _snapshot(pid, ep, "Before moving a line")
        lines, d = scenes[si]["lines"], (1 if body.dir > 0 else -1)
        if 0 <= li + d < len(lines):
            lines[li], lines[li + d] = lines[li + d], lines[li]
        elif 0 <= si + d < len(scenes):  # hop into the neighbouring scene
            lines.pop(li)
            nxt = scenes[si + d]["lines"]
            nxt.insert(0 if d > 0 else len(nxt), ln)
        else:
            raise HTTPException(400, "Can't move further")
        _save_ep(pid, ep)
    return {"ok": True}


@app.post("/api/projects/{pid}/episodes/{eid}/lines/{si}/{li}/takes")
def new_take(pid: str, eid: str, si: int, li: int):
    """Render another performance of the same line; the user picks the best take."""
    with _lock(pid):
        ep = _editable(pid, eid)
        try:
            ln = ep["script"]["scenes"][si]["lines"][li]
        except (IndexError, KeyError):
            raise HTTPException(404, "Line not found")
        if not ln.get("audio") or not ln.get("takes"):
            raise HTTPException(400, "Voice this line first")
        if len(ln["takes"]) >= 5:
            raise HTTPException(400, "Five takes is the limit. Pick one, or edit the line.")
        p = _load_project(pid)
        voice, emb = _voice_spec(pid, p, ln["speaker"])
        spoken = apply_lexicon(ln["text"], p.get("lexicon", []))
        base = _audio_key(spoken, ln, voice)
        if base != _base_of(ln):
            raise HTTPException(409, "This line changed. Re-voice it before making takes.")
        ep["job"] = {"status": "voicing", "stage": "Recording another take", "done": 0, "total": 1, "error": None}
        _save_ep(pid, ep)

    def work():
        try:
            key = f"{base}_t{len(ln['takes'])}"
            f = ctx.root() / pid / "audio" / f"{key}.wav"
            f.write_bytes(gnani.tts(spoken, voice, ln["language"], PACE[ln["pace"]], embedding=emb))
            dur = round(gnani.wav_duration(str(f)), 2)
            with _lock(pid):
                cur = _load_ep(pid, eid)
                tgt = cur["script"]["scenes"][si]["lines"][li]
                tgt.setdefault("takes", []).append({"key": key, "duration": dur})
                tgt.update(audio=f"/media/{pid}/{key}.wav", audio_key=key, duration=dur)
                cur["job"] = _blank_job() | {"status": "done", "stage": "Ready"}
                _save_ep(pid, cur)
        except Exception as e:
            _set_job(pid, eid, status="error", stage="Failed", error=str(e)[:500])

    ctx.spawn(work)
    return {"ok": True}


# ================================================================== version history
MAX_VERSIONS = 40


def _vpath(pid: str, eid: str) -> Path:
    return ctx.root() / pid / "episodes" / f"{eid}.versions.json"


def _load_versions(pid: str, eid: str) -> list[dict]:
    try:
        return json.loads(_vpath(pid, eid).read_text("utf-8"))
    except (OSError, ValueError):
        return []


def _snapshot(pid: str, ep: dict, label: str) -> None:
    """Save the current script before a change (call with the project lock held)."""
    if not ep.get("script"):
        return
    vs = _load_versions(pid, ep["id"])
    vs.append({"id": _new_id(8), "at": time.time(), "label": label, "script": json.loads(json.dumps(ep["script"]))})
    _vpath(pid, ep["id"]).write_text(json.dumps(vs[-MAX_VERSIONS:], ensure_ascii=False), "utf-8")


@app.get("/api/projects/{pid}/episodes/{eid}/versions")
def list_versions(pid: str, eid: str):
    _epath(pid, eid)
    return [{"id": v["id"], "at": v["at"], "label": v["label"], "title": v["script"].get("title", ""),
             "scenes": len(v["script"].get("scenes", [])),
             "lines": sum(len(s["lines"]) for s in v["script"].get("scenes", []))}
            for v in reversed(_load_versions(pid, eid))]


@app.post("/api/projects/{pid}/episodes/{eid}/versions/{vid}/restore")
def restore_version(pid: str, eid: str, vid: str):
    with _lock(pid):
        ep = _editable(pid, eid)
        vs = _load_versions(pid, eid)
        v = next((x for x in vs if x["id"] == vid), None)
        if not v:
            raise HTTPException(404, "Version not found")
        _snapshot(pid, ep, "Before restoring an older version")
        ep["script"] = v["script"]
        ep["history"].append({"role": "assistant", "at": time.time(), "text": f"Restored the version saved {v['label'].lower()}."})
        _save_ep(pid, ep)
    return {"ok": True}


@app.post("/api/projects/{pid}/episodes/{eid}/undo")
def undo(pid: str, eid: str):
    with _lock(pid):
        ep = _editable(pid, eid)
        vs = _load_versions(pid, eid)
        if not vs:
            raise HTTPException(400, "Nothing to undo")
        v = vs.pop()
        ep["script"] = v["script"]
        _vpath(pid, eid).write_text(json.dumps(vs, ensure_ascii=False), "utf-8")
        _save_ep(pid, ep)
    return {"ok": True, "label": v["label"]}


# ================================================================== pronunciation dictionary
@app.put("/api/projects/{pid}/lexicon")
def put_lexicon(pid: str, body: LexiconReq):
    items = []
    for it in body.items[:200]:
        src, dst = str(it.get("from", "")).strip()[:60], str(it.get("to", "")).strip()[:120]
        if src and dst:
            items.append({"from": src, "to": dst})
    with _lock(pid):
        p = _load_project(pid)
        p["lexicon"] = items
        _save_project(p)
    return _project_view(p)


@app.post("/api/projects/{pid}/lexicon/preview")
def preview_lexicon(pid: str, body: LexiconReq):
    """Speak one replacement with the Narrator's voice so the user can check the pronunciation."""
    it = (body.items or [{}])[0]
    word = str(it.get("to", "")).strip()[:120]
    if not word:
        raise HTTPException(400, "Nothing to preview")
    p = _load_project(pid)
    voice, emb = _voice_spec(pid, p, "Narrator")
    try:
        wav = gnani.tts(word, voice, "hi-en", embedding=emb)
    except gnani.GnaniError as e:
        raise HTTPException(502, str(e))
    return Response(wav, media_type="audio/wav")


# ================================================================== routes: media + exports
@app.get("/media/{pid}/{name}")
def media(pid: str, name: str):
    if not re.match(r"^[a-z0-9_]{8,24}\.wav$", name):
        raise HTTPException(404)
    f = _pdir(pid) / "audio" / name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="audio/wav")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24] or "line"


def _sequence(pid: str, ep: dict) -> list[dict]:
    """Every voiced line in play order with its pause, file name and start time."""
    seq, t = [], 0.0
    scenes = (ep.get("script") or {}).get("scenes", [])
    for si, s in enumerate(scenes):
        for li, ln in enumerate(s["lines"]):
            if not ln.get("audio"):
                continue
            # Between lines: the line's pause. After a scene's last line: the scene's pause.
            if li == len(s["lines"]) - 1:
                gap = s.get("pause_after", SCENE_GAP) if si < len(scenes) - 1 else 0.0
            else:
                gap = ln.get("pause_after", LINE_GAP)
            path = ctx.root() / pid / "audio" / ln["audio"].rsplit("/", 1)[-1]
            seq.append({"si": si, "li": li, "line": ln, "scene": s, "path": path, "start": t, "gap": gap,
                        "file": f"S{si + 1:02d}_L{li + 1:02d}_{_slug(ln['speaker'])}.wav"})
            t += (ln.get("duration") or 0) + gap
    return seq


def _ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{sec % 60:05.2f}"


@app.get("/api/projects/{pid}/episodes/{eid}/lines/{si}/{li}.wav")
def line_download(pid: str, eid: str, si: int, li: int):
    ep = _load_ep(pid, eid)
    for it in _sequence(pid, ep):
        if it["si"] == si and it["li"] == li:
            return FileResponse(it["path"], media_type="audio/wav", filename=it["file"])
    raise HTTPException(404, "This line has no audio yet")


def _safe_name(*parts: str) -> str:
    return "_".join(re.sub(r"[^\w\- ]", "", x).strip().replace(" ", "-") for x in parts if x) or "swar"


@app.get("/api/projects/{pid}/episodes/{eid}/audiobook.{fmt}")
def audiobook(pid: str, eid: str, fmt: str, normalize: int = 1, kbps: int = 128):
    """Full episode with pauses. normalize=1 evens out loudness between lines and adds click-free fades."""
    if fmt not in ("wav", "mp3"):
        raise HTTPException(404)
    p, ep = _load_project(pid), _load_ep(pid, eid)
    items = [(str(it["path"]), it["gap"]) for it in _sequence(pid, ep)]
    try:
        data = gnani.concat_mp3(items, bool(normalize), kbps if kbps in (96, 128, 192, 256, 320) else 128) \
            if fmt == "mp3" else gnani.concat_wavs(items, bool(normalize))
    except gnani.GnaniError as e:
        raise HTTPException(400, str(e))
    return Response(data, media_type="audio/mpeg" if fmt == "mp3" else "audio/wav",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(p["name"], ep["name"])}.{fmt}"'})


def _subtitles(pid: str, ep: dict, fmt: str, script: str = "roman") -> str:
    def stamp(t: float) -> str:
        h, rem = divmod(max(0.0, t), 3600)
        m, s = divmod(rem, 60)
        ms = int(round((s - int(s)) * 1000))
        sep = "," if fmt == "srt" else "."
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}{sep}{min(ms, 999):03d}"

    out = ["WEBVTT", ""] if fmt == "vtt" else []
    for n, it in enumerate(_sequence(pid, ep), 1):
        ln = it["line"]
        text = (ln.get("text_roman") or ln["text"]) if script == "roman" else ln["text"]
        text = re.sub(r"\s{2,}", " ", llm.TAG_RE.sub("", text)).strip()
        if ln["speaker"].lower() != "narrator":
            text = f"{ln['speaker']}: {text}"
        start, end = it["start"], it["start"] + (ln.get("duration") or 0)
        out += ([str(n)] if fmt == "srt" else []) + [f"{stamp(start)} --> {stamp(end)}", text, ""]
    return "\n".join(out)


@app.get("/api/projects/{pid}/episodes/{eid}/subtitles.{fmt}")
def subtitles(pid: str, eid: str, fmt: str, script: str = "roman"):
    if fmt not in ("srt", "vtt"):
        raise HTTPException(404)
    p, ep = _load_project(pid), _load_ep(pid, eid)
    if not _sequence(pid, ep):
        raise HTTPException(400, "No audio has been generated yet")
    return Response(_subtitles(pid, ep, fmt, script), media_type="text/vtt" if fmt == "vtt" else "application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(p["name"], ep["name"])}.{fmt}"'})


def _export_text(pid: str, p: dict, ep: dict) -> str:
    s = ep.get("script") or {}
    seq = _sequence(pid, ep)
    by_line = {(it["si"], it["li"]): it for it in seq}
    out = ["SWAR · PROJECT EXPORT", "=" * 60, f"Project : {p['name']}", f"Episode : {ep['name']}"]
    if s:
        out += [f"Title   : {s.get('title', '')}", f"Logline : {s.get('logline', '')}", f"Script  : {s.get('model', '')}"]
    if seq:
        total = seq[-1]["start"] + (seq[-1]["line"].get("duration") or 0)
        out.append(f"Runtime : {_ts(total)} (mm:ss.ss, including pauses)")
    out += ["", "CAST & VOICES", "-" * 60]
    for c in p["cast"]:
        out.append(f"- {c['name']} ({c['gender']}): voice {_voice_label(p, c['voice'])}. {c['description']}")
    if ep.get("history"):
        out += ["", "DIRECTION (chat)", "-" * 60]
        out += [f"[{h['role']}] {h['text']}" for h in ep["history"]]
    for si, sc in enumerate(s.get("scenes", [])):
        out += ["", "=" * 60, f"SCENE {si + 1}: {sc['title']}   [{sc.get('mood', '')}]", "=" * 60, f"Visual : {sc['visual']}"]
        if sc.get("sound_cue"):
            out.append(f"Sound  : {sc['sound_cue']}")
        if not sc["lines"]:
            out.append("(no dialogue)")
        for li, ln in enumerate(sc["lines"]):
            it = by_line.get((si, li))
            head = f"[{_ts(it['start'])}] " if it else "[ --:-- ] "
            out.append("")
            out.append(f"{head}{ln['speaker']}  ({ln['emotion']}, {ln['language']}, {ln['pace']})")
            out.append(f"    Audio    : {it['file'] + ' (' + str(ln.get('duration', 0)) + 's)' if it else 'not voiced yet'}")
            out.append(f"    Hinglish : {ln.get('text_roman') or ln['text']}")
            out.append(f"    Spoken   : {apply_lexicon(ln['text'], p.get('lexicon', []))}")
            if len(ln.get("takes") or []) > 1:
                out.append(f"    Takes    : {len(ln['takes'])} recorded, using take {next((i + 1 for i, t in enumerate(ln['takes']) if t['key'] == ln.get('audio_key')), 1)}")
            if it:
                out.append(f"    Pause    : {it['gap']:.2f}s")
    return "\n".join(out) + "\n"


@app.get("/api/projects/{pid}/episodes/{eid}/export.txt")
def export_text(pid: str, eid: str):
    p, ep = _load_project(pid), _load_ep(pid, eid)
    return Response(_export_text(pid, p, ep), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(p["name"], ep["name"])}.txt"'})


@app.get("/api/projects/{pid}/episodes/{eid}/audio.zip")
def export_zip(pid: str, eid: str):
    p, ep = _load_project(pid), _load_ep(pid, eid)
    seq = _sequence(pid, ep)
    if not seq:
        raise HTTPException(400, "No audio has been generated yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for it in seq:
            z.write(it["path"], f"lines/{it['file']}")
        items = [(str(it["path"]), it["gap"]) for it in seq]
        z.writestr("full_episode.wav", gnani.concat_wavs(items))
        z.writestr("full_episode.mp3", gnani.concat_mp3(items))
        z.writestr("subtitles_hinglish.srt", _subtitles(pid, ep, "srt", "roman"))
        z.writestr("subtitles_hinglish.vtt", _subtitles(pid, ep, "vtt", "roman"))
        z.writestr("subtitles_hindi.srt", _subtitles(pid, ep, "srt", "native"))
        z.writestr("project.txt", _export_text(pid, p, ep))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_name(p["name"], ep["name"])}.zip"'})


# ================================================================== live transcript (Prisma realtime)
FINAL_TYPES = ("transcript", "final_transcript")


@app.websocket("/ws/stt")
async def ws_stt(ws: WebSocket):
    """Relay the browser mic (16 kHz PCM16) to Prisma realtime and stream partial + final text back."""
    import websockets

    await ws.accept()
    try:
        ctx.bind(ws.cookies)
    except PermissionError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return
    try:
        upstream = await websockets.connect(gnani.WS_STT, additional_headers=gnani.stream_headers(), open_timeout=15,
                                            max_size=2**20)
    except gnani.GnaniError as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return
    except Exception as e:
        await ws.send_json({"type": "error", "message": f"Could not reach Prisma realtime: {str(e)[:150]}"})
        await ws.close()
        return

    buf, sent = bytearray(), 0
    stopping, final_after_stop = asyncio.Event(), asyncio.Event()

    async def browser_to_prisma():
        nonlocal sent
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    buf.extend(msg["bytes"])
                    while len(buf) >= 1024:  # Prisma wants exactly 1024-byte frames (512 samples)
                        await upstream.send(bytes(buf[:1024]))
                        del buf[:1024]
                        sent += 1024
                elif msg.get("text") and json.loads(msg["text"]).get("type") == "stop":
                    if buf:
                        await upstream.send(bytes(buf) + b"\x00" * (1024 - len(buf)))
                        sent += len(buf)
                        buf.clear()
                    await upstream.send(json.dumps({"type": "commit"}))
                    break
        except (WebSocketDisconnect, RuntimeError, websockets.ConnectionClosed):
            pass
        stopping.set()

    async def prisma_to_browser():
        try:
            async for raw in upstream:
                j = json.loads(raw)
                t = j.get("type")
                if t in ("partial_transcript", *FINAL_TYPES, "error", "speech_start", "speech_end"):
                    await ws.send_json({"type": "final" if t in FINAL_TYPES else t, "text": j.get("text", ""),
                                        "utterance_id": j.get("utterance_id"), "message": j.get("message"),
                                        "language": j.get("detected_language_code")})
                    if t in FINAL_TYPES and stopping.is_set():
                        final_after_stop.set()
        except Exception:
            pass
        final_after_stop.set()

    up, down = asyncio.create_task(browser_to_prisma()), asyncio.create_task(prisma_to_browser())
    await stopping.wait()
    try:
        await asyncio.wait_for(final_after_stop.wait(), timeout=3.5)
    except asyncio.TimeoutError:
        pass
    usage.add(stt_seconds=round(sent / 32000, 2), stt_requests=1)
    try:
        await ws.send_json({"type": "closed"})
        await ws.close()
    except Exception:
        pass
    await upstream.close()
    for task in (up, down):
        task.cancel()


app.mount("/samples", StaticFiles(directory=ROOT / "samples"), name="samples")
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
