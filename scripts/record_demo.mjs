#!/usr/bin/env node
// Capture the courtroom editor and turn the shots into a captioned demo.
// Requires the local service on $DEMO_URL (default http://127.0.0.1:8775)
// and ffmpeg on PATH for the mp4 step (--frames-only skips encoding).

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "docs", "demo");
const FRAMES = path.join(OUT, "frames");
const DEMO_URL = process.env.DEMO_URL || "http://127.0.0.1:8775/";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9223;

mkdirSync(FRAMES, { recursive: true });

function chromeProfile() {
  return path.join(ROOT, "var", "demo", "chrome-profile");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (!msg.id || !this.pending.has(msg.id)) return;
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result || {});
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(JSON.stringify(result.exceptionDetails.exception || result.exceptionDetails));
    }
    return result.result?.value;
  }
}

async function waitForChrome() {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const list = await fetch(`http://127.0.0.1:${PORT}/json/list`).then((r) => r.json());
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* chrome still starting */
    }
    await sleep(200);
  }
  throw new Error("Chrome DevTools did not come up");
}

async function waitUntil(cdp, expression, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    last = await cdp.eval(expression);
    if (last === true) return;
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${label}: ${last}`);
}

function captionScript(now, next) {
  const payload = JSON.stringify({ now, next });
  return `(() => {
    const { now, next } = ${payload};
    let bar = document.getElementById("demo-caption");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "demo-caption";
      bar.style.cssText = [
        "position:fixed", "left:0", "right:0", "bottom:0", "z-index:99999",
        "background:#142033", "color:#f4f7fb", "padding:14px 28px 16px",
        "font:15px/1.45 -apple-system, BlinkMacSystemFont, sans-serif",
        "box-shadow:0 -8px 24px rgba(0,0,0,.12)"
      ].join(";");
      document.body.appendChild(bar);
      document.body.style.paddingBottom = "92px";
    }
    bar.replaceChildren();
    const n = document.createElement("div");
    n.textContent = "Now: " + now;
    n.style.fontWeight = "600";
    const x = document.createElement("div");
    x.textContent = "Next: " + next;
    x.style.opacity = "0.85";
    x.style.marginTop = "2px";
    bar.append(n, x);
    return true;
  })()`;
}

async function shoot(cdp, name, now, next) {
  await cdp.eval(captionScript(now, next));
  await sleep(150);
  const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
  const file = path.join(FRAMES, `${name}.png`);
  writeFileSync(file, Buffer.from(data, "base64"));
  console.log("frame", name);
  return file;
}

async function typeWord(cdp, word) {
  for (const ch of word) {
    await cdp.eval(`(() => {
      const ed = document.getElementById("editor");
      ed.focus();
      const pos = ed.selectionStart ?? ed.value.length;
      ed.setRangeText(${JSON.stringify(ch)}, pos, pos, "end");
      ed.dispatchEvent(new InputEvent("input", { bubbles: true, data: ${JSON.stringify(ch)}, inputType: "insertText" }));
      return ed.value;
    })()`);
    await sleep(40);
  }
  const q = word.toLowerCase();
  await waitUntil(
    cdp,
    `(() => {
      const roman = (document.getElementById("dd-roman").textContent || "").toLowerCase();
      const meta = document.getElementById("dd-meta").textContent || "";
      const open = !document.getElementById("dropdown").classList.contains("hidden");
      return open && roman === ${JSON.stringify(q)} && meta.includes("ms");
    })()`,
    15000,
    `candidates for ${word}`,
  );
}

async function press(cdp, key) {
  // The editor's keydown handler is async (it waits for the in-flight lookup).
  // dispatchEvent does not wait for that, so the next word must not start until
  // the dropdown has closed and the commit has landed.
  await cdp.eval(`(() => {
    const ed = document.getElementById("editor");
    ed.focus();
    ed.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(key)}, bubbles: true, cancelable: true }));
    return true;
  })()`);
  await waitUntil(
    cdp,
    `document.getElementById("dropdown").classList.contains("hidden")`,
    8000,
    `dropdown closed after ${key}`,
  );
  await sleep(150);
}

async function card(cdp, name, title, lines, now, next) {
  const html = `<!doctype html><meta charset="utf-8"><title>${title}</title>
  <body style="margin:0;background:#142033;color:#f4f7fb;font:20px/1.45 -apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh">
  <div style="max-width:860px;padding:48px">
    <div style="letter-spacing:.08em;text-transform:uppercase;font-size:13px;opacity:.7">IndicXlit courtroom runtime</div>
    <h1 style="font-size:40px;line-height:1.2;margin:12px 0 20px">${title}</h1>
    ${lines.map((l) => `<p style="margin:8px 0;font-size:20px;opacity:.92">${l}</p>`).join("")}
  </div></body>`;
  const file = path.join(ROOT, "var", "demo", `${name}.html`);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, html);
  await cdp.send("Page.navigate", { url: `file://${file}` });
  await sleep(400);
  await shoot(cdp, name, now, next);
}

