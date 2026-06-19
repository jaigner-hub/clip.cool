// clip.cool in-browser tab recorder (CSP-clean: external same-origin script, no inline JS).
//   1) getDisplayMedia  -> user shares a browser tab; we show a live preview
//   2) MediaRecorder    -> Record/Stop bounds the clip (the recording window IS the trim)
//   3) presign -> PUT to R2 -> finalize  (same path as a file upload; emits video/webm)
// The captured blob is video/webm, which finalize routes straight to the transcode queue.
(function () {
  "use strict";

  const root = document.getElementById("clip-record");
  if (!root) return;

  const els = {
    share: document.getElementById("record-share"),
    hint: document.getElementById("record-hint"),
    preview: document.getElementById("record-preview"),
    playback: document.getElementById("record-playback"),
    controls: document.getElementById("record-controls"),
    start: document.getElementById("record-start"),
    stop: document.getElementById("record-stop"),
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

  let stream = null;        // the shared-tab MediaStream
  let recorder = null;      // MediaRecorder
  let chunks = [];          // recorded data
  let clip = null;          // final Blob
  let clipURL = null;       // object URL for playback (revoked on reset)
  let timerId = null;
  let startedAt = 0;

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

  async function share() {
    resetClip();
    setStatus("");
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 30 },
        audio: true,   // tab audio if the user opts in; ignored otherwise
      });
    } catch (err) {
      // User cancelled the picker, or permission denied — not an error worth shouting about.
      setStatus(err && err.name === "NotAllowedError" ? "Sharing cancelled." : "Couldn't start sharing: " + err, "error");
      return;
    }
    // If the user clicks the browser's native "Stop sharing", tear down gracefully.
    stream.getVideoTracks()[0].addEventListener("ended", function () {
      if (recorder && recorder.state !== "inactive") stopRecording();
      teardownPreview();
    });
    els.preview.srcObject = stream;
    show(els.preview, true);
    show(els.controls, true);
    show(els.start, true);
    show(els.stop, false);
    show(els.reset, false);
    els.share.textContent = "Share a different tab";
    els.hint.textContent = "Click Record when your moment starts. Recording auto-stops at " + fmt(maxSeconds) + ".";
  }

  function teardownPreview() {
    stopTracks();
    els.preview.srcObject = null;
    show(els.preview, false);
    show(els.controls, false);
    els.timer.textContent = "";
    els.share.textContent = "Share a browser tab";
  }

  function startRecording() {
    if (!stream) return;
    resetClip();
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
    show(els.playback, false);
    timerId = setInterval(tick, 250);
    tick();
  }

  function stopRecording() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }

  function onRecordingStopped() {
    clip = new Blob(chunks, { type: UPLOAD_TYPE });
    chunks = [];
    if (!clip.size) { setStatus("Nothing was recorded — try again.", "error"); return; }
    clipURL = URL.createObjectURL(clip);
    els.playback.src = clipURL;
    show(els.preview, false);
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
      const filename = "tab-recording-" + fmt(Math.floor((clip.size % 86400))).replace(":", "") + ".webm";
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
  els.reset.addEventListener("click", function () { resetClip(); show(els.preview, !!stream); });
  els.upload.addEventListener("click", upload);
  window.addEventListener("beforeunload", stopTracks);
})();
