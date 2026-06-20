// clip.cool remix editor (CSP-clean: external same-origin script, no inline JS).
// Load a template clip, re-crop + re-trim it with the shared ClipEdit widget (clipedit.js), then
// POST to the remix endpoint, which clones it into a NEW clip the user owns and re-transcodes with
// their crop/trim. We only DRAW an overlay box over the <video> (never read its pixels), so a
// cross-origin R2 source needs no CORS dance.
(function () {
  "use strict";

  const root = document.getElementById("clip-remix");
  if (!root) return;

  const els = {
    playback: document.getElementById("remix-playback"),
    cropCanvas: document.getElementById("remix-crop"),
    cropReset: document.getElementById("remix-crop-reset"),
    trim: document.getElementById("remix-trim"),
    trimBar: document.getElementById("trim-bar"),
    trimSel: document.getElementById("trim-sel"),
    trimPlayhead: document.getElementById("trim-playhead"),
    trimIn: document.getElementById("trim-in"),
    trimOut: document.getElementById("trim-out"),
    trimLabel: document.getElementById("trim-label"),
    trimReset: document.getElementById("trim-reset"),
    title: document.getElementById("remix-title"),
    tags: document.getElementById("remix-tags"),
    submit: document.getElementById("remix-submit"),
    status: document.getElementById("remix-status"),
  };
  const remixURL = root.dataset.remixUrl;
  const srcURL = root.dataset.srcUrl;

  const edit = ClipEdit.init({
    video: els.playback, cropCanvas: els.cropCanvas, cropReset: els.cropReset,
    trim: els.trim, trimBar: els.trimBar, trimSel: els.trimSel, trimPlayhead: els.trimPlayhead,
    trimIn: els.trimIn, trimOut: els.trimOut, trimLabel: els.trimLabel, trimReset: els.trimReset,
  });

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken") },
      body: JSON.stringify(body),
    });
  }

  function setStatus(msg, kind) {
    els.status.textContent = msg || "";
    els.status.className = "kg-status" + (kind ? " is-" + kind : "");
  }

  // Arm crop + trim once the source duration is known, then loop it muted as a backdrop for the box.
  els.playback.addEventListener("loadedmetadata", function () {
    edit.arm(els.playback.duration);
    els.playback.play().catch(function () {});
  });
  els.playback.src = srcURL;

  function failed(msg) {
    setStatus(msg, "error");
    els.submit.disabled = false;
    els.submit.textContent = "Create my GIF";
  }

  async function submit() {
    els.submit.disabled = true;
    els.submit.textContent = "Creating…";
    setStatus("Creating your GIF…");
    try {
      const tags = (els.tags.value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
      const body = { title: els.title.value || "", tags: tags };
      const cf = edit.cropFractions();
      if (cf) body.crop = cf;
      Object.assign(body, edit.trimPayload());   // trim_start / trim_end (seconds), omitted if whole clip
      const res = await postJSON(remixURL, body);
      if (!res.ok) { failed("Couldn't create the clip (" + res.status + "): " + (await res.text())); return; }
      const asset = await res.json();
      setStatus("Created — opening your clip…", "ok");
      window.location.href = "/clips/asset/" + encodeURIComponent(asset.id) + "/";
    } catch (err) {
      failed("Error: " + err);
    }
  }

  els.submit.addEventListener("click", submit);
})();
