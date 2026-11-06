/* Swar front end: stories → episodes, shared cast & voices, direction chat, scene player, exports. */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
const COLORS = ["#16a34a", "#0e7490", "#b45309", "#7c3aed", "#be185d", "#4d7c0f", "#475569", "#c2410c"];

const state = {
  meta: null, projects: [], project: null, ep: null,
  open: new Set(JSON.parse(store.get("swar:open") || "[]")),
  view: store.get("swar:view") === "native" ? "native" : "roman",
  editing: null, pauseEdit: null, castOpen: false, cloneFor: null, pollTimer: null, lastRender: "",
  takesOpen: null, sceneEdit: null, addLineFor: null, pasteOpen: false, lexOpen: false, lexDraft: null,
};
const IC = (d) => `<svg viewBox="0 0 24 24"><path d="${d}"/></svg>`;
const TOOL = { up: IC("M12 19V5M6 11l6-6 6 6"), down: IC("M12 5v14M6 13l6 6 6-6"), trash: IC("M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"),
  takes: IC("M4 7h11M4 12h8M4 17h11M17 9l3 3-3 3"), plus: IC("M12 5v14M5 12h14") };
const shown = (l) => (state.view === "roman" ? l.text_roman || l.text : l.text);
const BUSY = ["casting", "scripting", "voicing"];
const busy = () => BUSY.includes(state.ep?.job?.status);
const inReview = () => !!(state.ep?.document && !state.ep.cast_confirmed);

/* ============================================================ session, browser keys, workspace mirror
   Hosted Swar keeps nothing permanent on the server. This browser holds:
   - a random session id + your API keys (localStorage, sent to the Swar server as cookies),
   - a full copy of your workspace in IndexedDB, restored to the server whenever its scratch copy is gone. */
const KEYS = { gnani: "swar:gnaniKey", gemini: "swar:geminiKey", model: "swar:model" };
function sessionId() {
  let s = store.get("swar:sid");
  if (!s || !/^[a-zA-Z0-9_-]{16,64}$/.test(s)) {
    s = (crypto.randomUUID?.() || String(Math.random()).slice(2) + Date.now()).replace(/[^a-zA-Z0-9]/g, "").padEnd(24, "0");
    store.set("swar:sid", s);
  }
  return s;
}
function setCookies() {
  const sec = location.protocol === "https:" ? "; Secure" : "";
  const put = (k, v) => (document.cookie = `${k}=${(v || "").replace(/[^\w.\-]/g, "")}; path=/; SameSite=Strict; max-age=31536000${sec}`);
  put("swar_sid", sessionId());
  put("swar_gk", store.get(KEYS.gnani)); put("swar_mk", store.get(KEYS.gemini)); put("swar_model", store.get(KEYS.model));
}
setCookies();

const mirror = {
  db: null,
  open() {
    if (this.db) return Promise.resolve(this.db);
    return new Promise((res, rej) => {
      const r = indexedDB.open("swar-workspace", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("files");
      r.onsuccess = () => { this.db = r.result; res(this.db); };
      r.onerror = () => rej(r.error);
    });
  },
  async tx(mode, fn) {
    const db = await this.open();
    return new Promise((res, rej) => {
      const t = db.transaction("files", mode), s = t.objectStore("files");
      const out = fn(s);
      t.oncomplete = () => res(out?.result ?? out);
      t.onerror = () => rej(t.error);
    });
  },
  get(p) { return this.tx("readonly", (s) => s.get(p)); },
  put(p, rec) { return this.tx("readwrite", (s) => s.put(rec, p)); },
  async versions() {
    const db = await this.open();
    return new Promise((res) => {
      const out = {}, cur = db.transaction("files").objectStore("files").openCursor();
      cur.onsuccess = () => { const c = cur.result; if (!c) return res(out); out[c.key] = c.value.v; c.continue(); };
      cur.onerror = () => res(out);
    });
  },
  async dropPrefix(prefix) {
    const vs = await this.versions();
    await this.tx("readwrite", (s) => { for (const k of Object.keys(vs)) if (k.startsWith(prefix)) s.delete(k); });
  },
};
const sync = { running: null, again: false, timer: null, ready: false };

async function syncNow() {
  if (!state.meta?.hosted || !window.indexedDB) return;
  if (sync.running) { sync.again = true; return sync.running; }
  sync.running = (async () => {
    try {
      const mine = await mirror.versions();
      const { files } = await (await fetch("/api/sync/manifest")).json();
      // 1) the server lost files this browser has (restart / cleanup): put them back
      const missing = Object.keys(mine).filter((p) => !(p in files));
      if (missing.length) {
        if (missing.length > 3) toast(`Restoring your work from this browser (${missing.length} files)…`, "info");
        for (const p of missing) {
          const rec = await mirror.get(p);
          if (!rec?.blob) continue;
          const r = await fetch(`/api/sync/file?path=${encodeURIComponent(p)}`, { method: "PUT", body: rec.blob });
          if (r.ok) { rec.v = (await r.json()).version; await mirror.put(p, rec); files[p] = rec.v; }
        }
      }
      // 2) copy anything new or changed on the server into this browser
      for (const [p, v] of Object.entries(files)) {
        if (mine[p] === v) continue;
        const r = await fetch(`/api/sync/file?path=${encodeURIComponent(p)}`);
        if (r.ok) await mirror.put(p, { v: r.headers.get("X-Swar-Version") || v, blob: await r.blob() });
      }
    } catch (e) { console.warn("sync", e); }
    sync.running = null;
    if (sync.again) { sync.again = false; syncNow(); }
  })();
  return sync.running;
}
function syncSoon(ms = 1200) { clearTimeout(sync.timer); sync.timer = setTimeout(syncNow, ms); }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") syncNow(); });

async function api(path, opts = {}) {
  let r = await fetch(path, opts);
  // Server restarted and lost this browser's scratch copy: restore from IndexedDB once, then retry.
  if (r.status === 404 && state.meta?.hosted && !opts._retried && /^\/api\/projects\//.test(path)) {
    await syncNow();
    r = await fetch(path, opts);
  }
  if ((opts.method || "GET") !== "GET" && r.ok) syncSoon();
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}
const J = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const epUrl = (suffix = "") => `/api/projects/${state.project.id}/episodes/${state.ep.id}${suffix}`;

function toast(msg, kind = "err") {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast " + kind; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), kind === "err" ? 6000 : 2800);
}

/* ============================================================ tree */
async function loadProjects() { state.projects = await api("/api/projects"); renderTree(); }

function renderTree() {
  const chev = `<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>`;
  $("#tree").innerHTML = state.projects.map((p) => {
    const open = state.open.has(p.id);
    const eps = open ? p.episodes.map((e) => `
      <button class="t-row ep ${state.ep?.id === e.id ? "active" : ""}" data-ep="${p.id}:${e.id}">
        <span class="dot ${e.status}"></span><span class="name">${esc(e.name)}</span></button>`).join("")
      + `<button class="t-row add" data-add-ep="${p.id}"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg> New episode</button>` : "";
    return `<button class="t-row story ${open ? "open" : ""}" data-story="${p.id}">${chev}<span class="name">${esc(p.name)}</span>
      <small style="color:var(--faint)">${p.episodes.length}</small></button>${eps}`;
  }).join("") || `<div class="doc-sub" style="padding:6px 10px">No stories yet. Create one, or try a sample.</div>`;
}

function toggleStory(pid, force) {
  const open = force ?? !state.open.has(pid);
  open ? state.open.add(pid) : state.open.delete(pid);
  store.set("swar:open", JSON.stringify([...state.open]));
  renderTree();
}

async function openEpisode(pid, eid) {
  stopPlayback();
  state.editing = state.pauseEdit = null;
  state.project = await api(`/api/projects/${pid}`);
  state.ep = await api(`/api/projects/${pid}/episodes/${eid}`);
  state.castOpen = false;
  store.set("swar:last", `${pid}:${eid}`);
  toggleStory(pid, true);
  setSide(false);
  render(true);
  schedulePoll();
}

async function newStory(name = "Untitled story") {
  // Reuse an empty untitled story instead of piling up blank ones.
  const blank = state.projects.find((x) => x.name === "Untitled story" && x.episodes.length === 1 && x.episodes[0].status === "idle" && !x.episodes[0].has_script);
  if (blank) {
    const ep = await api(`/api/projects/${blank.id}/episodes/${blank.episodes[0].id}`);
    if (!ep.document) { await openEpisode(blank.id, ep.id); return blank; }
  }
  const p = await api("/api/projects", J("POST", { name }));
  await loadProjects();
  await openEpisode(p.id, p.episodes[0].id);
  return p;
}

async function newEpisode(pid) {
  const p = state.projects.find((x) => x.id === pid);
  const ep = await api(`/api/projects/${pid}/episodes`, J("POST", { name: `Episode ${(p?.episodes.length || 0) + 1}` }));
  await loadProjects();
  await openEpisode(pid, ep.id);
}

async function refresh() {
  if (!state.ep) return;
  const [p, ep] = await Promise.all([api(`/api/projects/${state.project.id}`), api(epUrl())]);
  state.project = p; state.ep = ep;
  render();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!busy()) return;
  state.pollTimer = setTimeout(async () => {
    const id = state.ep?.id;
    try {
      const wasBusy = busy();
      await refresh();
      if (state.ep?.id !== id) return;
      if (wasBusy && !busy()) { loadProjects(); syncSoon(300); }
    } catch (e) { console.warn(e); }
    schedulePoll();
  }, 1200);
}

