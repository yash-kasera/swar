"""LLM steps: (1) detect the cast of a story, (2) write the scene + speech script.

Evon v3.3 has no hosted API (confirmed by the Gnani team on Discord), so this uses
Gemini. The provider is isolated here so Evon can be dropped in later.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx

from . import ctx, usage
from .catalog import LANGUAGES, PACE, TAGS, voice_catalog_text


class LLMError(RuntimeError):
    pass


GENDERS = ["male", "female", "unknown"]

CAST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "characters": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "gender": {"type": "STRING", "enum": GENDERS},
                    "description": {"type": "STRING"},
                    "suggested_voice": {"type": "STRING"},
                },
                "required": ["name", "gender", "description", "suggested_voice"],
            },
        }
    },
    "required": ["characters"],
}

SCRIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "logline": {"type": "STRING"},
        "new_characters": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "gender": {"type": "STRING", "enum": GENDERS},
                    "description": {"type": "STRING"},
                },
                "required": ["name", "gender", "description"],
            },
        },
        "scenes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "visual": {"type": "STRING"},
                    "mood": {"type": "STRING"},
                    "sound_cue": {"type": "STRING"},
                    "lines": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "speaker": {"type": "STRING"},
                                "emotion": {"type": "STRING"},
                                "language": {"type": "STRING", "enum": list(LANGUAGES)},
                                "pace": {"type": "STRING", "enum": list(PACE)},
                                "text": {"type": "STRING"},
                                "text_roman": {"type": "STRING"},
                            },
                            "required": ["speaker", "emotion", "language", "pace", "text", "text_roman"],
                        },
                    },
                },
                "required": ["title", "visual", "mood", "lines"],
            },
        },
    },
    "required": ["title", "logline", "scenes"],
}

CAST_SYSTEM = f"""You read a story, script or commentary and list every character who SPEAKS or should get a
voice in an audio drama of it. Always include "Narrator" first (the storyteller voice). Merge aliases
(e.g. "Dadi" and "Amina" are the same person if the text says so) and use the most natural short name.
"gender": male or female when the text makes it clear or strongly implies it (names, pronouns, relations
like dadi/abba), otherwise unknown. "description": age, personality and how they should sound, one line.
"suggested_voice": the best-fitting voice name from this list, matching gender and personality, different
for every character where possible:
{voice_catalog_text()}
If a list of EXISTING CHARACTERS is given, reuse those exact names for anyone already in it."""

SCRIPT_SYSTEM = f"""You are the director of an Indian audio-drama studio for creators. You turn a story or
commentary into a production script made of SCENES. Each scene has:
- "visual": what the viewer would SEE on screen right now (setting, action, camera), 1-3 vivid sentences, in English.
- "mood" and an optional "sound_cue" (ambient sound/music idea, description only).
- "lines": the speech spoken during that scene, in order. A scene may have zero lines if it is purely visual.

Cast rules:
- The CAST below is fixed and already has voices. "speaker" must be exactly one of the cast names.
- Only if the story truly needs a speaking character who is not in the cast, add them to "new_characters"
  and use that name as speaker. Prefer the Narrator for minor voices.

Speech rules (the lines are sent to the Timbre v3.0 text-to-speech model):
- Expressiveness comes ONLY from these inline tags, written exactly as shown (lowercase, angle brackets):
  {", ".join(TAGS)}
  A tag changes delivery from where it is placed until the next tag, so put it right before the words it
  affects. Use 0-2 tags per line, only where they genuinely help. Sound tags like <laugh> or <sigh> produce
  that sound. Never invent other tags, never use [brackets] or *asterisks* for effects.
- Every line is synthesized as a separate request, so give each character one baseline delivery and use
  style tags (<excited>, <whispers>, <shouting>, <slow>) only on lines that need contrast.
- Keep each line under 300 characters; split long narration into several lines.
- Write numbers, dates, years and currency as words the way a person would say them, never as digits.
- "language": the language actually spoken in the line; use "hi-en" for Hindi-English code-mixed speech.
  Keep the story's language mix: if the story is Hinglish, the lines stay Hinglish, with the English
  words kept in English (do not translate them into pure Hindi).
