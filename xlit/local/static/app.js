// Courtroom editor: keystroke suggestions from the local Xlit service.
//
// Interaction model
//   * The roman word being typed lives in the editor (client owns the prefix).
//   * Every keystroke inside a latin word fires /v1/suggest; stale responses are dropped.
//   * Space / punctuation accepts the highlighted candidate (top-1 by default) and
//     continues; Enter/Tab/digit/click accept without a trailing char; Esc keeps roman.
//   * Typing another letter just refines the prefix: "move forward with another stroke".
"use strict";

const ed = document.getElementById("editor");
const dd = document.getElementById("dropdown");
const ddList = document.getElementById("dd-list");
const ddRoman = document.getElementById("dd-roman");
const ddMeta = document.getElementById("dd-meta");
const activity = document.getElementById("activity");

const PUNCT = new Set([".", ",", "?", "!", ";", ":", "\"", "'", ")", "(", "-", "।"]);
const SESSION = Math.random().toString(36).slice(2, 10);

const st = {
  topk: 4,
  word: null,        // {start, end, text} of the latin run ending at the caret
  cands: [],         // candidates for st.forText
  forText: null,
  meta: null,        // last response metadata
  sel: 0,
  open: false,
  dismissed: false,  // Esc pressed for the current word
  seq: 0,
  pending: null,     // {text, promise}
  lastTier: null,
};

// ---------- word tracking ----------

function currentWord() {
  const pos = ed.selectionStart;
  if (pos !== ed.selectionEnd) return null;
  const m = ed.value.slice(0, pos).match(/[A-Za-z]+$/);
  if (!m) return null;
  if (/[A-Za-z]/.test(ed.value.charAt(pos))) return null; // caret mid-word
  return { start: pos - m[0].length, end: pos, text: m[0] };
}

function items() {
  const list = st.cands.map((c) => ({ text: c, raw: false }));
  if (st.word) list.push({ text: st.word.text, raw: true });
  return list;
}

// ---------- suggestions ----------

function requestSuggest(text) {
  const seq = ++st.seq;
  const t0 = performance.now();
  const url = `/v1/suggest?q=${encodeURIComponent(text.toLowerCase())}&k=${st.topk}&word_start=${text.length === 1 ? 1 : 0}`;
  const promise = fetch(url)
    .then(async (r) => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw Object.assign(new Error(body.error || `HTTP ${r.status}`), { body });
      return body;
    })
    .then((j) => {
      j.rtt_ms = performance.now() - t0;
      if (seq === st.seq) {
        st.cands = j.candidates;
        st.forText = text;
        st.meta = j;
        st.sel = 0;
        setLatency(j);
        render();
      }
      return j;
    })
    .catch((err) => {
      if (seq === st.seq) {
        st.cands = [];
        st.forText = null;
        renderError(err.body && err.body.error === "model_not_ready" ? "model not installed yet…" : "suggestions unavailable");
      }
      throw err;
    });
  st.pending = { text, promise };
  promise.catch(() => {});
  return promise;
}

// Resolve candidates for the word under the caret, waiting for an in-flight lookup
// if the typist hit space faster than the response arrived.
async function ensureFresh() {
  const text = st.word.text;
  if (st.forText === text) return true;
  try {
    if (st.pending && st.pending.text === text) await st.pending.promise;
    else await requestSuggest(text);
    return st.forText === text;
  } catch (_) {
    return false;
  }
}

// ---------- commit ----------

function commit(idx, source, trailing) {
  const w = st.word;
  const list = items();
  const item = list[Math.max(0, Math.min(idx, list.length - 1))];
  if (!w || !item) return;
  ed.setRangeText(item.text + trailing, w.start, w.end, "end");
  const rank = item.raw ? -1 : idx;
  addActivity(w.text, item.text, rank, source);
  postEvent({
    type: "commit", source: item.raw ? "raw" : source, roman: w.text, chosen: item.text,
    rank, shown: st.cands.length, rtt_ms: st.meta ? Math.round(st.meta.rtt_ms * 10) / 10 : null,
  });
  close();
}

function insertPlain(trailing) {
  const pos = ed.selectionStart;
  ed.setRangeText(trailing, pos, ed.selectionEnd, "end");
}

function postEvent(ev) {
  fetch("/v1/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...ev, session: SESSION }),
  })
    .then((r) => r.json())
    .then((j) => { if (j.promoted) pollStatus(); })
    .catch(() => {});
}