/* ============================================================ rendering */
const charIndex = (id) => Math.max(0, (state.project?.cast || []).findIndex((c) => c.id === id));
const colorOf = (id) => COLORS[charIndex(id) % COLORS.length];
const initials = (n) => String(n).split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const richText = (t) => esc(t).replace(/&lt;([a-z ]{2,20})&gt;/g, (m, tag) => `<span class="tag">${tag}</span>`);
const linePause = (l) => l.pause_after ?? state.meta.line_gap;
const scenePause = (s) => s.pause_after ?? state.meta.scene_gap;
const ICON = {
  play: `<svg viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z" class="fill"/></svg>`,
  stop: `<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" class="fill"/></svg>`,
  dl: `<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>`,
  edit: `<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>`,
  clone: `<svg viewBox="0 0 24 24"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM19 11a7 7 0 0 1-14 0M12 18v3"/><path d="M17 4l2-2M19 7h2"/></svg>`,
  x: `<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>`,
  chev: `<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>`,
};

function render(force = false) {
  const p = state.project, ep = state.ep;
  const key = JSON.stringify([p?.updated, ep?.id, ep?.updated, state.editing, state.pauseEdit, state.view, state.castOpen,
    state.takesOpen, state.sceneEdit, state.addLineFor, state.pasteOpen, state.lexOpen, !!state.lexDraft]);
  if (!force && key === state.lastRender) return;
  state.lastRender = key;
  renderTopbar();
  renderComposer();
  const feed = $("#feed");
  const scroll = feed.scrollTop;
  if (!ep) { feed.innerHTML = heroHTML(true); return; }
  let html = "";
  if (!ep.document) html += heroHTML(p.episodes.length <= 1);
  else html += docHTML(ep.document);
  if (ep.job.status === "casting") html += progressHTML(ep.job);
  else if (ep.document) html += castHTML() + lexHTML();
  for (const h of ep.history) html += `<div class="msg ${h.role}"><div class="bubble">${esc(h.text)}</div></div>`;
  if (busy() && ep.job.status !== "casting") html += progressHTML(ep.job);
  if (ep.job.status === "error") html += `<div class="card error-card"><b>Something went wrong.</b><br>${esc(ep.job.error)}<br><small>Try again; your work is saved.</small></div>`;
  if (ep.script && !busy() && ep.stale_lines) html += `<div class="card banner">⟳ <span>${ep.stale_lines} line${ep.stale_lines > 1 ? "s" : ""} need new audio because a voice or line changed.</span><button class="btn small primary" data-revoice>Re-voice now</button></div>`;
  if (ep.script) html += scriptHTML(ep.script);
  // Keep what the user is typing in inline forms across re-renders (polling can re-render mid-typing).
  const keep = [...feed.querySelectorAll("[data-keep]")].map((el) => [el.dataset.keep, el.value]);
  feed.innerHTML = html;
  for (const [k, v] of keep) { const el = feed.querySelector(`[data-keep="${k}"]`); if (el) el.value = v; }
  feed.scrollTop = scroll;
  markPlaying();
  const pi = $("#pauseInput"); if (pi) { pi.focus(); pi.select(); }
}

/* ---------- pronunciation dictionary */
function lexHTML() {
  const p = state.project, items = state.lexDraft || p.lexicon || [];
  const head = `<div class="cast-head" data-toggle-lex>${ICON.chev}<h3>Pronunciation <span class="doc-sub">· fix how names and words are spoken, for the whole story</span></h3>
    <span class="doc-sub">${(p.lexicon || []).length} rule${(p.lexicon || []).length === 1 ? "" : "s"}</span></div>`;
  if (!state.lexOpen) return `<div class="card cast-card lex-card">${head}</div>`;
  const rows = items.map((it, i) => `<div class="lex-row" data-lex="${i}">
      <input class="field" data-lex-from data-keep="lexf${i}" placeholder="Written as, e.g. Hamid" value="${esc(it.from)}"/>
      <span class="arrow">→</span>
      <input class="field" data-lex-to data-keep="lext${i}" placeholder="Say it as, e.g. हामिद" value="${esc(it.to)}"/>
      <span class="c-actions"><button class="icon-btn" data-lex-play="${i}" title="Hear it">${ICON.play}</button>
        <button class="icon-btn" data-lex-del="${i}" title="Remove">${ICON.x}</button></span></div>`).join("");
  return `<div class="card cast-card lex-card open">${head}
    <div class="cast-intro">Every line is checked for these words (whole words, any case) just before it's voiced. The script you read doesn't change. Tip: write the "say it as" side in Devanagari.</div>
    <div class="lex-rows">${rows || `<div class="doc-sub">No rules yet.</div>`}</div>
    <div class="lex-foot"><button class="btn ghost small" data-lex-add>+ Add word</button><span class="spacer"></span>
      ${state.lexDraft ? `<button class="btn small" data-lex-cancel>Cancel</button>` : ""}
      <button class="btn small primary" data-lex-save>Save rules</button></div></div>`;
}
function readLexDraft() {
  return [...document.querySelectorAll("[data-lex]")].map((r) => ({ from: r.querySelector("[data-lex-from]").value, to: r.querySelector("[data-lex-to]").value }));
}

function renderTopbar() {
  const p = state.project, ep = state.ep;
  $("#crumbs").innerHTML = p
    ? `<span class="crumb story" data-rename="story" title="Rename story">${esc(p.name)}</span><span class="crumb-sep">/</span>
       <span class="crumb ep" data-rename="ep" title="Rename episode">${esc(ep?.name || "")}</span>`
    : `<span class="crumb-home">Swar</span>`;
  const hasAudio = !!ep?.script?.scenes?.some((s) => s.lines.some((l) => l.audio));
  $("#topActions").hidden = !ep;
  $("#playAll").disabled = !hasAudio;
  $("#exportBtn").disabled = !ep?.script;
  $("#undoBtn").disabled = !ep?.versions || busy();
  $("#undoBtn").style.opacity = ep?.versions && !busy() ? 1 : 0.35;
  $("#historyBtn").style.opacity = ep?.versions ? 1 : 0.35;
  if (ep) {
    const links = { exMp3: "/audiobook.mp3", exWav: "/audiobook.wav", exSrt: "/subtitles.srt", exSrtHi: "/subtitles.srt?script=native",
      exVtt: "/subtitles.vtt", exZip: "/audio.zip", exTxt: "/export.txt" };
    for (const [id, path] of Object.entries(links)) {
      $("#" + id).href = epUrl(path);
      if (id !== "exTxt") $("#" + id).classList.toggle("disabled", !hasAudio);
    }
  }
}

function renderComposer() {
  const ep = state.ep;
  $("#sendBtn").disabled = busy() || !ep?.document;
  $("#sendBtn").title = inReview() ? "Proceed: write scenes and voice them" : "Send direction";
  $("#prompt").placeholder = !ep?.document
    ? "Attach this episode's script with 📎, or drop a file anywhere"
    : inReview()
      ? "Optional direction before you proceed: “60-second trailer, narrator deep and cinematic”… or tap the mic"
      : "Direct a revision: “make Hamid sound more excited in scene 2”… or tap the mic and say it in any language";
}