- "text" is what the TTS engine reads, so it must be written for correct pronunciation: every Hindi
  (or other Indian-language) word in its native script (Devanagari for Hindi), English words in Latin
  script. Example: "<excited> आज Eid है और पूरा गाँव super excited है!"
- "text_roman" is the exact same line, same words and same tags, fully in Latin script, the way people
  type Hinglish (e.g. "<excited> Aaj Eid hai aur poora gaon super excited hai!"). It is only shown to
  readers. For all-English lines text_roman equals text.
- "emotion" is a short human-readable label for the UI (e.g. "smug", "panicked").
- "pace" is slow, normal or fast. Keep each character's pace consistent (Narrator almost always "normal").

Notes the author left inside the story for you, such as "(make it feel like a trailer)", and stage
directions in *asterisks* are instructions: act them out as scenes, never read them aloud.
Follow the user's extra instructions carefully (they may be written in any Indian language). Keep it tight:
usually 4-8 scenes. Return only JSON matching the schema."""


def _models() -> list[str]:
    primary = ctx.key("model") or "gemini-3.5-flash"
    fallback = [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest").split(",") if m.strip()]
    return [primary] + [m for m in fallback if m != primary]


def _call(model: str, system: str, prompt: str, schema: dict | None, temperature: float = 0.9) -> str:
    key = ctx.key("gemini")
    if not key:
        raise LLMError("No Gemini API key yet. Add it in Settings (gear icon, bottom left).")
    usage.add(llm_requests=1)
    cfg = {"temperature": temperature}
    if schema:
        cfg |= {"responseMimeType": "application/json", "responseSchema": schema}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key}, json=body, timeout=httpx.Timeout(120, connect=15),
    )
    if r.status_code != 200:
        raise LLMError(f"{model} returned {r.status_code}: {r.text[:300]}")
    j = r.json()
    try:
        parts = j["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise LLMError(f"{model} returned no content: {json.dumps(j)[:300]}")
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _json_call(system: str, prompt: str, schema: dict, validate) -> tuple[dict, str]:
    errors = []
    for model in _models():
        for attempt in range(2):
            try:
                return validate(json.loads(_call(model, system, prompt, schema))), model
            except (LLMError, json.JSONDecodeError, httpx.TransportError, KeyError, TypeError) as e:
                errors.append(f"{model}: {str(e)[:160]}")
                msg = str(e)
                # out of quota, retired, or hanging: don't retry this model, move to the next one
                if any(code in msg for code in (" 429", " 404", " 403")) or isinstance(e, httpx.TimeoutException):
                    break
                time.sleep(2)
    raise LLMError("The script model failed. " + " | ".join(errors[-3:]))


TAG_RE = re.compile(r"<[^<>]{1,30}>")


def clean_text(text: str) -> str:
    """Drop any tag Timbre does not know (it would be silently stripped anyway)."""
    text = TAG_RE.sub(lambda m: m.group(0) if m.group(0) in TAGS else "", text)
    text = re.sub(r"\[[^\]]{1,30}\]", "", text)  # stray [laughs]-style cues
    return re.sub(r"\s{2,}", " ", text).strip()


def speakable(text: str) -> bool:
    return bool(TAG_RE.sub("", text).strip())


# ------------------------------------------------------------------ cast
def detect_cast(story: str, existing: list[dict]) -> list[dict]:
    prompt = f"STORY:\n<<<\n{story}\n>>>\n"
    if existing:
        prompt += "\nEXISTING CHARACTERS:\n" + "\n".join(f"- {c['name']} ({c['gender']}): {c['description']}" for c in existing)

    def validate(j):
        out = []
        for c in j["characters"]:
            name = c["name"].strip()
            if name and name.lower() not in {o["name"].lower() for o in out}:
                out.append({"name": name, "gender": c.get("gender") if c.get("gender") in GENDERS else "unknown",
                            "description": c.get("description", "").strip(), "suggested_voice": c.get("suggested_voice", "")})
        if not any(o["name"].lower() == "narrator" for o in out):
            out.insert(0, {"name": "Narrator", "gender": "male", "description": "Storyteller", "suggested_voice": "Yashraj"})
        return out

    return _json_call(CAST_SYSTEM, prompt, CAST_SCHEMA, validate)[0]


# ------------------------------------------------------------------ script
def write_script(story: str, instructions: list[str], cast: list[dict], previous: dict | None = None) -> tuple[dict, str]:
    """Returns (script, model_used). Speakers are matched to the fixed cast by the caller."""
    prompt = "CAST (fixed):\n" + "\n".join(f"- {c['name']} ({c['gender']}): {c['description']}" for c in cast)
    prompt += f"\n\nSTORY / COMMENTARY TEXT:\n<<<\n{story}\n>>>\n\n"
    if instructions:
        prompt += "USER INSTRUCTIONS (oldest first; the last one is the newest request):\n"
        prompt += "\n".join(f"{i + 1}. {t}" for i, t in enumerate(instructions)) + "\n\n"
    if previous:
        slim = {"title": previous.get("title"), "logline": previous.get("logline"), "scenes": [
            {k: s.get(k) for k in ("title", "visual", "mood", "sound_cue")} | {"lines": [
                {k: ln.get(k) for k in ("speaker", "emotion", "language", "pace", "text", "text_roman")}
                for ln in s.get("lines", [])]}
            for s in previous.get("scenes", [])]}
        prompt += ("CURRENT SCRIPT (revise it according to the newest instruction; keep everything the user did "
                   f"not ask to change):\n{json.dumps(slim, ensure_ascii=False)}\n")
    else:
        prompt += "Write the full production script now."

    def validate(script):
        for s in script.get("scenes", []):
            kept = []
            for ln in s.get("lines", []):
                ln["text"] = clean_text(ln.get("text", ""))
                if not speakable(ln["text"]):
                    continue  # tag-only line would be rejected by Timbre
                ln["text_roman"] = clean_text(ln.get("text_roman") or "") or ln["text"]
                ln["language"] = ln.get("language") if ln.get("language") in LANGUAGES else "hi-en"
                ln["pace"] = ln.get("pace") if ln.get("pace") in PACE else "normal"
                kept.append(ln)
            s["lines"] = kept
        if not script.get("scenes"):
            raise LLMError("The model returned no scenes")
        return script

    return _json_call(SCRIPT_SYSTEM, prompt, SCRIPT_SCHEMA, validate)


LINE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "text": {"type": "STRING"},
        "text_roman": {"type": "STRING"},
        "language": {"type": "STRING", "enum": list(LANGUAGES)},
        "emotion": {"type": "STRING"},
    },
    "required": ["text", "text_roman", "language", "emotion"],
}


def prepare_line(raw: str, speaker: str) -> dict:
    """Turn a line the user typed (any script) into Swar's spoken + reader forms."""
    system = (
        "You prepare one line of dialogue for an Indian text-to-speech engine. Keep the user's words and meaning. "
        "\"text\": Hindi/Indian-language words in native script (Devanagari for Hindi), English words in Latin script, "
        f"keeping any of these tags exactly: {', '.join(TAGS)}. \"text_roman\": the same line fully in Latin script "
        "(Hinglish as people type it), same tags. \"language\": spoken language (hi-en for code-mixed). "
        "\"emotion\": a one-word delivery label. Write numbers as words."
    )
    try:
        j, _ = _json_call(system, f"Speaker: {speaker}\nLine: {raw}", LINE_SCHEMA, lambda x: x)
        text = clean_text(j.get("text") or raw)
        return {"text": text if speakable(text) else clean_text(raw), "text_roman": clean_text(j.get("text_roman") or raw),
                "language": j.get("language") if j.get("language") in LANGUAGES else "hi-en",
                "emotion": (j.get("emotion") or "neutral")[:24]}
    except LLMError:
        t = clean_text(raw)
        return {"text": t, "text_roman": t, "language": "hi-en", "emotion": "neutral"}


def romanize(text: str) -> str:
    """Best-effort Latin-script (Hinglish) copy of an edited line, for the reader view only."""
    if not re.search(r"[ऀ-෿]", text):
        return text
    prompt = ("Transliterate this line into Latin script the way Indians type Hinglish. Keep English words, "
              "punctuation and every <tag> exactly as they are. Reply with the line only.\n\n" + text)
    for model in _models():
        try:
            out = _call(model, "", prompt, None, temperature=0).strip()
            if out:
                return clean_text(out)
        except (LLMError, httpx.HTTPError):
            continue
    return text
