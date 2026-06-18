// clip.cool meme builder — freeform draggable/resizable text boxes (CSP-clean static script).
// Everything is drawn on ONE canvas (both while editing and on export), so preview === output.
// Boxes are stored in fractional coords (resolution-independent). Publish reuses presign/finalize.
(function () {
  "use strict";
  const form = document.getElementById("builder-form");
  if (!form) return;
  const canvas = document.getElementById("meme-canvas");
  const ctx = canvas.getContext("2d");
  const statusEl = document.getElementById("builder-status");
  const panel = document.getElementById("box-panel");
  const hint = document.getElementById("builder-hint");
  const textEl = document.getElementById("box-text");
  const sizeEl = document.getElementById("box-size");
  const widthEl = document.getElementById("box-width");
  const addBtn = document.getElementById("add-text");
  const delBtn = document.getElementById("del-box");
  const FONT = "Anton, Impact, 'Arial Narrow', sans-serif";
  // burn   = draw the template image + text onto the canvas, export the composite (Phase 1).
  // overlay = caption an existing clip: backdrop is a CSS <video>/<img> behind a transparent
  //           canvas; export the text-only PNG the player overlays / ffmpeg burns in (Phase 2b).
  const mode = form.dataset.mode || "burn";

  const img = new Image();
  img.crossOrigin = "anonymous";
  let boxes = [];      // {text, cx, cy, w, size}  — cx/cy/w/size are fractions of canvas w/h
  let sel = -1;
  let drag = null;     // {mode:'move'|'resize', ...}
  let exporting = false;

  const W = () => canvas.width;
  const H = () => canvas.height;

  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }
  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = "kg-status" + (kind ? " is-" + kind : "");
  }

  function wrap(text, maxWidth, size) {
    ctx.font = size + "px " + FONT;
    const out = [];
    (text || "").split("\n").forEach(function (para) {
      const words = para.split(/\s+/).filter(Boolean);
      let cur = "";
      for (const w of words) {
        const test = cur ? cur + " " + w : w;
        if (!cur || ctx.measureText(test).width <= maxWidth) cur = test;
        else { out.push(cur); cur = w; }
      }
      out.push(cur);
    });
    return out.length ? out : [""];
  }

  function metrics(b) {
    const size = Math.max(8, b.size * H());
    const maxW = b.w * W();                       // wrap width (the Width slider)
    const lines = wrap((b.text || "").toUpperCase(), maxW, size);
    ctx.font = size + "px " + FONT;
    let textW = 1;
    for (const ln of lines) textW = Math.max(textW, ctx.measureText(ln).width);
    const stroke = Math.max(2, size / 8);
    const padX = size * 0.16 + stroke / 2;
    const padY = size * 0.20 + stroke / 2;        // room for the outline above/below the glyphs
    const lineH = size * 1.08;
    const textH = lines.length * lineH;
    const boxW = Math.min(W(), textW + padX * 2);  // hug the text (+ outline), capped at canvas
    const boxH = textH + padY * 2;
    const cx = b.cx * W(), cy = b.cy * H();
    const x = cx - boxW / 2, y = cy - boxH / 2;
    return { size, maxW, boxW, boxH, lines, lineH, cx, cy, x, y, textTop: y + padY };
  }

  function handleSize() { return Math.max(12, W() * 0.022); }

  function render() {
    ctx.clearRect(0, 0, W(), H());
    if (mode === "burn" && img.naturalWidth) ctx.drawImage(img, 0, 0, W(), H());  // overlay = transparent
    boxes.forEach(function (b, i) {
      const m = metrics(b);
      ctx.font = m.size + "px " + FONT;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.lineJoin = "round";
      ctx.lineWidth = Math.max(2, m.size / 8);
      ctx.strokeStyle = "#000";
      ctx.fillStyle = "#fff";
      m.lines.forEach(function (ln, li) {
        const y = m.textTop + (li + 0.5) * m.lineH;   // center of each line slot
        ctx.strokeText(ln, m.cx, y);
        ctx.fillText(ln, m.cx, y);
      });
      if (!exporting && i === sel) {
        const hs = handleSize();
        ctx.save();
        ctx.strokeStyle = "#2563eb";
        ctx.lineWidth = Math.max(1.5, W() * 0.003);
        ctx.setLineDash([7, 5]);
        ctx.strokeRect(m.x, m.y, m.boxW, m.boxH);
        ctx.setLineDash([]);
        ctx.fillStyle = "#2563eb";
        ctx.fillRect(m.x + m.boxW - hs / 2, m.y + m.boxH - hs / 2, hs, hs);  // resize handle
        ctx.restore();
      }
    });
  }

  function pointAt(e) {
    const r = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - r.left) * (canvas.width / r.width),
      y: (e.clientY - r.top) * (canvas.height / r.height),
    };
  }
  function onHandle(b, p) {
    const m = metrics(b), hs = handleSize();
    return p.x >= m.x + m.boxW - hs && p.x <= m.x + m.boxW + hs &&
           p.y >= m.y + m.boxH - hs && p.y <= m.y + m.boxH + hs;
  }
  function onBox(b, p) {
    const m = metrics(b);
    return p.x >= m.x && p.x <= m.x + m.boxW && p.y >= m.y && p.y <= m.y + m.boxH;
  }

  function syncPanel() {
    if (sel < 0) {
      panel.hidden = true;
      hint.hidden = false;
      return;
    }
    panel.hidden = false;
    hint.hidden = true;
    const b = boxes[sel];
    textEl.value = b.text;
    sizeEl.value = Math.round(b.size * 100);
    widthEl.value = Math.round(b.w * 100);
  }

  function addBox() {
    boxes.push({ text: "TEXT", cx: 0.5, cy: boxes.length ? 0.5 : 0.12, w: 0.8, size: 0.1 });
    sel = boxes.length - 1;
    syncPanel();
    render();
    textEl.focus();
    textEl.select();
  }

  canvas.addEventListener("pointerdown", function (e) {
    const p = pointAt(e);
    if (sel >= 0 && onHandle(boxes[sel], p)) {
      drag = { mode: "resize", p: p, size: boxes[sel].size };
      canvas.setPointerCapture(e.pointerId);
      return;
    }
    for (let i = boxes.length - 1; i >= 0; i--) {
      if (onBox(boxes[i], p)) {
        sel = i;
        syncPanel();
        drag = { mode: "move", p: p, cx: boxes[i].cx, cy: boxes[i].cy };
        canvas.setPointerCapture(e.pointerId);
        render();
        return;
      }
    }
    sel = -1;
    syncPanel();
    render();
  });
  canvas.addEventListener("pointermove", function (e) {
    if (!drag || sel < 0) return;
    const p = pointAt(e), b = boxes[sel];
    if (drag.mode === "move") {
      b.cx = Math.min(1, Math.max(0, drag.cx + (p.x - drag.p.x) / W()));
      b.cy = Math.min(1, Math.max(0, drag.cy + (p.y - drag.p.y) / H()));
    } else {
      b.size = Math.min(0.4, Math.max(0.03, drag.size + (p.y - drag.p.y) / H()));
      sizeEl.value = Math.round(b.size * 100);
    }
    render();
  });
  function endDrag() { drag = null; }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  addBtn.addEventListener("click", addBox);
  delBtn.addEventListener("click", function () {
    if (sel < 0) return;
    boxes.splice(sel, 1);
    sel = -1;
    syncPanel();
    render();
  });
  textEl.addEventListener("input", function () {
    if (sel >= 0) { boxes[sel].text = textEl.value; render(); }
  });
  sizeEl.addEventListener("input", function () {
    if (sel >= 0) { boxes[sel].size = sizeEl.value / 100; render(); }
  });
  widthEl.addEventListener("input", function () {
    if (sel >= 0) { boxes[sel].w = widthEl.value / 100; render(); }
  });

  function start() {
    if (!boxes.length) addBox();
    syncPanel();
    render();
  }
  if (mode === "overlay") {
    // Backdrop is the page's <video>/<img>; the canvas is transparent at the clip's native size.
    canvas.width = parseInt(form.dataset.width, 10) || 600;
    canvas.height = parseInt(form.dataset.height, 10) || 600;
    try { boxes = JSON.parse(form.dataset.layers || "[]") || []; } catch (e) { boxes = []; }
    start();
  } else {
    img.onload = function () {
      canvas.width = img.naturalWidth || 600;
      canvas.height = img.naturalHeight || 600;
      start();
    };
    img.onerror = function () { setStatus("Couldn't load the template image.", "error"); };
    img.src = form.dataset.img;
  }
  if (document.fonts && document.fonts.load) {
    document.fonts.load("64px Anton").then(render).catch(function () {});
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken") },
      body: JSON.stringify(body),
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    // Only burn mode draws the template image; overlay mode exports a transparent text-only PNG
    // (the backdrop is a separate <video>/<img>), so there's no img to wait on there.
    if (mode === "burn" && !img.naturalWidth) { setStatus("Template still loading…", "error"); return; }
    sel = -1;             // drop selection so handles aren't rendered
    exporting = true;
    render();
    canvas.toBlob(async function (blob) {
      exporting = false;
      render();
      if (!blob) { setStatus("Could not render the image.", "error"); return; }
      const ctype = "image/png";
      try {
        setStatus(mode === "overlay" ? "Saving caption…" : "Uploading…");
        let res = await postJSON(form.dataset.presign, {
          filename: mode === "overlay" ? "caption.png" : "meme.png", content_type: ctype,
        });
        if (!res.ok) { setStatus("Presign failed: " + (await res.text()), "error"); return; }
        const j = await res.json();
        res = await fetch(j.url, { method: "PUT", headers: { "Content-Type": ctype }, body: blob });
        if (!res.ok) { setStatus("Upload failed (" + res.status + "). Check bucket CORS.", "error"); return; }

        if (mode === "overlay") {
          res = await postJSON(form.dataset.saveUrl, { text_key: j.key, layers: boxes });
          if (!res.ok) { setStatus("Save failed: " + (await res.text()), "error"); return; }
          setStatus("Saved — opening…", "ok");
          window.location.href = form.dataset.assetUrl;
          return;
        }

        setStatus("Finalizing…");
        const caption = boxes.map(function (b) { return b.text.trim(); }).filter(Boolean).join(" / ");
        const title = (caption || form.dataset.name || "").slice(0, 120);
        res = await postJSON(form.dataset.finalize, { key: j.key, title: title, content_type: ctype, tags: [] });
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