function heroHTML(first) {
  return `<div class="hero">
    ${first ? `<span class="eyebrow">For YouTube, Reels, podcasts and audio stories</span>` : ""}
    <h2>${first ? `Turn a written script into a voiced, <span class="nw">scene-by-scene</span> draft`
                : `Add the script for ${esc(state.ep?.name || "this episode")}`}</h2>
    <p>${first ? `Upload your script or paste it in. Swar finds the characters and gives each one a voice. You can listen to it, swap it, or clone your own. Then it voices every line, scene by scene, in Hindi, Hinglish or nine other Indian languages.`
               : `This episode uses the same characters and voices as the rest of “${esc(state.project?.name || "")}”. If the script has someone new, Swar picks a voice for them, and you can change it before anything is recorded.`}</p>
    ${first ? `<div class="hero-cta"><button class="btn primary" data-sample="idgah-trailer.txt">Try it with a sample: Idgah</button>
      <button class="btn" data-sample="tung-tung-2067.md">Or a comedy: Saal 2067</button></div>
      <div class="audience">Idgah is a short Hinglish trailer based on Premchand's story.</div>` : ""}
    <div class="drop" id="drop"><b>Drop a file here</b> or click to choose one<br><small>Word, PDF, text, Markdown, HTML or RTF</small></div>
    ${state.pasteOpen ? `<div class="card paste-card">
        <input class="field" id="pasteTitle" data-keep="pasteTitle" placeholder="Title (optional)"/>
        <textarea class="field" id="pasteText" data-keep="pasteText" placeholder="Paste or type your script here, in any language or Hinglish…"></textarea>
        <div class="acts"><span class="doc-sub" style="flex:1">Your text is used exactly as written, with no AI cleanup.</span>
          <button class="btn small" data-paste-cancel>Cancel</button><button class="btn small primary" data-paste-use>Use this text →</button></div></div>`
      : `<div style="margin-top:10px"><button class="btn ghost small" data-paste-open>No file? Paste the text instead</button></div>`}
    ${first ? `<div class="steps">
      <div class="step"><span class="n">01</span><b>Bring the script</b>The file is turned into plain text with simple rules, not AI, so your words stay exactly as written.</div>
      <div class="step"><span class="n">02</span><b>Check the cast</b>Each character gets a voice to start with. Listen, pick another, or clone one from a recording.</div>
      <div class="step"><span class="n">03</span><b>Direct it</b>Say how it should sound: "slower narrator", "Hamid more excited". Type it, or speak it.</div>
      <div class="step"><span class="n">04</span><b>Take it with you</b>MP3 or WAV, each line on its own, subtitles for your editor, and the full script as text.</div></div>` : ""}
  </div>`;
}

function docHTML(d) {
  const ext = (d.filename || "txt").split(".").pop();
  return `<div class="card doc-card" id="docCard"><div class="doc-icon">${esc(ext)}</div>
    <div class="doc-meta"><div class="doc-name">${esc(d.filename)}</div>
      <div class="doc-sub">${d.words} words · converted to plain text · <a href="#" id="docToggle">show text</a></div>
      <div class="doc-preview">${esc(d.preview)}${d.chars > 600 ? "…" : ""}</div></div></div>`;
}

function progressHTML(job) {
  const pct = job.total ? Math.round((100 * job.done) / job.total) : job.status === "casting" ? 30 : 8;
  const sub = { casting: "Reading the script and assigning default voices…", scripting: "Planning scenes and writing every line…",
    voicing: `${job.done} / ${job.total} lines voiced` }[job.status] || "";
  return `<div class="card progress"><div class="row"><span class="spinner"></span><b>${esc(job.stage)}</b></div>
    <div class="doc-sub" style="margin-top:3px">${sub}</div><div class="bar"><span style="width:${pct}%"></span></div></div>`;
}

/* ---------- cast */
function voiceOptions(c) {
  const m = state.meta.voices, opt = (v, label) => `<option value="${esc(v)}" ${v === c.voice ? "selected" : ""}>${esc(label)}</option>`;
  const match = Object.keys(m).filter((v) => c.gender === "unknown" || m[v][0] === c.gender);
  const rest = Object.keys(m).filter((v) => !match.includes(v));
  const lab = (v) => `${v} · ${m[v][0]}, ${m[v][1]}`;
  let html = `<optgroup label="${c.gender === "unknown" ? "Timbre voices" : `Suggested (${c.gender})`}">${match.map((v) => opt(v, lab(v))).join("")}</optgroup>`;
  if (rest.length) html += `<optgroup label="Other voices">${rest.map((v) => opt(v, lab(v))).join("")}</optgroup>`;
  if (state.project.clones.length) html += `<optgroup label="Your cloned voices">${state.project.clones.map((cl) => opt(`clone:${cl.id}`, `${cl.name} · cloned`)).join("")}</optgroup>`;
  return html;
}

function castHTML() {
  const p = state.project, ep = state.ep;
  const open = inReview() || state.castOpen;
  const inEp = new Set((ep.episode_cast || []).map((n) => n.toLowerCase()));
  const spoke = new Set();
  ep.script?.scenes.forEach((s) => s.lines.forEach((l) => spoke.add(l.speaker.toLowerCase())));
  const here = (c) => inEp.has(c.name.toLowerCase()) || spoke.has(c.name.toLowerCase());
  const sorted = [...p.cast].sort((a, b) => here(b) - here(a));
  const head = `<div class="cast-head" data-toggle-cast>${inReview() ? "" : ICON.chev}
      <h3>Cast & voices <span class="doc-sub">· shared by every episode of “${esc(p.name)}”</span></h3>
      ${inReview() ? `<span class="doc-sub">Check before recording</span>` : `<span class="doc-sub">${p.cast.length} characters · edit</span>`}</div>`;
  if (!open) {
    return `<div class="card cast-card">${head}<div class="cast-summary">${sorted.filter(here).map((c) =>
      `<span class="who-item"><i style="background:${colorOf(c.id)}"></i>${esc(c.name)}<small>voiced by ${esc(c.voice_label)}</small></span>`).join(`<span class="dot-sep">/</span>`)}</div></div>`;
  }
  const rows = sorted.map((c) => `
    <div class="c-row ${here(c) ? "" : "other"}" data-char="${c.id}">
      <span class="avatar" style="background:${colorOf(c.id)}">${esc(initials(c.name))}</span>
      <div><input value="${esc(c.name)}" data-field="name" ${c.name.toLowerCase() === "narrator" ? "readonly" : ""} aria-label="Character name"/></div>
      <select data-field="gender" aria-label="Gender">${["male", "female", "unknown"].map((g) => `<option ${g === c.gender ? "selected" : ""}>${g}</option>`).join("")}</select>
      <select data-field="voice" aria-label="Voice">${voiceOptions(c)}</select>
      <div class="c-actions">
        <button class="icon-btn" data-sample-voice="${esc(c.voice)}" title="Listen to this voice">${ICON.play}</button>
        <button class="icon-btn" data-clone="${c.id}" title="Clone a voice from an audio file (WAV/MP3, 5–30 s) for ${esc(c.name)}">${ICON.clone}</button>
        ${c.name.toLowerCase() === "narrator" ? "" : `<button class="icon-btn" data-del-char="${c.id}" title="Remove character">${ICON.x}</button>`}
      </div>
      <div class="desc">${here(c) ? `<span class="in-ep">in this episode</span> ` : `<span style="margin-right:6px">from another episode ·</span>`}${esc(c.description)}</div>
    </div>`).join("");
  const foot = inReview()
    ? `<span class="doc-sub">Defaults are ready. Change anything, or just proceed.</span><span class="spacer"></span>
       <button class="btn ghost small" data-add-char>+ Add character</button>
       <button class="btn primary" data-proceed>Proceed: write scenes & voice them →</button>`
    : `<button class="btn ghost small" data-add-char>+ Add character</button><span class="spacer"></span>
       <span class="doc-sub">Voice changes apply to all episodes. Re-voice to update audio.</span>`;
  return `<div class="card cast-card open">${head}
    ${inReview() ? `<div class="cast-intro">Swar found these characters and gave each one a default voice. Press ▶ to audition, pick another voice, fix a gender, or clone a voice from your own recording.</div>` : ""}
    <div class="cast-rows">${rows}</div><div class="cast-foot">${foot}</div></div>`;
}

/* ---------- script */
function scriptHTML(s) {
  const total = s.scenes.reduce((a, sc, si) => a + sc.lines.reduce((b, l, li) => b + (l.duration || 0) + (li < sc.lines.length - 1 ? linePause(l) : 0), 0) + (si < s.scenes.length - 1 ? scenePause(sc) : 0), 0);
  const n = s.scenes.length;
  const scenes = s.scenes.map((sc, si) => {
    const editing = state.sceneEdit === si;
    const tools = `<span class="scene-tools">
        <button class="icon-btn" data-scene-move="${si},-1" title="Move scene up" ${si === 0 ? "disabled" : ""}>${TOOL.up}</button>
        <button class="icon-btn" data-scene-move="${si},1" title="Move scene down" ${si === n - 1 ? "disabled" : ""}>${TOOL.down}</button>
        <button class="icon-btn" data-scene-edit="${si}" title="Edit scene details">${ICON.edit}</button>
        <button class="icon-btn" data-scene-del="${si}" title="Delete scene">${TOOL.trash}</button></span>`;
    const body = editing
      ? `<div class="scene-form">
          <div class="row2"><input class="field" id="scTitle" data-keep="scTitle" value="${esc(sc.title)}" placeholder="Scene title"/>
            <input class="field" id="scMood" data-keep="scMood" value="${esc(sc.mood)}" placeholder="Mood"/></div>
          <textarea class="field" id="scVisual" data-keep="scVisual" placeholder="What we see on screen">${esc(sc.visual)}</textarea>
          <input class="field" id="scCue" data-keep="scCue" value="${esc(sc.sound_cue || "")}" placeholder="Sound / music idea (optional)"/>
          <div class="acts"><button class="btn small" data-scene-cancel>Cancel</button><button class="btn small primary" data-scene-save="${si}">Save scene</button></div></div>`
      : `<div class="visual"><svg viewBox="0 0 24 24"><rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/></svg><div>${esc(sc.visual) || `<i style="color:var(--faint)">No visual description yet</i>`}</div></div>
         ${sc.sound_cue ? `<div class="cue"><svg viewBox="0 0 24 24"><path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>${esc(sc.sound_cue)}</div>` : ""}`;
    const castOpts = state.project.cast.map((c) => `<option>${esc(c.name)}</option>`).join("");
    const addLine = state.addLineFor === si
      ? `<div class="add-line"><select class="field" id="alSpeaker" data-keep="alSpeaker">${castOpts}</select>
          <input class="field" id="alText" data-keep="alText" placeholder="What do they say? Hinglish or any script is fine"/>
          <button class="btn small" data-addline-cancel>Cancel</button><button class="btn small primary" data-addline-save="${si}">Add & voice</button></div>`
      : `<button class="btn ghost small add-line-btn" data-addline="${si}">${TOOL.plus} Add line</button>`;
    return `
    <article class="card scene ${editing ? "editing" : ""}" id="scene-${si}">
      <div class="scene-top"><span class="scene-num">SCENE ${si + 1}</span><span class="scene-title">${esc(sc.title)}</span>${tools}<span class="mood">${esc(sc.mood)}</span></div>
      ${body}
      <div class="lines">${sc.lines.length ? sc.lines.map((l, li) => lineHTML(l, si, li, sc.lines.length, n) + (li < sc.lines.length - 1 ? pauseHTML(l, si, li) : "")).join("") : `<div class="no-lines">No dialogue in this scene.</div>`}</div>
      ${addLine}
      ${sc.lines.some((l) => l.audio) ? `<div class="scene-actions"><button class="btn small" data-play-scene="${si}">${ICON.play} Play scene</button></div>` : ""}
    </article>${si < n - 1 ? scenePauseHTML(sc, si) : `<div class="scene-pause"><button class="add-scene" data-add-scene="${si}">+ Add scene at the end</button></div>`}`;
  }).join("");
  return `<div class="script-head"><h2>${esc(s.title)}</h2><p>${esc(s.logline)}</p>
      <div class="head-row"><span class="doc-sub">${s.scenes.length} scenes${total ? ` · ${fmtTime(total)} with pauses` : ""} · script by ${esc(s.model || "LLM")}</span>
        <div class="seg" role="group" aria-label="Script view">
          <button type="button" data-view="roman" class="${state.view === "roman" ? "on" : ""}" title="Hinglish in English letters, for reading">Hinglish</button>
          <button type="button" data-view="native" class="${state.view === "native" ? "on" : ""}" title="Exact text sent to Timbre, with Hindi in Devanagari">हिंदी</button>
        </div></div></div>${scenes}`;
}

function pauseHTML(l, si, li) {
  const k = `l:${si}:${li}`, v = linePause(l);
  if (state.pauseEdit === k) return `<div class="pause custom">pause <input id="pauseInput" type="number" min="0" max="30" step="0.05" value="${v}" data-pause-input="${k}"/> s</div>`;
  return `<div class="pause ${l.pause_after != null ? "custom" : ""}"><button data-pause="${k}" title="Set the pause after this line">${v.toFixed(2)}s pause</button></div>`;
}

function scenePauseHTML(sc, si) {
  const k = `s:${si}`, v = scenePause(sc);
  if (state.pauseEdit === k) return `<div class="scene-pause custom">pause before next scene <input id="pauseInput" type="number" min="0" max="30" step="0.1" value="${v}" data-pause-input="${k}"/> s</div>`;
  return `<div class="scene-pause ${sc.pause_after != null ? "custom" : ""}"><button data-pause="${k}" title="Set the pause between these scenes">${v.toFixed(1)}s between scenes</button>
    <button class="add-scene" data-add-scene="${si}" title="Insert a new scene here">+ scene</button></div>`;
}

function takesHTML(l, si, li) {
  const takes = l.takes || [];
  const chips = takes.map((t, i) => `<span class="take ${t.key === l.audio_key ? "on" : ""}">Take ${i + 1} · ${fmtTime(t.duration)}
      <button class="tp" data-take-play="${t.audio}" title="Listen">${ICON.play}</button>
      ${t.key === l.audio_key ? `<b style="padding:0 6px">✓ using</b>` : `<button data-take-use="${si},${li},${i}">Use</button>`}</span>`).join("");
  return `<div class="takes"><span class="doc-sub">Takes:</span>${chips}
    ${takes.length < 5 ? `<button class="btn ghost small" data-take-new="${si},${li}">+ Record another take</button>` : ""}
    <span class="doc-sub">Each take is a fresh performance of the same line. Pick the best one.</span></div>`;
}

function lineHTML(l, si, li, nLines = 1, nScenes = 1) {
  const editing = state.editing && state.editing[0] === si && state.editing[1] === li;
  const play = l.audio
    ? `<button class="play" data-play="${si},${li}" title="Play line">${ICON.play}</button>`
    : `<button class="play" disabled title="Not voiced yet">${busy() ? `<span class="spinner"></span>` : "–"}</button>`;
  return `<div class="line" id="line-${si}-${li}">${play}
    <div><div class="who"><span class="avatar" style="width:20px;height:20px;font-size:9.5px;background:${colorOf(l.char_id)}">${esc(initials(l.speaker))}</span>
      <b>${esc(l.speaker)}</b><span class="badge">${esc(l.emotion)}</span><span class="sep">·</span><span class="badge" title="Voice · language">${esc(l.voice_label)}, ${esc((state.meta.languages[l.language] || l.language).replace(/ (.*)/, ""))}</span>
      ${l.stale && l.audio ? `<span class="badge warn">needs re-voice</span>` : ""}</div>
      <div class="text">${richText(shown(l))}</div>
      ${l.spoken && state.view === "native" ? `<div class="doc-sub">Spoken with pronunciation rules: ${richText(l.spoken)}</div>` : ""}</div>
    <div class="line-side">${l.duration ? fmtTime(l.duration) : ""}${(l.takes || []).length > 1 ? ` · ${l.takes.length} takes` : ""}
      <button class="icon-btn" data-line-move="${si},${li},-1" title="Move up" ${si === 0 && li === 0 ? "disabled" : ""}>${TOOL.up}</button>
      <button class="icon-btn" data-line-move="${si},${li},1" title="Move down" ${si === nScenes - 1 && li === nLines - 1 ? "disabled" : ""}>${TOOL.down}</button>
      ${l.audio && !l.stale ? `<button class="icon-btn" data-takes="${si},${li}" title="Takes: record alternatives and pick the best">${TOOL.takes}</button>` : ""}
      ${l.audio ? `<a class="icon-btn" href="${l.download}" download title="Download this line">${ICON.dl}</a>` : ""}
      <button class="icon-btn" data-edit="${si},${li}" title="Edit & re-voice">${ICON.edit}</button>
      <button class="icon-btn" data-line-del="${si},${li}" title="Delete line">${TOOL.trash}</button></div>
    ${state.takesOpen === `${si},${li}` ? takesHTML(l, si, li) : ""}
    ${editing ? editHTML(l, si, li) : ""}</div>`;
}

function editHTML(l, si, li) {
  const m = state.meta;
  return `<div class="edit-box">
    <div class="doc-sub">Spoken text sent to Timbre. Write Hindi words in Devanagari for correct pronunciation.</div>
    <textarea id="editText" data-keep="editText">${esc(l.text)}</textarea>
    <div class="tag-pick">${m.tags.map((t) => `<button type="button" class="tag" data-insert-tag="${esc(t)}">${esc(t.slice(1, -1))}</button>`).join("")}</div>
    <div class="edit-row">
      <select id="editSpeaker" title="Who says this line">${state.project.cast.map((c) => `<option ${c.name === l.speaker ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
      <select id="editLang">${Object.keys(m.languages).map((k) => `<option value="${k}" ${k === l.language ? "selected" : ""}>${esc(m.languages[k])}</option>`).join("")}</select>
      <select id="editPace">${m.paces.map((p) => `<option ${p === l.pace ? "selected" : ""}>${p}</option>`).join("")}</select>
      <span style="flex:1"></span>
      <button type="button" class="btn small" data-cancel-edit>Cancel</button>
      <button type="button" class="btn small primary" data-save-edit="${si},${li}">Save & re-voice</button>
    </div></div>`;
}

/* ============================================================ composer + uploads */
const prompt = $("#prompt");
function autosize() {
  // Grow with the text up to 40% of the window; only then switch on the scrollbar.
  const max = Math.round(window.innerHeight * 0.4);
  prompt.style.height = "auto";
  const h = prompt.scrollHeight;
  prompt.style.height = Math.min(h, max) + "px";
  prompt.classList.toggle("scroll", h > max);
}
window.addEventListener("resize", autosize);
prompt.addEventListener("input", autosize);
prompt.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#fileInput").addEventListener("change", (e) => { if (e.target.files[0]) attach(e.target.files[0]); e.target.value = ""; });

async function attach(file) {
  if (!state.ep) await newStory();
  else if (state.ep.document && !confirm(`Replace the script of “${state.ep.name}”? To keep it, create a new episode instead.`)) return;
  try {
    const fd = new FormData(); fd.append("file", file);
    await api(epUrl("/document"), { method: "POST", body: fd });
    await refresh(); loadProjects(); schedulePoll();
    toast(`Converted “${file.name}”. Finding characters…`, "info");
  } catch (e) { toast(e.message); }
}

async function generate() {
  if (busy() || !state.ep?.document) return;
  try {
    await api(epUrl("/generate"), J("POST", { instructions: prompt.value.trim() }));
    prompt.value = ""; autosize();
    await refresh(); loadProjects(); schedulePoll();
    setTimeout(() => ($("#feed").scrollTop = $("#feed").scrollHeight), 50);
  } catch (err) { toast(err.message); }
}
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); if (!state.ep?.document) return toast("Attach a script first (📎)"); generate(); });