// ---------- rendering ----------

function open() {
  if (!st.word) return;
  st.open = true;
  dd.classList.remove("hidden");
  position();
}

function close() {
  st.open = false;
  st.word = null;
  st.cands = [];
  st.forText = null;
  st.sel = 0;
  st.seq++; // invalidate in-flight responses
  st.pending = null;
  dd.classList.add("hidden");
}

function render() {
  if (!st.word) return;
  ddRoman.textContent = st.word.text;
  if (st.meta && st.forText === st.word.text) {
    ddMeta.textContent = `${st.meta.tier}${st.meta.cached ? " · cached" : ""} · ${Math.round(st.meta.rtt_ms)} ms`;
  }
  ddList.innerHTML = "";
  const list = items();
  if (st.forText !== st.word.text && st.cands.length === 0) {
    const li = document.createElement("li");
    li.className = "placeholder";
    li.textContent = "…";
    ddList.appendChild(li);
  }
  list.forEach((it, i) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    if (i === st.sel) li.classList.add("sel");
    if (it.raw) li.classList.add("raw");
    li.innerHTML = `<span class="n"></span><span class="c"></span>${it.raw ? '<span class="tag">keep roman</span>' : ""}`;
    li.querySelector(".n").textContent = String(i + 1);
    li.querySelector(".c").textContent = it.text;
    li.addEventListener("mousedown", (e) => {
      e.preventDefault(); // keep focus in the editor
      st.word = currentWord() || st.word;
      commit(i, "click", "");
      ed.focus();
    });
    ddList.appendChild(li);
  });
  open();
}

function renderError(msg) {
  ddList.innerHTML = "";
  const li = document.createElement("li");
  li.className = "placeholder";
  li.textContent = msg;
  ddList.appendChild(li);
  ddMeta.textContent = "";
  open();
}

const mirrorProps = [
  "boxSizing", "width", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "fontStyle", "fontVariant", "fontWeight",
  "fontStretch", "fontSize", "lineHeight", "fontFamily", "textAlign", "textTransform", "textIndent",
  "letterSpacing", "wordSpacing", "tabSize",
];

// Pixel position of a character offset inside the textarea (mirror-div technique).
function caretXY(pos) {
  const cs = getComputedStyle(ed);
  const div = document.createElement("div");
  mirrorProps.forEach((p) => { div.style[p] = cs[p]; });
  Object.assign(div.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", wordWrap: "break-word", top: "0", left: "-9999px" });
  div.textContent = ed.value.slice(0, pos);
  const span = document.createElement("span");
  span.textContent = ed.value.slice(pos) || ".";
  div.appendChild(span);
  document.body.appendChild(div);
  const lineH = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
  const x = span.offsetLeft - ed.scrollLeft;
  const y = span.offsetTop - ed.scrollTop + lineH;
  document.body.removeChild(div);
  return { x, y };
}

function position() {
  const { x, y } = caretXY(st.word.start);
  const maxX = ed.clientWidth - dd.offsetWidth;
  dd.style.left = `${Math.max(0, Math.min(x, maxX))}px`;
  dd.style.top = `${y + 4}px`;
}

// ---------- events ----------

ed.addEventListener("input", () => {
  st.dismissed = false;
  const w = currentWord();
  if (!w) { close(); return; }
  st.word = w;
  if (st.forText !== w.text) {
    st.cands = st.forText && w.text.startsWith(st.forText) ? st.cands : [];
    render();
  }
  requestSuggest(w.text);
});

ed.addEventListener("keydown", async (e) => {
  if (e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (!st.open || st.dismissed) return;
  const w = currentWord();
  if (!w) { close(); return; }
  st.word = w;
  const n = items().length;

  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    st.sel = (st.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    render();
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    postEvent({ type: "dismiss", source: "escape", roman: w.text, shown: st.cands.length });
    addActivity(w.text, w.text, -1, "escape");
    st.dismissed = true;
    dd.classList.add("hidden");
    st.open = false;
    return;
  }
  if (/^[1-9]$/.test(e.key) && Number(e.key) <= n && st.forText === w.text) {
    e.preventDefault();
    commit(Number(e.key) - 1, "digit", "");
    return;
  }

  let source = null, trailing = "";
  if (e.key === " ") { source = "space"; trailing = " "; }
  else if (e.key === "Enter") source = "enter";
  else if (e.key === "Tab") source = "tab";
  else if (PUNCT.has(e.key)) { source = "punct"; trailing = e.key; }
  if (!source) return; // a letter: refine the prefix via the input handler

  e.preventDefault();
  const ok = await ensureFresh();
  if (ok && currentWord() && currentWord().text === w.text) {
    st.word = w;
    commit(st.sel, source, trailing);
  } else {
    close();
    if (trailing) insertPlain(trailing);
    else if (source === "enter") insertPlain("\n");
  }
});

["click", "blur"].forEach((evt) => ed.addEventListener(evt, () => {
  const w = currentWord();
  if (!w || !st.word || w.start !== st.word.start) close();
}));
ed.addEventListener("keyup", (e) => {
  if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
    const w = currentWord();
    if (!w || !st.word || w.text !== st.word.text) close();
  }
});
ed.addEventListener("scroll", () => { if (st.open) position(); });

// ---------- status bar ----------

function setPill(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = `pill ${cls || ""}`;
}

function setLatency(j) {
  const compute = j.cached ? "cached" : `${j.compute_ms.toFixed(1)} ms model`;
  setPill("pill-latency", `lookup: ${Math.round(j.rtt_ms)} ms (${compute})`, j.rtt_ms < 100 ? "ok" : "warn");
}

function fmtMB(b) { return `${(b / 1e6).toFixed(0)} MB`; }

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), 4000);
}