async function main() {
  rmSync(chromeProfile(), { recursive: true, force: true });
  const chrome = spawn(CHROME, [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    `--window-size=1280,800`,
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${chromeProfile()}`,
    "about:blank",
  ], { stdio: "ignore" });

  try {
    const page = await waitForChrome();
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", reject);
    });
    const cdp = new Cdp(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1280, height: 800, deviceScaleFactor: 1, mobile: false,
    });

    await card(
      cdp, "01-title",
      "Suggestions on the typist's machine",
      [
        "The model runs next to the editor. The cloud only ships model packages.",
        "This recording uses a package already on disk. Central is not running.",
      ],
      "Opening the editor a courtroom typist would use.",
      "Show that suggestions work with the registry unreachable.",
    );

    await cdp.send("Page.navigate", { url: DEMO_URL });
    await waitUntil(
      cdp,
      `document.getElementById("pill-engine") && document.getElementById("pill-engine").textContent.includes("hi-")`,
      30000,
      "engine ready",
    );
    await waitUntil(
      cdp,
      `document.getElementById("pill-central").textContent.includes("offline")`,
      20000,
      "central offline",
    );
    await shoot(
      cdp, "02-ready",
      "The local engine is warm. Central is offline, and that is fine.",
      "Type a romanized Hindi word. The lookup stays on this machine.",
    );

    await typeWord(cdp, "adalat");
    await shoot(
      cdp, "03-typing",
      "Each letter asks the local model for a ranked list.",
      "Press Space. The highlighted suggestion is written into the document.",
    );

    await press(cdp, " ");
    await waitUntil(
      cdp,
      `document.getElementById("editor").value.includes(" ") && document.getElementById("dropdown").classList.contains("hidden")`,
      8000,
      "word committed",
    );
    await shoot(
      cdp, "04-commit",
      "Space accepted the top suggestion. The roman word is in the activity list.",
      "Finish the sentence the same way: ne, gawah, ko, bulaya.",
    );

    for (const word of ["ne", "gawah", "ko", "bulaya"]) {
      await typeWord(cdp, word);
      await press(cdp, " ");
      await sleep(200);
    }
    await shoot(
      cdp, "05-sentence",
      "A short courtroom line, converted word by word, with the registry down.",
      "Type FIR and press Esc, so a token that should stay roman is left alone.",
    );

    await typeWord(cdp, "FIR");
    await press(cdp, "Escape");
    await press(cdp, " ");
    await shoot(
      cdp, "06-keep-roman",
      "Esc kept FIR in roman. Names and abbreviations use the same key.",
      "What this demo does not show, and what comes after it.",
    );

    await card(
      cdp, "07-next",
      "What comes next",
      [
        "Set one compute thread per machine. On a shared computer that cut p95 from 65 ms to 29 ms.",
        "Score the official Dakshina Hindi list, and refuse a publish that drifts from the stock model.",
        "A court mirror, so sixteen PCs do not each pull 138 MB over one uplink.",
        "A container or a public URL. This video is the walkthrough until that exists.",
      ],
      "The keystroke path is local, and it survived a dead registry.",
      "Harden the install, prove the published quality number, and mirror packages per court.",
    );

    ws.close();
  } finally {
    chrome.kill("SIGTERM");
  }

  if (process.argv.includes("--frames-only")) {
    console.log("frames in", FRAMES);
    return;
  }
  encode();
}

function encode() {
  const list = path.join(OUT, "frames.txt");
  const shots = [
    ["01-title", 5],
    ["02-ready", 5],
    ["03-typing", 5],
    ["04-commit", 5],
    ["05-sentence", 6],
    ["06-keep-roman", 5],
    ["07-next", 8],
  ];
  const lines = [];
  for (const [name, seconds] of shots) {
    lines.push(`file 'frames/${name}.png'`);
    lines.push(`duration ${seconds}`);
  }
  lines.push(`file 'frames/${shots.at(-1)[0]}.png'`);
  writeFileSync(list, lines.join("\n") + "\n");
  const mp4 = path.join(OUT, "courtroom-editor-demo.mp4");
  const ff = spawn("ffmpeg", [
    "-y", "-f", "concat", "-safe", "0", "-i", list,
    "-vf", "format=yuv420p",
    "-movflags", "+faststart",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-crf", "23",
    mp4,
  ], { cwd: OUT, stdio: "inherit" });
  ff.on("exit", (code) => process.exit(code ?? 1));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