/* ---------- mic: live transcript over Gnani Prisma realtime, with REST upload as a fallback */
const rec = { ctx: null, stream: null, node: null, chunks: [], rate: 48000, t0: 0, timer: null,
  ws: null, live: false, base: "", finals: [], partial: "", closed: null };

function to16k(f32, inRate) {
  const ratio = inRate / 16000, n = Math.floor(f32.length / ratio), out = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * ratio), b = Math.min(f32.length, Math.floor((i + 1) * ratio)); let s = 0;
    for (let j = a; j < b; j++) s += f32[j];
    const v = Math.max(-1, Math.min(1, s / Math.max(1, b - a))); out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  return out.buffer;
}
function showLive() {
  const parts = [rec.base, ...rec.finals, rec.partial].filter((x) => x && x.trim());
  prompt.value = parts.join(" "); autosize();
  prompt.scrollTop = prompt.scrollHeight;
}
function openLive() {
  return new Promise((resolve) => {
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/stt`); } catch { return resolve(null); }
    ws.binaryType = "arraybuffer";
    const timer = setTimeout(() => resolve(null), 4000);
    rec.closed = new Promise((done) => {
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "speech_start" || m.type === "partial_transcript") { clearTimeout(timer); resolve(ws); }
        if (m.type === "partial_transcript") { rec.partial = m.text; showLive(); }
        else if (m.type === "final") { if (m.text) rec.finals.push(m.text); rec.partial = ""; showLive(); }
        else if (m.type === "error") { toast(m.message || "Live transcript error"); }
        else if (m.type === "closed") done();
      };
      ws.onclose = () => done();
    });
    ws.onopen = () => { clearTimeout(timer); resolve(ws); };
    ws.onerror = () => { clearTimeout(timer); resolve(null); };
  });
}
async function startRec() {
  try { rec.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }); }
  catch { toast("Microphone is blocked. Allow it in your browser's site settings."); return; }
  rec.ctx = new AudioContext(); rec.rate = rec.ctx.sampleRate;
  rec.base = prompt.value.trim(); rec.finals = []; rec.partial = ""; rec.chunks = [];
  rec.ws = await openLive();
  rec.live = !!rec.ws && rec.ws.readyState <= 1;
  const src = rec.ctx.createMediaStreamSource(rec.stream);
  rec.node = rec.ctx.createScriptProcessor(4096, 1, 1);
  rec.node.onaudioprocess = (ev) => {
    const f = new Float32Array(ev.inputBuffer.getChannelData(0));
    rec.chunks.push(f);
    if (rec.live && rec.ws.readyState === 1) rec.ws.send(to16k(f, rec.rate));
  };
  src.connect(rec.node); rec.node.connect(rec.ctx.destination);
  rec.t0 = Date.now(); $("#micBtn").classList.add("recording");
  $("#composer").classList.toggle("live", rec.live);
  if (rec.live) { prompt.placeholder = "Listening… speak in any language"; }
  rec.timer = setInterval(() => { const s = (Date.now() - rec.t0) / 1000; $("#recTime").textContent = fmtTime(s); if (s > 300) stopRec(); }, 250);
}
function encodeWav(chunks, inRate, outRate = 16000) {
  const len = chunks.reduce((a, c) => a + c.length, 0), all = new Float32Array(len); let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const ratio = inRate / outRate, n = Math.floor(len / ratio), buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (p, s) => [...s].forEach((ch, i) => v.setUint8(p + i, ch.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, outRate, true); v.setUint32(28, outRate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * ratio), b = Math.min(len, Math.floor((i + 1) * ratio)); let sum = 0;
    for (let j = a; j < b; j++) sum += all[j];
    const s = Math.max(-1, Math.min(1, sum / Math.max(1, b - a))); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}
async function stopRec() {
  clearInterval(rec.timer);
  const mic = $("#micBtn"); mic.classList.remove("recording"); mic.classList.add("busy"); $("#recTime").textContent = "…";
  rec.node.disconnect(); rec.stream.getTracks().forEach((t) => t.stop()); await rec.ctx.close();
  $("#composer").classList.remove("live");
  if (rec.live) {
    try { rec.ws.send(JSON.stringify({ type: "stop" })); } catch {}
    await Promise.race([rec.closed, new Promise((r) => setTimeout(r, 5000))]);
    try { rec.ws.close(); } catch {}
    rec.partial = ""; showLive();
    const got = rec.finals.join(" ").trim();
    mic.classList.remove("busy"); $("#recTime").textContent = ""; rec.ctx = null; renderComposer();
    if (got) { prompt.focus(); toast("Live transcript by Gnani Prisma", "info"); return; }
    // nothing came back live: fall through and try the REST upload of the same recording
    prompt.value = rec.base; autosize();
  }
  try {
    if ((Date.now() - rec.t0) / 1000 < 0.6) throw new Error("Recording too short. Tap the mic, speak, then tap again.");
    const fd = new FormData(); fd.append("file", encodeWav(rec.chunks, rec.rate), "speech.wav");
    const r = await api("/api/stt", { method: "POST", body: fd });
    if (!r.transcript) throw new Error("Prisma didn't hear any speech. Try again closer to the mic.");
    prompt.value = (prompt.value.trim() ? prompt.value.trim() + " " : "") + r.transcript; autosize(); prompt.focus();
    toast(`Transcribed by Gnani Prisma${r.languages?.length ? " · " + r.languages.join(", ") : ""}`, "info");
  } catch (e) { toast(e.message); }
  mic.classList.remove("busy"); $("#recTime").textContent = ""; rec.ctx = null;
}
$("#micBtn").addEventListener("click", () => (rec.ctx ? stopRec() : startRec()));

/* ============================================================ playback */
const audio = new Audio();
const preview = new Audio();
const player = { queue: [], i: 0, active: false, gapTimer: null };

function queueFrom(onlyScene = null) {
  const out = [];
  state.ep.script.scenes.forEach((sc, si) => { if (onlyScene === null || si === onlyScene) sc.lines.forEach((l, li) => l.audio && out.push([si, li])); });
  return out;
}
function nowHTML(si, li) {
  const l = state.ep.script.scenes[si].lines[li];
  return `<b>Scene ${si + 1}</b> · ${esc(l.speaker)} · <span style="color:var(--muted)">${esc(shown(l).replace(/<[^>]+>/g, "").slice(0, 90))}</span>`;
}
function playQueue(q) {
  if (!q.length) return;
  preview.pause();
  player.queue = q; player.i = 0; player.active = true; $("#player").hidden = false; playCurrent();
}
function playCurrent() {
  clearTimeout(player.gapTimer);
  const [si, li] = player.queue[player.i];
  audio.src = state.ep.script.scenes[si].lines[li].audio; audio.play().catch(() => {});
  $("#pNow").innerHTML = nowHTML(si, li); setPauseIcon(true); markPlaying(true);
}
function gapAfter(si, li) {
  const sc = state.ep.script.scenes[si];
  return li < sc.lines.length - 1 ? linePause(sc.lines[li]) : scenePause(sc);
}
function markPlaying(scrollTo = false) {
  document.querySelectorAll(".line.playing, .scene.playing").forEach((e) => e.classList.remove("playing"));
  if (!player.active) return;
  const [si, li] = player.queue[player.i] || [];
  const line = document.getElementById(`line-${si}-${li}`);
  line?.classList.add("playing"); document.getElementById(`scene-${si}`)?.classList.add("playing");
  if (scrollTo && player.queue.length > 1) line?.scrollIntoView({ block: "center", behavior: "smooth" });
}
function stopPlayback() { clearTimeout(player.gapTimer); audio.pause(); player.active = false; $("#player").hidden = true; markPlaying(); }
function setPauseIcon(playing) {
  $("#player").classList.toggle("paused", !playing);
  $("#pIcon").innerHTML = playing ? `<path d="M8 5h3v14H8zM13 5h3v14h-3z" class="fill"/>` : `<path d="M7 5l12 7-12 7z" class="fill"/>`;
}
audio.addEventListener("ended", () => {
  if (player.i < player.queue.length - 1) {
    const [si, li] = player.queue[player.i];
    player.i++; player.gapTimer = setTimeout(playCurrent, gapAfter(si, li) * 1000);
  } else { setPauseIcon(false); player.active = false; markPlaying(); setTimeout(() => !player.active && ($("#player").hidden = true), 1500); }
});
audio.addEventListener("timeupdate", () => { $("#pFill").style.width = audio.duration ? `${(100 * audio.currentTime) / audio.duration}%` : "0"; });
$("#pToggle").onclick = () => {
  if (!player.active) return playQueue(player.queue);
  if (audio.paused) { audio.play(); setPauseIcon(true); } else { audio.pause(); clearTimeout(player.gapTimer); setPauseIcon(false); }
};
$("#pNext").onclick = () => { if (player.i < player.queue.length - 1) { player.i++; playCurrent(); } };
$("#pPrev").onclick = () => { if (audio.currentTime > 2 || player.i === 0) audio.currentTime = 0; else { player.i--; playCurrent(); } };
$("#pClose").onclick = stopPlayback;
$("#playAll").onclick = () => playQueue(queueFrom());

function playPreview(url, btn) {
  if (btn.classList.contains("playing")) { preview.pause(); btn.classList.remove("playing"); btn.innerHTML = ICON.play; return; }
  document.querySelectorAll("[data-sample-voice].playing").forEach((b) => { b.classList.remove("playing"); b.innerHTML = ICON.play; });
  if (player.active) { audio.pause(); setPauseIcon(false); }
  btn.classList.add("playing"); btn.innerHTML = `<span class="spinner"></span>`;
  preview.src = url;
  preview.oncanplay = () => { if (btn.classList.contains("playing")) btn.innerHTML = ICON.stop; };
  preview.onended = preview.onerror = () => { btn.classList.remove("playing"); btn.innerHTML = ICON.play; if (preview.error) toast("Couldn't load the voice sample"); };
  preview.play().catch(() => {});
}
const sampleUrl = (voice) => voice.startsWith("clone:") ? `/api/projects/${state.project.id}/clones/${voice.slice(6)}/sample.wav` : `/api/voice-sample/${voice}.wav`;

/* ============================================================ cast actions */
async function saveChar(cid, patch) {
  try { state.project = await api(`/api/projects/${state.project.id}/cast/${cid}`, J("PUT", patch)); state.ep = await api(epUrl()); render(); }
  catch (e) { toast(e.message); render(true); }
}
document.addEventListener("change", (e) => {
  const row = e.target.closest("[data-char]");
  if (row && e.target.dataset.field) {
    const f = e.target.dataset.field, v = e.target.value.trim();
    if (f === "name" && !v) return render(true);
    saveChar(row.dataset.char, { [f]: v });
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("[data-char] input")) e.target.blur();
  if (e.target.matches("[data-pause-input]")) {
    if (e.key === "Enter") e.target.blur();
    if (e.key === "Escape") { state.pauseEdit = null; render(); }
  }
});
async function commitPause(e) {
  if (e.target.matches("[data-pause-input]")) {
    if (e.target.dataset.done) return;  // change + focusout both fire; save once
    e.target.dataset.done = "1";
    const k = e.target.dataset.pauseInput, v = parseFloat(e.target.value);
    state.pauseEdit = null;
    if (!isNaN(v)) {
      const [kind, si, li] = k.split(":");
      try {
        await api(kind === "s" ? epUrl(`/scenes/${si}`) : epUrl(`/lines/${si}/${li}`), J("PATCH", { pause_after: v }));
        state.ep = await api(epUrl());
      } catch (err) { toast(err.message); }
    }
    render(true);
  }
}
document.addEventListener("focusout", commitPause);
document.addEventListener("change", commitPause);
$("#cloneInput").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f || !state.cloneFor) return;
  const c = state.project.cast.find((x) => x.id === state.cloneFor);
  const fd = new FormData(); fd.append("file", f); fd.append("char_id", state.cloneFor); fd.append("name", `${c?.name || "My"} voice`);
  toast("Cloning voice with Gnani… this takes a few seconds", "info");
  try { state.project = await api(`/api/projects/${state.project.id}/clones`, { method: "POST", body: fd }); state.ep = await api(epUrl()); render(true); toast(`Cloned voice assigned to ${c?.name}. Press ▶ to hear it.`, "info"); }
  catch (err) { toast(err.message); }
});

/* ============================================================ click handling */
document.addEventListener("click", async (e) => {
  if (!e.target.closest(".menu-wrap")) document.querySelectorAll(".menu").forEach((m) => (m.hidden = true));
  const t = e.target.closest("[data-story],[data-ep],[data-add-ep],[data-sample],[data-play],[data-play-scene],[data-edit],[data-cancel-edit],[data-save-edit],[data-insert-tag],[data-view],[data-toggle-cast],[data-sample-voice],[data-clone],[data-del-char],[data-add-char],[data-proceed],[data-revoice],[data-pause],[data-rename],#drop,#docToggle," +
    "[data-toggle-lex],[data-lex-add],[data-lex-del],[data-lex-play],[data-lex-save],[data-lex-cancel],[data-paste-open],[data-paste-cancel],[data-paste-use]," +
    "[data-takes],[data-take-play],[data-take-use],[data-take-new],[data-line-move],[data-line-del],[data-scene-move],[data-scene-edit],[data-scene-del]," +
    "[data-scene-cancel],[data-scene-save],[data-addline],[data-addline-cancel],[data-addline-save],[data-add-scene],[data-restore]");
  if (!t) return;
  const d = t.dataset;
  if (d.story) return toggleStory(d.story);
  if (d.ep) { const [pid, eid] = d.ep.split(":"); return openEpisode(pid, eid); }
  if (d.addEp) return newEpisode(d.addEp);
  if (d.sample) {
    try {
      const r = await fetch(`/samples/${d.sample}`); if (!r.ok) throw new Error("Sample not found");
      const blob = await r.blob();
      if (!state.ep || state.ep.document) await newStory();
      return attach(new File([blob], d.sample, { type: "text/plain" }));
    } catch (err) { return toast(err.message); }
  }
  if (t.id === "drop") return $("#fileInput").click();
  if (t.id === "docToggle") { e.preventDefault(); const c = $("#docCard"); c.classList.toggle("open"); await showFullDoc(); t.textContent = c.classList.contains("open") ? "hide text" : "show text"; return; }
  if (d.rename) return startRename(t, d.rename);
  if ("toggleCast" in d) { if (!inReview()) { state.castOpen = !state.castOpen; render(); } return; }
  if (d.sampleVoice) return playPreview(sampleUrl(d.sampleVoice), t);
  if (d.clone) { state.cloneFor = d.clone; return $("#cloneInput").click(); }
  if (d.delChar) {
    const c = state.project.cast.find((x) => x.id === d.delChar);
    if (!confirm(`Remove ${c.name} from the cast? Their lines will use the Narrator's voice until reassigned.`)) return;
    try { state.project = await api(`/api/projects/${state.project.id}/cast/${d.delChar}`, { method: "DELETE" }); state.ep = await api(epUrl()); render(); } catch (err) { toast(err.message); }
    return;
  }
  if ("addChar" in d) {
    const name = (window.prompt("New character name") || "").trim(); if (!name) return;
    try { state.project = await api(`/api/projects/${state.project.id}/cast`, J("POST", { name, gender: "unknown" })); render(); } catch (err) { toast(err.message); }
    return;
  }
  if ("proceed" in d) return generate();
  const act = async (fn, okMsg) => { try { await fn(); await refresh(); schedulePoll(); if (okMsg) toast(okMsg, "info"); } catch (err) { toast(err.message); } };
  const nums = (s) => s.split(",").map(Number);

  /* pronunciation dictionary */
  if ("toggleLex" in d) { state.lexOpen = !state.lexOpen; return render(); }
  if ("lexAdd" in d) { state.lexDraft = [...readLexDraft(), { from: "", to: "" }]; return render(true); }
  if (d.lexDel) { const dr = readLexDraft(); dr.splice(+d.lexDel, 1); state.lexDraft = dr; return render(true); }
  if ("lexCancel" in d) { state.lexDraft = null; return render(true); }
  if (d.lexPlay) {
    const it = readLexDraft()[+d.lexPlay]; if (!it?.to.trim()) return toast("Type how it should be said first");
    try {
      const r = await fetch(`/api/projects/${state.project.id}/lexicon/preview`, J("POST", { items: [it] }));
      if (!r.ok) throw new Error((await r.json()).detail || "Preview failed");
      preview.src = URL.createObjectURL(await r.blob()); preview.play();
    } catch (err) { toast(err.message); }
    return;
  }
  if ("lexSave" in d) {
    try {
      state.project = await api(`/api/projects/${state.project.id}/lexicon`, J("PUT", { items: readLexDraft() }));
      state.lexDraft = null; state.ep = await api(epUrl()); render(true);
      toast(state.ep.stale_lines ? `Saved. ${state.ep.stale_lines} line(s) will sound different; press Re-voice.` : "Saved", "info");
    } catch (err) { toast(err.message); }
    return;
  }
  /* paste text */
  if ("pasteOpen" in d) { state.pasteOpen = true; render(true); setTimeout(() => $("#pasteText")?.focus(), 30); return; }
  if ("pasteCancel" in d) { state.pasteOpen = false; return render(true); }
  if ("pasteUse" in d) {
    const text = $("#pasteText").value, title = $("#pasteTitle").value;
    if (text.trim().length < 10) return toast("Paste at least a sentence or two");
    if (!state.ep) await newStory();
    try { await api(epUrl("/document-text"), J("POST", { text, title })); state.pasteOpen = false; await refresh(); loadProjects(); schedulePoll(); toast("Text added. Finding characters…", "info"); }
    catch (err) { toast(err.message); }
    return;
  }
  /* takes */
  if (d.takes) { state.takesOpen = state.takesOpen === d.takes ? null : d.takes; return render(); }
  if (d.takePlay) { if (player.active) { audio.pause(); setPauseIcon(false); } preview.src = d.takePlay; preview.play(); return; }
  if (d.takeUse) { const [si, li, k] = nums(d.takeUse); return act(() => api(epUrl(`/lines/${si}/${li}`), J("PATCH", { take: k }))); }
  if (d.takeNew) { const [si, li] = nums(d.takeNew); return act(() => api(epUrl(`/lines/${si}/${li}/takes`), { method: "POST" })); }
  /* storyboard */
  if (d.lineMove) { const [si, li, dir] = nums(d.lineMove); return act(() => api(epUrl(`/lines/${si}/${li}/move`), J("POST", { dir }))); }
  if (d.lineDel) {
    const [si, li] = nums(d.lineDel); if (!confirm("Delete this line? You can undo it.")) return;
    return act(() => api(epUrl(`/lines/${si}/${li}`), { method: "DELETE" }));
  }
  if (d.sceneMove) { const [si, dir] = nums(d.sceneMove); return act(() => api(epUrl(`/scenes/${si}/move`), J("POST", { dir }))); }
  if (d.sceneEdit) { state.sceneEdit = +d.sceneEdit; return render(); }
  if ("sceneCancel" in d) { state.sceneEdit = null; return render(); }
  if (d.sceneSave) {
    const si = +d.sceneSave;
    return act(async () => {
      await api(epUrl(`/scenes/${si}`), J("PATCH", { title: $("#scTitle").value, mood: $("#scMood").value, visual: $("#scVisual").value, sound_cue: $("#scCue").value }));
      state.sceneEdit = null;
    });
  }
  if (d.sceneDel) {
    const si = +d.sceneDel; if (!confirm(`Delete scene ${si + 1} and its lines? You can undo it.`)) return;
    return act(() => api(epUrl(`/scenes/${si}`), { method: "DELETE" }));
  }
  if (d.addScene) {
    const si = +d.addScene;
    return act(async () => { const r = await api(epUrl("/scenes"), J("POST", { after: si })); state.sceneEdit = r.index; });
  }
  if (d.addline) { state.addLineFor = +d.addline; render(); setTimeout(() => $("#alText")?.focus(), 30); return; }
  if ("addlineCancel" in d) { state.addLineFor = null; return render(); }
  if (d.addlineSave) {
    const si = +d.addlineSave, text = $("#alText").value.trim(), speaker = $("#alSpeaker").value;
    if (!text) return toast("Type what they say");
    const after = state.ep.script.scenes[si].lines.length - 1;
    state.addLineFor = null;
    return act(() => api(epUrl(`/scenes/${si}/lines`), J("POST", { after, speaker, text })));
  }
  /* history */
  if (d.restore) { $("#historyMenu").hidden = true; return act(() => api(epUrl(`/versions/${d.restore}/restore`), { method: "POST" }), "Restored. The version you replaced is saved in history too."); }
  if ("revoice" in d) { try { await api(epUrl("/revoice"), { method: "POST" }); await refresh(); schedulePoll(); } catch (err) { toast(err.message); } return; }
  if (d.pause) { state.pauseEdit = d.pause; return render(); }
  if (d.play) {
    const [si, li] = d.play.split(",").map(Number), cur = player.queue[player.i];
    if (player.active && cur && cur[0] === si && cur[1] === li && !audio.paused) { audio.pause(); setPauseIcon(false); return; }
    return playQueue([[si, li]]);
  }
  if (d.playScene) return playQueue(queueFrom(+d.playScene));
  if (d.edit) { if (busy()) return toast("Wait for the current step to finish"); state.editing = d.edit.split(",").map(Number); return render(); }
  if ("cancelEdit" in d) { state.editing = null; return render(); }
  if (d.view) { state.view = d.view; store.set("swar:view", d.view); render(); if (player.active) $("#pNow").innerHTML = nowHTML(...player.queue[player.i]); return; }
  if (d.insertTag) {
    const ta = $("#editText"), pos = ta.selectionStart ?? ta.value.length;
    ta.value = ta.value.slice(0, pos) + d.insertTag + " " + ta.value.slice(pos); ta.focus(); ta.selectionStart = ta.selectionEnd = pos + d.insertTag.length + 1; return;
  }
  if (d.saveEdit) {
    const [si, li] = d.saveEdit.split(",").map(Number);
    try {
      await api(epUrl(`/lines/${si}/${li}`), J("PATCH", { text: $("#editText").value, language: $("#editLang").value, pace: $("#editPace").value, speaker: $("#editSpeaker").value }));
      state.editing = null; await refresh(); schedulePoll();
    } catch (err) { toast(err.message); }
  }
});

