// clip.cool shared crop + trim editor (CSP-clean: external same-origin script, no inline JS).
//
// A reusable widget bound to a <video> that already has a playable src. It draws a crop box over the
// video (drag to select a region) and a trim scrubber beneath it (drag in/out handles), and exposes
// cropFractions() + trimPayload() for the server — both are baked by ffmpeg at transcode, never in
// the browser. Lifted verbatim from the tab recorder so the recorder (record.js, post-capture) and
// the remix editor (remix.js) share one implementation.
//
// Usage:
//   const edit = ClipEdit.init({ video, cropCanvas, cropReset, trim, trimBar, trimSel,
//                                trimPlayhead, trimIn, trimOut, trimLabel, trimReset, isLocked });
//   edit.arm(durationSeconds);   // (re)initialise once the video's duration is known
//   const body = { ...edit.trimPayload() };  const cf = edit.cropFractions();
(function () {
  "use strict";

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // opts: DOM els (see usage above) + optional isLocked() → bool that, when true, ignores crop drags
  // (the recorder uses it to block re-cropping mid-record). cropReset/trim* may be absent.
  function init(opts) {
    const els = opts;
    const isLocked = opts.isLocked || function () { return false; };
    const MIN_CROP_PX = 16;   // a drag smaller than this (in display px) is treated as a stray click

    let cropDisp = null;      // selection in display (overlay) px: {x,y,w,h}; null = whole frame
    let dragStart = null;     // pointer-down point while dragging
    let clipDuration = 0;     // video length (s) — source of truth for the trim bar
    let trimInS = 0;          // kept-range start (s)
    let trimOutS = 0;         // kept-range end (s)
    let trimDrag = null;      // "in" | "out" while dragging a handle

    function show(el, on) { if (el) el.hidden = !on; }

    // --- trim scrubber (drag in/out over the clip; applied at transcode) ---

    function layoutTrim() {
      if (!els.trimBar || clipDuration <= 0) return;
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
      if (els.trimPlayhead && clipDuration > 0) els.trimPlayhead.style.left = ((t / clipDuration) * 100) + "%";
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
      try { els.video.currentTime = edge; } catch (e) {}
      setPlayhead(edge);
      layoutTrim();
    }

    function endTrimDrag() { trimDrag = null; }

    function resetTrim() { trimInS = 0; trimOutS = clipDuration; layoutTrim(); setPlayhead(0); }

    // Fraction-free seconds of the kept range to send the server; {} when the whole clip is kept.
    function trimPayload() {
      const body = {};
      if (clipDuration > 0) {
        if (trimInS > 0.05) body.trim_start = trimInS;
        if (trimOutS < clipDuration - 0.05) body.trim_end = trimOutS;
      }
      return body;
    }

    // --- crop selection (overlay drawn in display px; converted to source fractions at upload) ---

    function syncOverlaySize() {
      // 1 overlay px == 1 displayed CSS px, so pointer offsets map straight onto the canvas.
      const w = els.video.clientWidth, h = els.video.clientHeight;
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
        const label = "✂ Drag across the clip to crop — or keep the whole frame";
        ctx.font = "600 13px system-ui, -apple-system, sans-serif";
        ctx.textBaseline = "top";
        const tw = Math.min(ctx.measureText(label).width, c.width - 20);
        ctx.fillStyle = "rgba(11,18,32,0.7)";
        ctx.fillRect((c.width - tw) / 2 - 10, 10, tw + 20, 26);
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
      if (isLocked()) return;
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

    // The selection as fractions (0..1) of the source frame — resolution-independent. null = no crop.
    function cropFractions() {
      const c = els.cropCanvas;
      if (!cropDisp || !c.width || !c.height) return null;
      return { x: cropDisp.x / c.width, y: cropDisp.y / c.height, w: cropDisp.w / c.width, h: cropDisp.h / c.height };
    }

    // --- wiring ---
    els.cropCanvas.addEventListener("pointerdown", onPointerDown);
    els.cropCanvas.addEventListener("pointermove", onPointerMove);
    els.cropCanvas.addEventListener("pointerup", onPointerUp);
    if (els.cropReset) els.cropReset.addEventListener("click", clearCrop);

    if (els.trimBar) {
      els.trimIn.addEventListener("pointerdown", onTrimDown("in"));
      els.trimOut.addEventListener("pointerdown", onTrimDown("out"));
      els.trimBar.addEventListener("pointerdown", function (ev) {
        if (ev.target === els.trimIn || ev.target === els.trimOut) return;  // a handle grab
        const t = barSeconds(ev.clientX);
        try { els.video.currentTime = t; } catch (e) {}
        setPlayhead(t);
      });
      els.trimBar.addEventListener("pointermove", onTrimMove);
      els.trimBar.addEventListener("pointerup", endTrimDrag);
      els.trimBar.addEventListener("pointercancel", endTrimDrag);
      if (els.trimReset) els.trimReset.addEventListener("click", resetTrim);
    }
    window.addEventListener("pointerup", endTrimDrag);

    // Loop playback within the kept range, and track the playhead.
    els.video.addEventListener("timeupdate", function () {
      const t = els.video.currentTime;
      if (clipDuration > 0 && (t >= trimOutS || t < trimInS - 0.05)) {
        try { els.video.currentTime = trimInS; } catch (e) {}
      }
      setPlayhead(els.video.currentTime);
    });

    // Resize invalidates the display-px crop mapping — clear it and re-fit rather than bake a stale box.
    window.addEventListener("resize", function () { clearCrop(); layoutTrim(); });

    return {
      // (re)initialise the trim range to the full clip and clear any crop, once duration is known.
      arm: function (duration) {
        clipDuration = (isFinite(duration) && duration > 0) ? duration : 0;
        trimInS = 0;
        trimOutS = clipDuration;
        layoutTrim();
        setPlayhead(0);
        show(els.trim, true);
        clearCrop();
        requestAnimationFrame(drawBand);
      },
      cropFractions: cropFractions,
      trimPayload: trimPayload,
      clearCrop: clearCrop,
      resetTrim: resetTrim,
      redraw: drawBand,            // repaint the overlay (e.g. after a layout change / PiP move)
      relayout: layoutTrim,
    };
  }

  window.ClipEdit = { init: init };
})();
