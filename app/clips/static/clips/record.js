// clip.cool in-browser tab recorder (CSP-clean: external same-origin script, no inline JS).
//   1) getDisplayMedia  -> user shares a browser tab; we show a live preview
//   2) optional crop    -> drag a box over the preview; we record only that region
//   3) MediaRecorder    -> Record/Stop bounds the clip (the recording window IS the trim)
//   4) presign -> PUT to R2 -> finalize  (same path as a file upload; emits video/webm)
//
// Cropping: a tab capture is the WHOLE rendered tab. The "crop a captured tab to one element" API
// (Region Capture / cropTo) is self-capture only, so it can't target another tab's video. Instead,
// when a crop is set we draw the selected source rect onto a canvas each frame and record
// canvas.captureStream() (audio re-attached from the display stream). No crop = record the raw
// stream (best quality, the original path).
(function () {
  "use strict";

  const root = document.getElementById("clip-record");
  if (!root) return;

  const els = {
    share: document.getElementById("record-share"),
    pip: document.getElementById("record-pip"),
    hint: document.getElementById("record-hint"),
    stage: document.getElementById("record-stage"),
    preview: document.getElementById("record-preview"),
    cropCanvas: document.getElementById("record-crop"),
    editStage: document.getElementById("record-edit-stage"),
    playback: document.getElementById("record-playback"),
    trim: document.getElementById("record-trim"),
    trimBar: document.getElementById("trim-bar"),
    trimSel: document.getElementById("trim-sel"),
    trimPlayhead: document.getElementById("trim-playhead"),
    trimIn: document.getElementById("trim-in"),
    trimOut: document.getElementById("trim-out"),
    trimLabel: document.getElementById("trim-label"),
    trimReset: document.getElementById("trim-reset"),
    controls: document.getElementById("record-controls"),
    start: document.getElementById("record-start"),
    stop: document.getElementById("record-stop"),
    cropReset: document.getElementById("record-crop-reset"),
    reset: document.getElementById("record-reset"),
    timer: document.getElementById("record-timer"),
    meta: document.getElementById("record-meta"),
    title: document.getElementById("record-title"),
    tags: document.getElementById("record-tags"),
    upload: document.getElementById("record-upload"),
    status: document.getElementById("record-status"),
  };
  const presignURL = root.dataset.presignUrl;
  const finalizeURL = root.dataset.finalizeUrl;
  const maxSeconds = parseInt(root.dataset.maxSeconds, 10) || 60;

  // R2 was signed for a bare "video/webm" Content-Type, so the PUT + finalize must use exactly
  // that (MediaRecorder's blob.type carries a ;codecs=… suffix the presign didn't sign for).
  const UPLOAD_TYPE = "video/webm";
  const MIN_CROP_PX = 16;   // a drag smaller than this (in display px) is treated as a stray click

  let stream = null;        // the shared-tab MediaStream
  let recorder = null;      // MediaRecorder
  let chunks = [];          // recorded data
  let clip = null;          // final Blob
  let clipURL = null;       // object URL for playback (revoked on reset)
  let timerId = null;
  let startedAt = 0;
  let cropDisp = null;      // selection in display (overlay) px: {x,y,w,h}; null = whole tab
  let dragStart = null;     // pointer-down point while dragging
  let clipDuration = 0;     // recorded length (s) — source of truth for the trim bar
  let trimInS = 0;          // kept-range start (s)
  let trimOutS = 0;         // kept-range end (s)
  let trimDrag = null;      // "in" | "out" while dragging a handle
  let pipWindow = null;     // Document Picture-in-Picture window (floating controls)
  let pipMoved = [];        // [{node, parent, next}] — where moved nodes return to on close

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

  function show(el, on) { el.hidden = !on; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // Feature gate: getDisplayMedia is Chromium/Firefox desktop; absent on iOS Safari.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia || typeof MediaRecorder === "undefined") {
    els.share.disabled = true;
    setStatus("Screen recording isn't supported in this browser. Try desktop Chrome, Edge, or Firefox — or use Upload.", "error");
    return;
  }

  function pickMimeType() {
    const prefs = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"];
    for (const t of prefs) {
      if (MediaRecorder.isTypeSupported(t)) return t;
    }
    return "";  // let the browser choose; still a webm container in practice
  }

  function stopTracks() {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
  }

  function resetClip() {
    if (clipURL) { URL.revokeObjectURL(clipURL); clipURL = null; }
    clip = null;
    chunks = [];
    cropDisp = null;
    show(els.cropReset, false);
    show(els.editStage, false);
    show(els.trim, false);
    show(els.meta, false);
  }

  // --- trim scrubber (drag in/out over the recorded clip; applied at transcode) ---

  function layoutTrim() {
    if (clipDuration <= 0) return;
    const inPct = (trimInS / clipDuration) * 100;
    const outPct = (trimOutS / clipDuration) * 100;
    els.trimSel.style.left = inPct + "%";
    els.trimSel.style.width = Math.max(0, outPct - inPct) + "%";
    els.trimIn.style.left = inPct + "%";
    els.trimOut.style.left = outPct + "%";
    els.trimLabel.textContent =
      "in " + trimInS.toFixed(1) + "s · out " + trimOutS.toFixed(1) + "s · "
      + (trimOutS - trimInS).toFixed(1) + "s clip";
  }

  function setPlayhead(t) {
    if (clipDuration > 0) els.trimPlayhead.style.left = ((t / clipDuration) * 100) + "%";
  }

  function initTrim() {
    trimInS = 0;
    trimOutS = clipDuration;
    layoutTrim();
    setPlayhead(0);
    show(els.trim, true);
  }

  function barSeconds(clientX) {
    const r = els.trimBar.getBoundingClientRect();
    if (r.width <= 0) return 0;
    return clamp((clientX - r.left) / r.width, 0, 1) * clipDuration;
  }

  function onTrimDown(which) {
    return function (ev) {
      ev.preventDefault();
      trimDrag = which;
      if (els.trimBar.setPointerCapture && ev.pointerId != null) {
        try { els.trimBar.setPointerCapture(ev.pointerId); } catch (e) {}
      }
      onTrimMove(ev);
    };
  }

  function onTrimMove(ev) {
    if (!trimDrag) return;
    const t = barSeconds(ev.clientX);
    if (trimDrag === "in") trimInS = clamp(t, 0, trimOutS - 0.1);
    else trimOutS = clamp(t, trimInS + 0.1, clipDuration);
    const edge = trimDrag === "in" ? trimInS : trimOutS;
    try { els.playback.currentTime = edge; } catch (e) {}
    setPlayhead(edge);
    layoutTrim();
  }

  function endTrimDrag() { trimDrag = null; }

  function resetTrim() { trimInS = 0; trimOutS = clipDuration; layoutTrim(); setPlayhead(0); }

  // Fraction (0..1) of the recorded range to send to the server; null when the whole clip is kept.
  function trimPayload() {
    const body = {};
    if (clipDuration > 0) {
      if (trimInS > 0.05) body.trim_start = trimInS;
      if (trimOutS < clipDuration - 0.05) body.trim_end = trimOutS;
    }
    return body;
  }

  function fmt(s) {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }

  function tick() {
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    els.timer.textContent = "Recording… " + fmt(elapsed) + " / " + fmt(maxSeconds);
    if (elapsed >= maxSeconds) stopRecording();
  }

  // --- crop selection (overlay drawn in display px; converted to source px at record time) ---

  function syncOverlaySize() {
    // 1 overlay px == 1 displayed CSS px, so pointer offsets map straight onto the canvas. The crop
    // overlay sits over the RECORDED clip (playback) now, not the live preview — you crop after the
    // fact, right before upload.
    const w = els.playback.clientWidth, h = els.playback.clientHeight;
    if (w && h && (els.cropCanvas.width !== w || els.cropCanvas.height !== h)) {
      els.cropCanvas.width = w;
      els.cropCanvas.height = h;
    }
  }

  function drawBand() {
    syncOverlaySize();
    const c = els.cropCanvas, ctx = c.getContext("2d");
    if (!c.width || !c.height) return;
    ctx.clearRect(0, 0, c.width, c.height);
    if (!cropDisp) {
      // Idle affordance: a dashed frame + label so the crop tool is actually discoverable.
      ctx.setLineDash([8, 6]);
      ctx.strokeStyle = "rgba(97,217,239,0.9)";     // --kg-cyan
      ctx.lineWidth = 2;
      ctx.strokeRect(5, 5, c.width - 10, c.height - 10);
      ctx.setLineDash([]);
      const label = "✂ Drag across the clip to crop — or upload the whole tab";
      ctx.font = "600 13px system-ui, -apple-system, sans-serif";
      ctx.textBaseline = "top";
      const tw = Math.min(ctx.measureText(label).width, c.width - 20);
      const bx = (c.width - tw) / 2 - 10;
      ctx.fillStyle = "rgba(11,18,32,0.7)";
      ctx.fillRect(bx, 10, tw + 20, 26);
      ctx.fillStyle = "#fff";
      ctx.fillText(label, (c.width - tw) / 2, 16, c.width - 20);
      return;
    }
    ctx.fillStyle = "rgba(11,18,32,0.55)";          // dim everything…
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.clearRect(cropDisp.x, cropDisp.y, cropDisp.w, cropDisp.h);  // …except the selection
    ctx.strokeStyle = "#61D9EF";                    // --kg-cyan
    ctx.lineWidth = 2;
    ctx.strokeRect(cropDisp.x, cropDisp.y, cropDisp.w, cropDisp.h);
  }

  function clearCrop() {
    cropDisp = null;
    drawBand();
    show(els.cropReset, false);
  }

  function onPointerDown(e) {
    if (recorder && recorder.state !== "inactive") return;  // no re-cropping mid-record
    syncOverlaySize();
    els.cropCanvas.setPointerCapture(e.pointerId);
    const r = els.cropCanvas.getBoundingClientRect();
    dragStart = { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  function onPointerMove(e) {
    if (!dragStart) return;
    const r = els.cropCanvas.getBoundingClientRect();
    const x = clamp(e.clientX - r.left, 0, els.cropCanvas.width);
    const y = clamp(e.clientY - r.top, 0, els.cropCanvas.height);
    cropDisp = {
      x: Math.min(dragStart.x, x),
      y: Math.min(dragStart.y, y),
      w: Math.abs(x - dragStart.x),
      h: Math.abs(y - dragStart.y),
    };
    drawBand();
  }

  function onPointerUp() {
    if (!dragStart) return;
    dragStart = null;
    if (!cropDisp || cropDisp.w < MIN_CROP_PX || cropDisp.h < MIN_CROP_PX) {
      clearCrop();  // treat a tiny drag as "no crop"
      return;
    }
    drawBand();
    show(els.cropReset, true);
  }

  // The selection as fractions (0..1) of the source frame — resolution-independent, so the server
  // can map it onto the recorded full-frame webm whatever its dimensions. null = no crop.
  function cropFractions() {
    const c = els.cropCanvas;
    if (!cropDisp || !c.width || !c.height) return null;
    return { x: cropDisp.x / c.width, y: cropDisp.y / c.height, w: cropDisp.w / c.width, h: cropDisp.h / c.height };
  }

  // --- floating controls via Document Picture-in-Picture -------------------------------------
  // The pain we're solving: to press play on the captured tab you must switch to it, which buries
  // clip.cool's Record button. There's no way to inject a click into a captured tab (browser
  // security), so instead we PUSH our controls out: Document PiP opens an always-on-top window we
  // fill with the live preview + Record/Stop. The user stays on YouTube, presses play natively, and
  // hits Record in the floating window — no tab dance. Chromium 116+; everyone else just doesn't see
  // the button and uses the page as before.
  function pipSupported() { return "documentPictureInPicture" in window; }

  // PiP windows start with no stylesheets; clone our same-origin <link>s so .kg-btn etc. survive
  // (CSP-clean — we copy existing external links, never inject inline CSS).
  function copyStyles(pipDoc) {
    document.querySelectorAll('link[rel="stylesheet"]').forEach(function (link) {
      pipDoc.head.appendChild(link.cloneNode(true));
    });
  }

  // Move the live preview + record controls into the floating window. Moving (not cloning) keeps
  // every existing listener attached — adopting a node across documents preserves its handlers — so
  // the same Record/Stop buttons just work from the PiP window.
  async function openPip(auto) {
    if (!pipSupported() || pipWindow || !stream) return;
    try {
      pipWindow = await documentPictureInPicture.requestWindow({ width: 420, height: 440 });
    } catch (err) {
      // Auto-open needs transient activation, which the share picker doesn't reliably carry; when it's
      // missing, fall back silently to the manual "Pop out controls" button rather than nagging.
      pipWindow = null;
      if (!auto) setStatus("Couldn't pop out the controls: " + err, "error");
      return;
    }
    copyStyles(pipWindow.document);
    const pdoc = pipWindow.document;
    pdoc.body.style.margin = "0";          // CSSOM .style isn't subject to CSP (unlike style="" attrs)
    pdoc.body.style.padding = "12px";
    pdoc.body.style.background = "#0b1220"; // --kg-bg, so it reads as part of clip.cool
    pdoc.body.style.display = "flex";       // stack vertically: video on top, controls below it
    pdoc.body.style.flexDirection = "column";
    pdoc.body.style.gap = "10px";
    [els.stage, els.controls].forEach(function (node) {
      pipMoved.push({ node: node, parent: node.parentNode, next: node.nextSibling });
      pdoc.body.appendChild(node);
    });
    // Repaint the crop overlay for the new (smaller) layout. Use the PiP window's rAF — the main
    // window's is throttled the moment its tab is backgrounded, which is exactly when this matters.
    pipWindow.requestAnimationFrame(drawBand);
    // Native close button (or close()) → put everything back on the page.
    pipWindow.addEventListener("pagehide", restoreFromPip, { once: true });
    els.pip.textContent = "Controls popped out ↗";
    els.pip.disabled = true;
  }

  function restoreFromPip() {
    pipMoved.forEach(function (m) {
      if (m.next && m.next.parentNode === m.parent) m.parent.insertBefore(m.node, m.next);
      else m.parent.appendChild(m.node);
    });
    pipMoved = [];
    pipWindow = null;
    requestAnimationFrame(drawBand);   // back on the page; re-fit the overlay
    els.pip.textContent = "⧉ Pop out controls";
    els.pip.disabled = !stream;
  }

  function closePip() {
    if (pipWindow) { try { pipWindow.close(); } catch (e) {} }  // fires pagehide → restoreFromPip
  }

  async function share() {
    closePip();
    resetClip();
    setStatus("");
    // Conditional Focus (Chromium): decide where focus lands when the picker closes (set below, right
    // after getDisplayMedia resolves). With the float carrying Record, we WANT to land on the shared
    // tab; without it, we keep focus here so the page controls stay reachable.
    let controller = null;
    try { if (typeof CaptureController !== "undefined") controller = new CaptureController(); } catch (e) { controller = null; }
    // Cap the captured surface to 1080p: a 2K/4K tab is pointless for a meme loop and just bloats the
    // upload + slows the server transcode. The browser downscales at the source; never breaks
    // background recording (a capture constraint, not a canvas pipeline). Pairs with the ≤1280
    // server-side rendition cap.
    const opts = {
      video: { frameRate: { ideal: 30, max: 30 }, width: { max: 1920 }, height: { max: 1080 } },
      audio: true,   // tab audio if the user opts in
    };
    if (controller) opts.controller = controller;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia(opts);
    } catch (err) {
      setStatus(err && err.name === "NotAllowedError" ? "Sharing cancelled." : "Couldn't start sharing: " + err, "error");
      return;
    }
    // Must be set right after the promise resolves (before yielding to the event loop), per spec — so
    // it's a synchronous pipSupported() check, not the (async) result of actually opening the float.
    // Float available → focus the captured tab so the user can press play immediately (the always-on-
    // top float carries Record). No float → keep focus on clip.cool so the page controls stay visible.
    if (controller && controller.setFocusBehavior) {
      try {
        controller.setFocusBehavior(pipSupported() ? "focus-captured-surface" : "no-focus-change");
      } catch (e) { /* unsupported / too late */ }
    }
    // If the user clicks the browser's native "Stop sharing", tear down gracefully.
    stream.getVideoTracks()[0].addEventListener("ended", function () {
      if (recorder && recorder.state !== "inactive") stopRecording();
      teardownPreview();
    });
    els.preview.srcObject = stream;
    show(els.stage, true);
    show(els.controls, true);
    show(els.start, true);
    show(els.stop, false);
    show(els.reset, false);
    els.share.textContent = "Share a different tab";
    if (pipSupported()) {
      show(els.pip, true);
      els.pip.disabled = false;
      openPip(true);   // try to float automatically; silently leaves the button if activation is gone
    }
  }

  function teardownPreview() {
    closePip();
    stopTracks();
    els.preview.srcObject = null;
    show(els.stage, false);
    show(els.controls, false);
    show(els.pip, false);
    els.timer.textContent = "";
    els.share.textContent = "Share a browser tab";
  }

  function startRecording() {
    if (!stream) return;
    resetClip();
    // Always record the RAW capture stream. getDisplayMedia keeps producing frames while the
    // clip.cool tab is in the background (so you can switch to YouTube and press play), whereas a
    // live canvas crop would freeze — requestAnimationFrame is throttled in hidden tabs. The crop
    // selection is sent to the server and baked in by ffmpeg at transcode instead.
    const mimeType = pickMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType: mimeType }) : new MediaRecorder(stream);
    } catch (err) {
      setStatus("Couldn't start recording: " + err, "error");
      return;
    }
    chunks = [];
    recorder.addEventListener("dataavailable", function (e) {
      if (e.data && e.data.size > 0) chunks.push(e.data);
    });
    recorder.addEventListener("stop", onRecordingStopped);
    recorder.start();
    startedAt = Date.now();
    setStatus("");
    show(els.start, false);
    show(els.stop, true);
    show(els.reset, false);
    show(els.cropReset, false);
    show(els.editStage, false);
    show(els.stage, true);   // ensure the live preview is up (matters on "Record again")
    timerId = setInterval(tick, 250);
    tick();
  }

  function stopRecording() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function onRecordingStopped() {
    closePip();   // trim + upload UI lives on the page; bring the controls back from the float
    clip = new Blob(chunks, { type: UPLOAD_TYPE });
    chunks = [];
    if (!clip.size) { setStatus("Nothing was recorded — try again.", "error"); return; }
    // The wall-clock record length is reliable; MediaRecorder webm duration metadata often isn't.
    const recordedSeconds = startedAt ? (Date.now() - startedAt) / 1000 : 0;
    clipURL = URL.createObjectURL(clip);
    els.playback.src = clipURL;
    fixDurationThen(function (d) {
      clipDuration = (isFinite(d) && d > 0) ? d : recordedSeconds;
      initTrim();
      // Crop happens HERE now — on the recorded clip, right before upload. Arm the overlay over the
      // (now laid-out, visible) playback and loop it muted so the box is framed against real footage.
      clearCrop();
      requestAnimationFrame(drawBand);
      els.playback.play().catch(function () {});
    });
    show(els.stage, false);
    show(els.editStage, true);
    show(els.stop, false);
    show(els.start, true);
    els.start.textContent = "● Record again";
    show(els.reset, false);
    show(els.meta, true);
    els.timer.textContent = "Captured " + Math.round(clip.size / 1024)
      + " KB — drag on the clip to crop, trim below, then upload.";
  }

  // MediaRecorder webm blobs often report duration=Infinity until you seek to the end. Force the
  // browser to compute it, then call back with the (now finite) duration.
  function fixDurationThen(cb) {
    const v = els.playback;
    function ready() {
      v.removeEventListener("loadedmetadata", ready);
      if (isFinite(v.duration) && v.duration > 0) { cb(v.duration); return; }
      const onSeek = function () {
        v.removeEventListener("timeupdate", onSeek);
        v.currentTime = 0;
        cb(v.duration);
      };
      v.addEventListener("timeupdate", onSeek);
      try { v.currentTime = 1e101; } catch (e) { cb(v.duration); }
    }
    v.addEventListener("loadedmetadata", ready);
  }

  function uploadFailed(msg) {
    setStatus(msg, "error");
    els.upload.disabled = false;
    els.upload.textContent = "Upload clip";
  }

  async function upload() {
    if (!clip) { setStatus("Record a clip first.", "error"); return; }
    // Immediate, visible feedback so a click is never silent (and never double-fires).
    els.upload.disabled = true;
    els.upload.textContent = "Uploading…";
    setStatus("Requesting upload URL…");
    try {
      const filename = "tab-recording-" + clip.size + ".webm";
      let res = await postJSON(presignURL, { filename: filename, content_type: UPLOAD_TYPE });
      if (!res.ok) { uploadFailed("Presign failed (" + res.status + "): " + (await res.text())); return; }
      const { key, url } = await res.json();

      setStatus("Uploading to storage…");
      res = await fetch(url, { method: "PUT", headers: { "Content-Type": UPLOAD_TYPE }, body: clip });
      if (!res.ok) { uploadFailed("Upload to R2 failed (" + res.status + "). Check bucket CORS."); return; }

      setStatus("Finalizing…");
      const tags = (els.tags.value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
      const body = { key: key, title: els.title.value || "", content_type: UPLOAD_TYPE, tags: tags };
      const cf = cropFractions();
      if (cf) body.crop = cf;
      Object.assign(body, trimPayload());   // trim_start / trim_end (seconds), omitted if whole clip
      res = await postJSON(finalizeURL, body);
      if (!res.ok) { uploadFailed("Finalize failed (" + res.status + "): " + (await res.text())); return; }
      const asset = await res.json();

      setStatus("Uploaded — opening your clip…", "ok");
      stopTracks();
      window.location.href = "/clips/asset/" + encodeURIComponent(asset.id) + "/";
    } catch (err) {
      uploadFailed("Error: " + err);
    }
  }

  els.share.addEventListener("click", share);
  els.pip.addEventListener("click", openPip);
  els.start.addEventListener("click", startRecording);
  els.stop.addEventListener("click", stopRecording);
  els.cropReset.addEventListener("click", clearCrop);
  els.reset.addEventListener("click", function () { resetClip(); show(els.stage, !!stream); });
  els.cropCanvas.addEventListener("pointerdown", onPointerDown);
  els.cropCanvas.addEventListener("pointermove", onPointerMove);
  els.cropCanvas.addEventListener("pointerup", onPointerUp);
  els.upload.addEventListener("click", upload);

  // Trim: drag the handles; click the bar to seek; loop playback within the kept range.
  els.trimIn.addEventListener("pointerdown", onTrimDown("in"));
  els.trimOut.addEventListener("pointerdown", onTrimDown("out"));
  els.trimBar.addEventListener("pointerdown", function (ev) {
    if (ev.target === els.trimIn || ev.target === els.trimOut) return;  // a handle grab
    const t = barSeconds(ev.clientX);
    try { els.playback.currentTime = t; } catch (e) {}
    setPlayhead(t);
  });
  els.trimBar.addEventListener("pointermove", onTrimMove);
  els.trimBar.addEventListener("pointerup", endTrimDrag);
  els.trimBar.addEventListener("pointercancel", endTrimDrag);
  window.addEventListener("pointerup", endTrimDrag);
  els.trimReset.addEventListener("click", resetTrim);
  els.playback.addEventListener("timeupdate", function () {
    const t = els.playback.currentTime;
    if (clipDuration > 0 && (t >= trimOutS || t < trimInS - 0.05)) {
      try { els.playback.currentTime = trimInS; } catch (e) {}
    }
    setPlayhead(els.playback.currentTime);
  });

  window.addEventListener("resize", function () { if (stream) clearCrop(); layoutTrim(); });
  window.addEventListener("beforeunload", function () { closePip(); stopTracks(); });
})();