async function showFullDoc() {
  const c = $("#docCard");
  if (!c.classList.contains("open") || c.dataset.full) return;
  try { const r = await fetch(epUrl("/source")); if (r.ok) { c.querySelector(".doc-preview").textContent = await r.text(); c.dataset.full = 1; } } catch {}
}

function startRename(el, which) {
  el.contentEditable = "true"; el.focus(); document.getSelection().selectAllChildren(el);
  const done = async () => {
    el.contentEditable = "false"; el.removeEventListener("blur", done);
    const name = el.textContent.trim();
    const cur = which === "story" ? state.project.name : state.ep.name;
    if (!name || name === cur) { el.textContent = cur; return; }
    try {
      await api(which === "story" ? `/api/projects/${state.project.id}` : epUrl(), J("PATCH", { name }));
      await refresh(); loadProjects();
    } catch (err) { toast(err.message); }
  };
  el.addEventListener("blur", done);
  el.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); el.blur(); } if (ev.key === "Escape") { el.textContent = which === "story" ? state.project.name : state.ep.name; el.blur(); } };
}

/* menus */
$("#exportBtn").onclick = () => { $("#moreMenu").hidden = true; $("#exportMenu").hidden = !$("#exportMenu").hidden; };
$("#moreBtn").onclick = () => { $("#exportMenu").hidden = true; $("#moreMenu").hidden = !$("#moreMenu").hidden; };
$("#delEpisode").onclick = async () => {
  if (!confirm(`Delete “${state.ep.name}”? The story and its cast stay.`)) return;
  const pid = state.project.id;
  const eid = state.ep.id;
  await api(epUrl(), { method: "DELETE" }); stopPlayback();
  await mirror.dropPrefix(`${pid}/episodes/${eid}`).catch(() => {});
  await loadProjects();
  const p = state.projects.find((x) => x.id === pid);
  if (p?.episodes.length) openEpisode(pid, p.episodes[0].id); else { state.ep = null; render(true); }
};
$("#delProject").onclick = async () => {
  if (!confirm(`Delete the whole story “${state.project.name}”, all its episodes, voices and audio?`)) return;
  const gone = state.project.id;
  await api(`/api/projects/${gone}`, { method: "DELETE" }); stopPlayback();
  await mirror.dropPrefix(`${gone}/`).catch(() => {});
  state.project = state.ep = null; await loadProjects(); render(true);
};
$("#newProject").onclick = () => newStory();