async function pollStatus() {
  try {
    const s = await (await fetch("/v1/status")).json();
    st.topk = (s.config && s.config.topk) || st.topk;
    const a = s.engine.active;
    if (a) {
      setPill("pill-engine", `engine: ${a.version} · ${a.tier}`, a.tier === "rescore" ? "ok" : "warn");
      if (st.lastTier && st.lastTier !== `${a.version}/${a.tier}`) {
        toast(`Switched to ${a.version} · ${a.tier} (applied at a word boundary)`);
      }
      st.lastTier = `${a.version}/${a.tier}`;
    } else {
      setPill("pill-engine", "engine: no model yet", "bad");
    }

    const staged = s.engine.staged;
    const pillStaged = document.getElementById("pill-staged");
    const loading = s.sync && s.sync.loading;
    if (staged) {
      pillStaged.textContent = `ready: ${staged.tier} · applies at next word`;
      pillStaged.classList.remove("hidden");
    } else if (loading) {
      const secs = Math.round(Date.now() / 1000 - loading.since);
      pillStaged.textContent = `loading ${loading.tier} engine… ${secs}s`;
      pillStaged.classList.remove("hidden");
    } else {
      pillStaged.classList.add("hidden");
    }

    if (s.sync) {
      const cs = s.sync.central_state;
      setPill("pill-central", `central: ${cs}${s.sync.target_version ? " · latest " + s.sync.target_version : ""}`,
        cs === "online" ? "ok" : cs === "offline" ? "bad" : "");
      const d = s.sync.download;
      const box = document.getElementById("download");
      if (d) {
        box.classList.remove("hidden");
        document.getElementById("download-label").textContent =
          `downloading ${d.package} package (${d.version}): ${fmtMB(d.done)} / ${fmtMB(d.total)}`;
        document.getElementById("download-fill").style.width = `${Math.min(100, (100 * d.done) / d.total)}%`;
      } else {
        box.classList.add("hidden");
      }
    }
  } catch (_) {
    setPill("pill-engine", "local service unreachable", "bad");
  }
}

function addActivity(roman, chosen, rank, source) {
  const li = document.createElement("li");
  li.innerHTML = '<span class="r"></span>→<span class="c"></span><span class="m"></span>';
  li.querySelector(".r").textContent = roman;
  li.querySelector(".c").textContent = chosen;
  li.querySelector(".m").textContent = rank < 0 ? `kept · ${source}` : `#${rank + 1} · ${source}`;
  activity.prepend(li);
  while (activity.children.length > 12) activity.removeChild(activity.lastChild);
}

document.getElementById("btn-sync").addEventListener("click", () => {
  fetch("/v1/sync", { method: "POST" }).then(() => toast("Asked the sync agent to check central"));
});

pollStatus();
setInterval(pollStatus, 2000);
ed.focus();
