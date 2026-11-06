<p align="center"><img src="frontend/favicon.png" width="88" alt="Swar logo"></p>

# Swar (स्वर): a voice studio for content creators

Turn a written script into a voiced, scene-by-scene draft. Swar finds the characters, gives each one a
voice you can audition, swap or clone, then voices every line in Hindi, Hinglish or nine other Indian
languages. You get a storyboard (what's on screen, mood, sound idea, who speaks) plus export-ready audio
and subtitles.

Built for the **Great Indian AI Internship Challenge** on **Gnani AI**'s speech stack.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yash-kasera/swar)

## What it does
| Step | What happens | Tech |
|---|---|---|
| Import | `.docx .pdf .md .html .rtf .txt` or pasted text → plain text | rule-based (python-docx, pypdf, regex), no AI |
| Cast | Characters + gender detected, a default voice assigned to each | Gemini 3.5 Flash, JSON schema output |
| Voices | Audition and swap any of 13 voices, or clone one from a 5–30 s recording | Gnani Timbre v3, Gnani voice cloning (`vachana-vc-v1`) |
| Direct | Type or **speak** your direction in any Indian language; words appear live | Gnani Prisma v3 realtime WebSocket (word-level partials) |
| Generate | Scenes with visuals, mood, sound cues, and lines with emotion tags (`<excited>`, `<sigh>`…) | Gemini plans the scenes, Timbre performs each line |
| Refine | Edit lines and tags, record takes, set pauses, add/move/delete scenes and lines, undo/history, pronunciation rules | audio cached by content, so only changed lines are re-voiced |
| Series | Stories hold episodes; cast, voices, clones and pronunciation are shared | |
| Export | MP3/WAV (loudness evened), SRT/VTT subtitles, every line as a file (ZIP), project as text | numpy, lameenc |

Hindi words are sent to Timbre in Devanagari for cleaner pronunciation; you can read the script in
Hinglish or हिंदी. Evon v3.3 is not used because it has no hosted API yet (confirmed by the Gnani team),
so the script model is Gemini.

## Use it online (bring your own keys)
The hosted version stores **nothing permanent on the server**:
- **API keys** (Gnani from [app.gnani.ai](https://app.gnani.ai/voice), Gemini from Google AI Studio) are saved in
  *your browser's* localStorage and sent only with your own requests. The server never writes them down.
- **Your stories and audio** are mirrored in your browser (IndexedDB). The server keeps a scratch copy per browser
  for processing and deletes it after 12 idle hours; if it's gone (free hosts restart often), your browser
  restores it automatically.
- Every browser gets its own private workspace (random session id). Clearing site data clears your work.

### Deploy your own copy
- **Render (free):** click the button above and sign in. `render.yaml` sets `SWAR_HOSTED=1`. The free plan
  sleeps after 15 minutes idle, so the first visit after that takes about 30 seconds.
- **Any Docker host** (Hugging Face Spaces, Railway, Koyeb, Fly.io): use the `Dockerfile` (port `7860`, or set `PORT`).

## Run locally
```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
.venv/Scripts/python -m uvicorn backend.main:app --port 8000
```
Open http://localhost:8000 and add your keys in **Settings & API keys** (locally they're saved to `.env`).
Set `SWAR_HOSTED=1` to try the browser-storage mode on your machine.

## Layout
- `backend/converter.py`: document → plain text
- `backend/gnani.py`: Prisma STT (REST + realtime), Timbre TTS, voice cloning, audio mixing and MP3
- `backend/llm.py`: cast detection, script writing, line preparation (prompts, schemas, validation)
- `backend/ctx.py`: per-request workspace and keys (local vs hosted mode)
- `backend/main.py`: FastAPI: stories, episodes, cast, clones, generation, editing, history, sync, exports
- `frontend/`: vanilla JS UI, IndexedDB workspace mirror
- `samples/`: demo scripts (a Hinglish *Idgah* trailer inspired by Premchand; *Saal 2067* comedy)