/* undo + history */
$("#undoBtn").onclick = async () => {
  if (busy() || !state.ep?.versions) return;
  try { const r = await api(epUrl("/undo"), { method: "POST" }); await refresh(); toast(`Undone (${r.label.replace(/^Before /, "")})`, "info"); }
  catch (err) { toast(err.message); }
};
$("#historyBtn").onclick = async () => {
  const m = $("#historyMenu");
  $("#exportMenu").hidden = $("#moreMenu").hidden = true;
  if (!m.hidden) { m.hidden = true; return; }
  m.innerHTML = `<div class="empty"><span class="spinner"></span></div>`; m.hidden = false;
  try {
    const vs = await api(epUrl("/versions"));
    const when = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    m.innerHTML = `<div class="menu-label">Saved before each change · newest first</div>` + (vs.length ? vs.map((v) => `<div class="h-item"><div>${esc(v.label)}
        <small>${when(v.at)} · ${v.scenes} scenes, ${v.lines} lines</small></div><button class="btn small" data-restore="${v.id}">Restore</button></div>`).join("")
      : `<div class="empty">No earlier versions yet. Every revision, edit, move or delete saves one here.</div>`);
  } catch (err) { m.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
};
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.target.closest("input,textarea,[contenteditable=true]")) { e.preventDefault(); $("#undoBtn").click(); }
});

