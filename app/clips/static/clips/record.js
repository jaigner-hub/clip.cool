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
    hint: document.getElementById("record-hint"),
    stage: document.getElementById("record-stage"),
    preview: document.getElementById("record-preview"),
    cropCanvas: document.getElementById("record-crop"),
    playback: document.getElementById("record-playback"),
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
  let rafId = 0;            // crop draw loop

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
    show(els.playback, false);
    show(els.meta, false);
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
    // 1 overlay px == 1 displayed CSS px, so pointer offsets map straight onto the canvas.
    const w = els.preview.clientWidth, h = els.preview.clientHeight;
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
      const label = "✂ Drag across the video to crop — or just press Record for the whole tab";
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
    els.hint.textContent = "Recording the whole tab. Drag a box over the area you want to crop it.";
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
    els.hint.textContent = "Cropping to your selection. Drag again to redo, or Clear crop for the whole tab.";
  }

  // Map the display-px selection to source pixels and start a canvas that draws only that rect.
  // Returns a MediaStream to record (canvas video + the display stream's audio).
  function buildCropStream() {
    const vw = els.preview.videoWidth, vh = els.preview.videoHeight;
    const sx = vw / els.cropCanvas.width, sy = vh / els.cropCanvas.height;
    let cx = Math.round(cropDisp.x * sx);
    let cy = Math.round(cropDisp.y * sy);
    let cw = Math.round(cropDisp.w * sx);
    let ch = Math.round(cropDisp.h * sy);
    cx = clamp(cx, 0, vw - 2); cy = clamp(cy, 0, vh - 2);
    cw = clamp(cw, 2, vw - cx); ch = clamp(ch, 2, vh - cy);
    cw -= cw % 2; ch -= ch % 2;   // even dims keep the encoder (yuv420p) happy

    const canvas = document.createElement("canvas");
    canvas.width = cw; canvas.height = ch;
    const ctx = canvas.getContext("2d");
    (function draw() {
      ctx.drawImage(els.preview, cx, cy, cw, ch, 0, 0, cw, ch);
      rafId = requestAnimationFrame(draw);
    })();

    const out = canvas.captureStream(30);
    stream.getAudioTracks().forEach(function (t) { out.addTrack(t); });
    return out;
  }

  async function share() {
    resetClip();
    setStatus("");
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 30 },
        audio: true,   // tab audio if the user opts in; ignored otherwise
      });
    } catch (err) {
      setStatus(err && err.name === "NotAllowedError" ? "Sharing cancelled." : "Couldn't start sharing: " + err, "error");
      return;
    }
    // If the user clicks the browser's native "Stop sharing", tear down gracefully.
    stream.getVideoTracks()[0].addEventListener("ended", function () {
      if (recorder && recorder.state !== "inactive") stopRecording();
      teardownPreview();
    });
    els.preview.srcObject = stream;
    clearCrop();
    show(els.stage, true);
    show(els.controls, true);
    show(els.start, true);
    show(els.stop, false);
    show(els.reset, false);
    els.preview.addEventListener("loadedmetadata", drawBand, { once: true });
    requestAnimationFrame(drawBand);   // paint the idle hint once the stage has laid out
    els.share.textContent = "Share a different tab";
  }

  function teardownPreview() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    stopTracks();
    els.preview.srcObject = null;
    show(els.stage, false);
    show(els.controls, false);
    els.timer.textContent = "";
    els.share.textContent = "Share a browser tab";
  }

  function startRecording() {
    if (!stream) return;
    resetClip();
    const recStream = cropDisp ? buildCropStream() : stream;
    const mimeType = pickMimeType();
    try {
      recorder = mimeType ? new MediaRecorder(recStream, { mimeType: mimeType }) : new MediaRecorder(recStream);
    } catch (err) {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
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
    show(els.playback, false);
    els.cropCanvas.style.cursor = "default";
    timerId = setInterval(tick, 250);
    tick();
  }

  function stopRecording() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function onRecordingStopped() {
    clip = new Blob(chunks, { type: UPLOAD_TYPE });
    chunks = [];
    if (!clip.size) { setStatus("Nothing was recorded — try again.", "error"); return; }
    clipURL = URL.createObjectURL(clip);
    els.playback.src = clipURL;
    show(els.stage, false);
    show(els.playback, true);
    show(els.stop, false);
    show(els.start, true);
    els.start.textContent = "● Record again";
    show(els.reset, false);
    show(els.meta, true);
    els.timer.textContent = "Captured " + Math.round(clip.size / 1024) + " KB. Review it, then upload.";
  }

  async function upload() {
    if (!clip) { setStatus("Record a clip first.", "error"); return; }
    els.upload.disabled = true;
    try {
      setStatus("Requesting upload URL…");
      const filename = "tab-recording-" + clip.size + ".webm";
      let res = await postJSON(presignURL, { filename: filename, content_type: UPLOAD_TYPE });
      if (!res.ok) { setStatus("Presign failed: " + (await res.text()), "error"); els.upload.disabled = false; return; }
      const { key, url } = await res.json();

      setStatus("Uploading to storage…");
      res = await fetch(url, { method: "PUT", headers: { "Content-Type": UPLOAD_TYPE }, body: clip });
      if (!res.ok) { setStatus("Upload to R2 failed (" + res.status + "). Check bucket CORS.", "error"); els.upload.disabled = false; return; }

      setStatus("Finalizing…");
      const tags = (els.tags.value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
      res = await postJSON(finalizeURL, { key: key, title: els.title.value || "", content_type: UPLOAD_TYPE, tags: tags });
      if (!res.ok) { setStatus("Finalize failed: " + (await res.text()), "error"); els.upload.disabled = false; return; }
      const asset = await res.json();

      setStatus("Uploaded — opening your clip…", "ok");
      stopTracks();
      window.location.href = "/clips/asset/" + encodeURIComponent(asset.id) + "/";
    } catch (err) {
      setStatus("Error: " + err, "error");
      els.upload.disabled = false;
    }
  }

  els.share.addEventListener("click", share);
  els.start.addEventListener("click", startRecording);
  els.stop.addEventListener("click", stopRecording);
  els.cropReset.addEventListener("click", clearCrop);
  els.reset.addEventListener("click", function () { resetClip(); show(els.stage, !!stream); });
  els.cropCanvas.addEventListener("pointerdown", onPointerDown);
  els.cropCanvas.addEventListener("pointermove", onPointerMove);
  els.cropCanvas.addEventListener("pointerup", onPointerUp);
  window.addEventListener("resize", function () { if (stream) clearCrop(); });
  window.addEventListener("beforeunload", stopTracks);
})();
