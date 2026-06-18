// clip.cool meme builder (CSP-clean: external same-origin script).
// The canvas you edit IS what gets posted — one render path, so preview === output.
// Publish reuses the normal presign -> PUT-to-R2 -> finalize pipeline.
(function () {
  "use strict";
  const form = document.getElementById("builder-form");
  if (!form) return;
  const canvas = document.getElementById("meme-canvas");
  const ctx = canvas.getContext("2d");
  const topEl = document.getElementById("top");
  const bottomEl = document.getElementById("bottom");
  const statusEl = document.getElementById("builder-status");
  const FONT = "Anton, Impact, 'Arial Narrow', sans-serif";

  const img = new Image();
  img.crossOrigin = "anonymous";

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = "kg-status" + (kind ? " is-" + kind : "");
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken") },
      body: JSON.stringify(body),
    });
  }

  function wrap(text, maxWidth, size) {
    ctx.font = size + "px " + FONT;
    const words = text.split(/\s+/).filter(Boolean);
    const lines = [];
    let cur = "";
    for (const w of words) {
      const test = cur ? cur + " " + w : w;
      if (!cur || ctx.measureText(test).width <= maxWidth) cur = test;
      else { lines.push(cur); cur = w; }
    }
    if (cur) lines.push(cur);
    return lines;
  }

  function drawBlock(raw, pos) {
    const text = (raw || "").toUpperCase().trim();
    if (!text) return;
    const W = canvas.width, H = canvas.height, maxWidth = W * 0.92;
    let size = Math.floor(H * 0.14);
    const min = Math.floor(H * 0.05);
    let lines = wrap(text, maxWidth, size);
    while (size > min) {
      const overflow = lines.some((l) => ctx.measureText(l).width > maxWidth);
      if (!overflow && lines.length * size * 1.05 <= H * 0.45) break;
      size -= Math.max(2, Math.floor(size * 0.08));
      lines = wrap(text, maxWidth, size);
    }
    ctx.font = size + "px " + FONT;
    ctx.textAlign = "center";
    ctx.lineJoin = "round";
    ctx.lineWidth = Math.max(2, size / 8);
    ctx.strokeStyle = "#000";
    ctx.fillStyle = "#fff";
    const lineH = size * 1.05;
    const pad = Math.floor(H * 0.025);
    for (let i = 0; i < lines.length; i++) {
      let y;
      if (pos === "top") { ctx.textBaseline = "top"; y = pad + i * lineH; }
      else { ctx.textBaseline = "bottom"; y = H - pad - (lines.length - 1 - i) * lineH; }
      ctx.strokeText(lines[i], W / 2, y);
      ctx.fillText(lines[i], W / 2, y);
    }
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (img.naturalWidth) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    drawBlock(topEl.value, "top");
    drawBlock(bottomEl.value, "bottom");
  }

  img.onload = function () {
    canvas.width = img.naturalWidth || 600;
    canvas.height = img.naturalHeight || 600;
    draw();
  };
  img.onerror = function () { setStatus("Couldn't load the template image.", "error"); };
  img.src = form.dataset.img;

  [topEl, bottomEl].forEach((el) => el.addEventListener("input", draw));
  if (document.fonts && document.fonts.load) {
    document.fonts.load("64px Anton").then(draw).catch(function () {});
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!img.naturalWidth) { setStatus("Template still loading…", "error"); return; }
    setStatus("Rendering…");
    canvas.toBlob(async function (blob) {
      if (!blob) { setStatus("Could not render the image.", "error"); return; }
      const ctype = "image/png";
      try {
        let res = await postJSON(form.dataset.presign, { filename: "meme.png", content_type: ctype });
        if (!res.ok) { setStatus("Presign failed: " + (await res.text()), "error"); return; }
        const { key, url } = await res.json();
        setStatus("Uploading…");
        res = await fetch(url, { method: "PUT", headers: { "Content-Type": ctype }, body: blob });
        if (!res.ok) { setStatus("Upload failed (" + res.status + "). Check bucket CORS.", "error"); return; }
        setStatus("Finalizing…");
        const title = (topEl.value || bottomEl.value || form.dataset.name || "").trim().slice(0, 120);
        res = await postJSON(form.dataset.finalize, { key: key, title: title, content_type: ctype, tags: [] });
        if (!res.ok) { setStatus("Finalize failed: " + (await res.text()), "error"); return; }
        const asset = await res.json();
        setStatus("Posted — opening…", "ok");
        window.location.href = "/clips/asset/" + encodeURIComponent(asset.id) + "/";
      } catch (err) {
        setStatus("Error: " + err, "error");
      }
    }, "image/png");
  });
})();