/* settings + usage */
const fmtNum = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n)));
function renderUsage(u) {
  $("#usageMini").classList.toggle("on", !!(u.tts_chars || u.stt_seconds));
  $("#usageMini").innerHTML = `Used: <b>${fmtNum(u.tts_chars)}</b> voice chars · <b>${fmtNum(u.stt_seconds)}s</b> listening`;
  $("#usageGrid").innerHTML = [
    [fmtNum(u.tts_chars), "characters voiced (Timbre)"], [u.tts_requests, "voice requests"], [`${fmtNum(u.stt_seconds)}s`, "speech heard (Prisma)"],
    [u.stt_requests, "transcriptions"], [u.clone_requests, "voice clones"], [u.llm_requests, "script model calls"],
  ].map(([v, l]) => `<div><b>${v}</b><span>${l}</span></div>`).join("");
}
async function loadUsage() { try { const s = await api("/api/settings"); renderUsage(s.usage); return s; } catch { return null; } }
async function openSettings() {
  const s = await loadUsage(); if (!s) return;
  $("#gnaniMask").textContent = s.gnani ? `current: ${s.gnani}` : "not set";
  $("#geminiMask").textContent = s.gemini ? `current: ${s.gemini}` : "not set";
  $("#gnaniKey").value = $("#geminiKey").value = ""; $("#geminiModel").value = s.gemini_model; $("#testResult").textContent = "";
  $("#settingsModal").hidden = false;
}
$("#settingsBtn").onclick = openSettings;
$("#setClose").onclick = () => ($("#settingsModal").hidden = true);
$("#settingsModal").addEventListener("click", (e) => { if (e.target.id === "settingsModal") $("#settingsModal").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#settingsModal").hidden = true; });
$("#saveKeys").onclick = async () => {
  const body = {};
  if ($("#gnaniKey").value.trim()) body.gnani_api_key = $("#gnaniKey").value.trim();
  if ($("#geminiKey").value.trim()) body.gemini_api_key = $("#geminiKey").value.trim();
  if ($("#geminiModel").value.trim()) body.gemini_model = $("#geminiModel").value.trim();
  try {
    if (state.meta.hosted) {
      // Hosted: keys stay in this browser. The server only checks they look valid; it never stores them.
      await api("/api/settings", J("PUT", body));
      if (body.gnani_api_key) store.set(KEYS.gnani, body.gnani_api_key);
      if (body.gemini_api_key) store.set(KEYS.gemini, body.gemini_api_key);
      if (body.gemini_model) store.set(KEYS.model, body.gemini_model);
      setCookies();
    }
    const s = state.meta.hosted ? await api("/api/settings") : await api("/api/settings", J("PUT", body));
    state.meta.keys = s; $("#llmName").textContent = s.gemini_model;
    await openSettings(); $("#testResult").innerHTML = `<span class="ok-text">Saved.</span> Press Test to check them.`;
  } catch (err) { $("#testResult").innerHTML = `<span class="err-text">${esc(err.message)}</span>`; }
};
$("#testKeys").onclick = async () => {
  $("#testResult").innerHTML = `<span class="spinner"></span> Testing…`;
  try {
    const r = await api("/api/settings/test", { method: "POST" });
    const f = (name, x) => `<span class="${x.ok ? "ok-text" : "err-text"}">${name}: ${esc(x.message)}</span>`;
    $("#testResult").innerHTML = `${f("Gnani", r.gnani)} · ${f("Gemini", r.gemini)}`; loadUsage();
  } catch (err) { $("#testResult").innerHTML = `<span class="err-text">${esc(err.message)}</span>`; }
};
$("#resetUsage").onclick = async () => { if (confirm("Reset the usage counters?")) { renderUsage(await api("/api/usage/reset", { method: "POST" })); } };
/* mobile sidebar: a scrim behind it closes it on any outside tap */
const scrim = document.createElement("div");
scrim.className = "side-scrim";
document.body.appendChild(scrim);
function setSide(open) { $("#sidebar").classList.toggle("open", open); scrim.classList.toggle("on", open); }
$("#toggleSide").onclick = () => setSide(!$("#sidebar").classList.contains("open"));
scrim.onclick = () => setSide(false);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setSide(false); });

