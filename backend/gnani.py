"""Gnani Vachana API clients: Prisma (STT, REST + realtime), Timbre (TTS), voice cloning, audio utils."""
from __future__ import annotations

import io
import os
import time
import wave

import httpx
import numpy as np

from . import ctx, usage

BASE = "https://api.vachana.ai"
WS_STT = "wss://api.vachana.ai/stt/v3/stream"
TTS_SAMPLE_RATE = 24000


class GnaniError(RuntimeError):
    pass


def key() -> str:
    k = ctx.key("gnani")
    if not k:
        raise GnaniError("No Gnani API key yet. Add it in Settings (gear icon, bottom left).")
    return k


def _post(url: str, *, retries: int = 4, **kwargs) -> httpx.Response:
    """POST with backoff on 429 / 5xx (Gnani sends no Retry-After header)."""
    delay = 1.5
    for attempt in range(retries + 1):
        try:
            r = httpx.post(url, headers={"X-API-Key-ID": key()}, timeout=90, **kwargs)
        except httpx.TransportError as e:
            if attempt == retries:
                raise GnaniError(f"Network error calling Gnani: {e}") from e
        else:
            if r.status_code == 200:
                return r
            if r.status_code in (401, 403):
                raise GnaniError("Gnani rejected the API key. Check it in Settings.")
            if r.status_code == 402:
                raise GnaniError("Gnani says the account is out of credits (402).")
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise GnaniError(f"Gnani API {r.status_code}: {r.text[:300]}")
        time.sleep(delay)
        delay *= 2
    raise GnaniError("unreachable")


# ---------------------------------------------------------------- Timbre TTS + cloning
def clone_embedding(raw: bytes, filename: str) -> dict:
    """Extract a speaker embedding from 5-30 s of clean reference audio (WAV/MP3)."""
    r = _post(f"{BASE}/api/v1/tts/voice-clone/embeddings", files={"audio_file": (filename, raw)})
    usage.add(clone_requests=1)
    j = r.json()
    emb = (j.get("data") or j).get("voice_clone_embedding") or j.get("data") or j
    if not isinstance(emb, dict) or "embedding" not in emb:
        raise GnaniError("Voice clone failed: no embedding returned")
    return {"embedding": emb["embedding"], "shape": emb.get("shape", [1, 768]), "dtype": emb.get("dtype", "torch.bfloat16")}


def tts(text: str, voice: str, language: str = "auto", speed: float = 1.0, embedding: dict | None = None) -> bytes:
    """Timbre v3.0 with a stock voice, or Gnani voice cloning when a speaker embedding is given."""
    body = {
        "text": text,
        "model": "vachana-vc-v1" if embedding else "timbre-v3.0",
        "language": language,
        "speed": speed,
        "audio_config": {"sample_rate": TTS_SAMPLE_RATE, "num_channels": 1, "sample_width": 2,
                         "encoding": "linear_pcm", "container": "wav"},
    }
    if embedding:
        body["speaker_embedding"] = embedding
    else:
        body["voice"] = voice
    out = fix_wav(_post(f"{BASE}/api/v1/tts/inference", json=body).content)
    usage.add(tts_chars=len(text), tts_requests=1)
    return out


# ---------------------------------------------------------------- Prisma STT (REST)
MAX_CLIP_S = 25  # REST limit is 30 s per clip; stay under it


def _read_pcm(src) -> tuple[tuple[int, int, int], bytes]:
    """Read all PCM frames. Timbre's streaming WAV header claims nframes=2^31-1, so never trust it."""
    with wave.open(src, "rb") as w:
        fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        return fmt, w.readframes(2**31 - 1)


