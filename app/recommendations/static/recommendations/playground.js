// Recommendations playground (ADR 0013). Strict-CSP safe: external file under script-src 'self',
// no inline handlers — everything wired via addEventListener, all config read from #rec-app data-*.
// Model output is inserted with textContent only (never innerHTML), so a recommendation can't
// inject markup.
(function () {
  "use strict";
  const app = document.getElementById("rec-app");
  if (!app) return;

  const streamUrl = app.dataset.streamUrl;
  const promptUrl = app.dataset.promptUrl;
  const interactionBase = app.dataset.interactionBase;
  const csrf = (app.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";

  // The prompt version used for the next run: starts at the champion, updated when you save one.
  let promptVersionId = app.dataset.championVersion || "";

  const urlInput = document.getElementById("rec-url");
  const analyzeBtn = document.getElementById("rec-analyze");
  const statusEl = document.getElementById("rec-status");
  const resultsEl = document.getElementById("rec-results");
  let source = null;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "kg-out " + (kind === "error" ? "kg-status-error" : "kg-muted");
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null && text !== "") node.textContent = text;
    return node;
  }

  function renderCard(rec) {
    const card = el("article", "kg-rec");
    const head = el("div", "kg-rec-head");
    head.appendChild(el("span", "kg-pill kg-rec-cat", String(rec.category || "").replace(/_/g, " ")));
    head.appendChild(el("span", "kg-pill kg-rec-effort", String(rec.effort || "").replace(/_/g, " ")));
    const score = Math.round(Number(rec.priority_score) || 0);
    head.appendChild(el("span", "kg-rec-score", "priority " + score));
    card.appendChild(head);

    card.appendChild(el("h3", "kg-rec-title", rec.title));
    if (rec.why) {
      const why = el("p", "kg-rec-why");
      why.appendChild(el("strong", null, "Why: "));
      why.appendChild(document.createTextNode(rec.why));
      card.appendChild(why);
    }
    if (rec.description) card.appendChild(el("p", "kg-rec-desc", rec.description));
    if (rec.action_type) {
      card.appendChild(el("p", "kg-rec-action kg-muted", String(rec.action_type).replace(/_/g, " ")));
    }

    const actions = el("div", "kg-row kg-rec-actions");
    actions.appendChild(makeInteraction(rec.id, "accepted", "Accept", "kg-btn kg-btn-sm"));
    actions.appendChild(makeInteraction(rec.id, "dismissed", "Dismiss", "kg-btn kg-btn-sm kg-btn-ghost"));
    card.appendChild(actions);
    return card;
  }

  function makeInteraction(recId, kind, label, className) {
    const btn = el("button", className, label);
    btn.type = "button";
    btn.addEventListener("click", function () {
      const body = new URLSearchParams({ kind: kind });
      fetch(interactionBase + recId, {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      }).then(function (r) {
        if (r.ok) {
          btn.parentNode.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
          btn.textContent = label + " ✓";
        }
      });
    });
    return btn;
  }

  function analyze() {
    const url = (urlInput.value || "").trim();
    if (!url) { setStatus("Enter a URL first.", "error"); return; }
    if (source) source.close();
    resultsEl.textContent = "";
    analyzeBtn.disabled = true;
    setStatus("Connecting…");

    const qs = new URLSearchParams({ url: url });
    if (promptVersionId) qs.set("prompt_version_id", promptVersionId);
    source = new EventSource(streamUrl + "?" + qs.toString());

    let chunks = 0;
    source.addEventListener("message", function (e) {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      if (ev.type === "page") {
        setStatus("Analyzing: " + (ev.title || ev.url));
      } else if (ev.type === "chunk") {
        chunks += 1;
        setStatus("Generating recommendations… (" + chunks + " chunks)");
      } else if (ev.type === "done") {
        finish(ev);
      } else if (ev.type === "error") {
        setStatus("Error: " + ev.error, "error");
        cleanup();
      }
    });
    source.addEventListener("error", function () {
      // Fires on normal close too; only surface if we never finished.
      if (source && source.readyState === EventSource.CLOSED) return;
      setStatus("Connection lost.", "error");
      cleanup();
    });
  }

  function finish(ev) {
    const recs = ev.recommendations || [];
    recs.forEach(function (rec) { resultsEl.appendChild(renderCard(rec)); });
    const u = ev.usage || {};
    const tokens = (u.input_tokens || 0) + (u.output_tokens || 0);
    const secs = ((ev.duration_ms || 0) / 1000).toFixed(1);
    setStatus(recs.length + " recommendations · " + tokens + " tokens · " + secs + "s");
    cleanup();
  }

  function cleanup() {
    if (source) { source.close(); source = null; }
    analyzeBtn.disabled = false;
  }

  analyzeBtn.addEventListener("click", analyze);
  urlInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") analyze();
  });

  // --- prompt editor (superuser only; controls are absent otherwise) ---
  const saveBtn = document.getElementById("rec-save-prompt");
  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      const promptStatus = document.getElementById("rec-prompt-status");
      const body = new URLSearchParams({
        system_prompt: document.getElementById("rec-prompt").value,
        model: document.getElementById("rec-model").value,
        temperature: document.getElementById("rec-temp").value,
        make_champion: document.getElementById("rec-make-champion").checked ? "1" : "0",
      });
      promptStatus.textContent = "saving…";
      fetch(promptUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      }).then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
        .then(function (data) {
          promptVersionId = String(data.id);
          promptStatus.textContent = "saved v" + data.version + (data.is_champion ? " (champion)" : "") + " — used for next run";
        })
        .catch(function () { promptStatus.textContent = "save failed"; });
    });
  }
})();