/* drag & drop */
document.addEventListener("dragover", (e) => { e.preventDefault(); $("#drop")?.classList.add("over"); });
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) $("#drop")?.classList.remove("over"); });
document.addEventListener("drop", (e) => { e.preventDefault(); $("#drop")?.classList.remove("over"); const f = e.dataTransfer.files[0]; if (f) attach(f); });

/* ============================================================ boot */
(async function boot() {
  state.meta = await api("/api/meta");
  $("#llmName").textContent = state.meta.llm;
  if (state.meta.hosted) {
    document.body.classList.add("hosted");
    navigator.storage?.persist?.().catch(() => {});
    await syncNow();  // bring this browser's saved work back onto the server if it's not there
    setInterval(() => { if (busy()) syncNow(); }, 15000);
    $("#keyNote").innerHTML = "Keys are saved only in <b>this browser</b> and sent with your own requests to Gnani and Google. The server never stores them. Your stories are kept in this browser too.";
  }
  await loadProjects();
  const [pid, eid] = (store.get("swar:last") || "").split(":");
  const p = state.projects.find((x) => x.id === pid);
  if (p && p.episodes.some((e) => e.id === eid)) await openEpisode(pid, eid);
  else if (state.projects[0]?.episodes[0]) await openEpisode(state.projects[0].id, state.projects[0].episodes[0].id);
  else render(true);
  loadUsage();
  setInterval(loadUsage, 15000);
  if (!state.meta.keys.gnani || !state.meta.keys.gemini) {
    openSettings();
    setTimeout(() => ($("#testResult").innerHTML = `<span class="err-text">Add your API keys to get started.</span>`), 100);
  }
})();