def _write_wav(fmt: tuple[int, int, int], frames: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setnchannels(fmt[0])
        dst.setsampwidth(fmt[1])
        dst.setframerate(fmt[2])
        dst.writeframes(frames)
    return buf.getvalue()


def _split_wav(raw: bytes) -> list[tuple[bytes, float]]:
    try:
        fmt, frames = _read_pcm(io.BytesIO(raw))
    except (wave.Error, EOFError):
        return [(raw, 0.0)]  # not a PCM WAV: send as-is and let the API decide
    bps = fmt[0] * fmt[1] * fmt[2]
    step = bps * MAX_CLIP_S
    return [(_write_wav(fmt, frames[i:i + step]), len(frames[i:i + step]) / bps) for i in range(0, len(frames), step)] or [(raw, 0.0)]


def stt(raw: bytes, filename: str = "speech.wav") -> dict:
    """Transcribe speech in any supported Indian language (auto-detected)."""
    texts, langs = [], []
    parts = _split_wav(raw) if filename.lower().endswith(".wav") else [(raw, 0.0)]
    for i, (chunk, secs) in enumerate(parts):
        r = _post(f"{BASE}/stt/v3", files={"audio_file": (f"part{i}_{filename}", chunk)},
                  data={"language_code": "indic-auto", "model": "prisma-v3.0", "format": "codemix"})
        usage.add(stt_seconds=secs, stt_requests=1)
        j = r.json()
        if not j.get("success", True):
            raise GnaniError(j.get("error", {}).get("message", "Transcription failed"))
        if j.get("transcript"):
            texts.append(j["transcript"].strip())
        if j.get("detected_language_code"):
            langs.append(j["detected_language_code"])
    return {"transcript": " ".join(texts).strip(), "languages": sorted(set(langs))}


def stream_headers() -> dict:
    """Headers for Prisma realtime. x-partials=true (seen in the server's config echo) streams word-level partials."""
    return {"x-api-key-id": key(), "language_code": "indic-auto", "model": "prisma-v3.0", "x-sample-rate": "16000",
            "format": "codemix", "x-partials": "true", "x-idle-timeout-sec": "30"}


# ---------------------------------------------------------------- audio utils
def _polish(frames: bytes, rate: int, target_rms: float = 0.1) -> bytes:
    """Even out loudness between lines (RMS of the voiced part to ~-20 dBFS), peak-limit, 8 ms fades."""
    x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return frames
    voiced = x[np.abs(x) > 0.02]
    rms = float(np.sqrt(np.mean(voiced ** 2))) if voiced.size > rate // 20 else 0.0
    if rms > 1e-4:
        x = x * min(max(target_rms / rms, 0.4), 4.0)
    peak = float(np.max(np.abs(x)))
    if peak > 0.97:
        x = x * (0.97 / peak)
    n = min(int(rate * 0.008), x.size // 2)
    if n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def mix(items: list[tuple[str, float]], normalize: bool = True) -> tuple[tuple[int, int, int], bytes]:
    """Join same-format WAVs. Each item is (path, seconds of silence after it)."""
    if not items:
        raise GnaniError("No audio has been generated yet")
    fmt0, parts = None, []
    for p, gap in items:
        fmt, frames = _read_pcm(p)
        if fmt0 is None:
            fmt0 = fmt
        if fmt != fmt0:
            continue  # all audio here is 24 kHz mono 16-bit; skip anything else
        if normalize:
            frames = _polish(frames, fmt[2])
        parts += [frames, b"\x00" * (int(fmt[2] * max(0.0, gap)) * fmt[0] * fmt[1])]
    return fmt0, b"".join(parts)


def concat_wavs(items: list[tuple[str, float]], normalize: bool = True) -> bytes:
    return _write_wav(*mix(items, normalize))


def concat_mp3(items: list[tuple[str, float]], normalize: bool = True, kbps: int = 128) -> bytes:
    import lameenc

    fmt, pcm = mix(items, normalize)
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(fmt[2])
    enc.set_channels(fmt[0])
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush())


def fix_wav(raw: bytes) -> bytes:
    """Rewrite a Timbre WAV with a correct header so browsers show the right duration."""
    try:
        return _write_wav(*_read_pcm(io.BytesIO(raw)))
    except (wave.Error, EOFError):
        return raw


def wav_duration(path: str) -> float:
    fmt, frames = _read_pcm(path)
    return len(frames) / float(fmt[0] * fmt[1] * fmt[2])
