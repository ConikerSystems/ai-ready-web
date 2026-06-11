/* Reusable Share + Feedback widget — drop-in for any of the static PWAs.
 *
 * Configure (optional) before this script loads:
 *   window.APP_INFO = { name, url, email, noFab }
 * Defaults: name = document.title, url = current URL, email = info@conikersystems.com.
 * If noFab is falsy, a floating 💬 button is injected (bottom-right). Apps that
 * want their own trigger can set noFab:true and call window.Feedback.open().
 *
 * Share  → iOS share sheet (text someone the URL) via navigator.share, else copy.
 * Feedback → type, or tap the 🎤 on the iOS keyboard to dictate. Save on the
 * device (works offline) and/or Send to the developer (prefilled email). */
(() => {
  "use strict";

  const cfg = Object.assign(
    { name: document.title || "this app", url: location.href, email: "info@conikersystems.com", noFab: false },
    window.APP_INFO || {}
  );
  const KEY = "appfeedback.notes";
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch { return []; } };
  const store = (a) => localStorage.setItem(KEY, JSON.stringify(a));

  const css = `
  .fb-fab{position:fixed;right:14px;bottom:calc(env(safe-area-inset-bottom) + 14px);z-index:9999;
    width:52px;height:52px;border-radius:50%;border:none;cursor:pointer;font-size:22px;
    background:#6ea8fe;color:#06101f;box-shadow:0 6px 18px rgba(0,0,0,.45);}
  .fb-overlay{position:fixed;inset:0;z-index:10000;background:rgba(6,9,15,.72);display:grid;place-items:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px;}
  .fb-card{background:#171a23;color:#e8eaf0;border:1px solid #2a2f3e;border-radius:16px;
    width:min(94vw,420px);max-height:88vh;overflow:auto;padding:18px 20px;}
  .fb-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;}
  .fb-head h2{font-size:18px;margin:0;}
  .fb-x{background:none;border:none;color:#8b90a0;font-size:26px;line-height:1;cursor:pointer;}
  .fb-btn{display:block;width:100%;border:1px solid #2a2f3e;background:#1d2130;color:#e8eaf0;
    border-radius:10px;padding:12px 14px;font-size:15px;cursor:pointer;margin:8px 0;}
  .fb-btn.primary{background:#6ea8fe;color:#06101f;border-color:#6ea8fe;font-weight:650;}
  .fb-row{display:flex;gap:10px;}
  .fb-row .fb-btn{margin:8px 0 0;}
  .fb-label{font-size:12px;color:#8b90a0;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.04em;}
  .fb-area{width:100%;min-height:96px;background:#0f1117;color:#e8eaf0;border:1px solid #2a2f3e;
    border-radius:10px;padding:12px;font-size:15px;line-height:1.5;resize:vertical;font-family:inherit;}
  .fb-note{font-size:12.5px;color:#9aa0b0;margin-top:14px;line-height:1.5;}
  .fb-meta{font-size:12px;color:#8b90a0;margin-top:8px;}
  .fb-link{background:none;border:none;color:#6ea8fe;cursor:pointer;font-size:12px;padding:0;}
  .fb-toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:10001;
    background:#1d2130;color:#e8eaf0;border:1px solid #2a2f3e;border-radius:999px;padding:8px 16px;font-size:13px;}
  `;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  function toast(msg) {
    const t = document.createElement("div");
    t.className = "fb-toast"; t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 1800);
  }

  async function shareApp() {
    const data = { title: cfg.name, text: cfg.name + " — try it:", url: cfg.url };
    if (navigator.share) { try { await navigator.share(data); return; } catch { return; } }
    try { await navigator.clipboard.writeText(cfg.url); toast("Link copied"); }
    catch { window.prompt("Copy this link:", cfg.url); }
  }

  function mailtoFor(text) {
    const notes = load();
    const lines = notes.map((n) => "• " + n.text);
    if (text && text.trim()) lines.push("• " + text.trim());
    const body = "Feedback for " + cfg.name + "\nURL: " + cfg.url + "\n\n" + (lines.join("\n") || "(no notes)") + "\n\n— sent from my device";
    return "mailto:" + cfg.email + "?subject=" + encodeURIComponent(cfg.name + " feedback") + "&body=" + encodeURIComponent(body);
  }

  function open() {
    const overlay = document.createElement("div");
    overlay.className = "fb-overlay";
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

    const card = document.createElement("div");
    card.className = "fb-card";
    card.innerHTML =
      '<div class="fb-head"><h2>' + cfg.name + '</h2><button class="fb-x" aria-label="Close">×</button></div>'
      + '<button class="fb-btn" id="fb-share">📤  Share / text this app</button>'
      + '<div class="fb-label">Send feedback</div>'
      + '<textarea class="fb-area" id="fb-text" placeholder="What works, what doesn’t, what you want next? Type here — or tap the 🎤 on your keyboard to talk."></textarea>'
      + '<div class="fb-row"><button class="fb-btn" id="fb-save">Save</button>'
      + '<button class="fb-btn primary" id="fb-send">Send to developer</button></div>'
      + '<div class="fb-meta"><span id="fb-count"></span> <button class="fb-link" id="fb-clear">clear saved</button></div>'
      + '<div class="fb-note">When you send feedback, we use it to improve ' + cfg.name
      + '. Because this is an installable web app, fixes are pushed automatically — just reopen the app to get the update.</div>';

    overlay.appendChild(card);
    document.body.appendChild(overlay);

    const text = card.querySelector("#fb-text");
    const countEl = card.querySelector("#fb-count");
    const refresh = () => { const n = load().length; countEl.textContent = n ? n + " saved on this device" : ""; };
    refresh();

    card.querySelector(".fb-x").onclick = () => overlay.remove();
    card.querySelector("#fb-share").onclick = shareApp;
    card.querySelector("#fb-save").onclick = () => {
      const v = text.value.trim();
      if (!v) { toast("Type something first"); return; }
      const notes = load(); notes.push({ t: new Date().toISOString(), text: v }); store(notes);
      text.value = ""; refresh(); toast("Saved on this device");
    };
    card.querySelector("#fb-send").onclick = () => { window.location.href = mailtoFor(text.value); };
    card.querySelector("#fb-clear").onclick = () => { store([]); refresh(); toast("Cleared"); };
  }

  if (!cfg.noFab) {
    const fab = document.createElement("button");
    fab.className = "fb-fab"; fab.title = "Share / Feedback"; fab.textContent = "💬";
    fab.onclick = open;
    const add = () => document.body.appendChild(fab);
    if (document.body) add(); else window.addEventListener("DOMContentLoaded", add);
  }

  window.Feedback = { open, shareApp };
})();
