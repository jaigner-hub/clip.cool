// clip.cool detail-page status poller (CSP-clean: external same-origin script).
// Replaces a meta-refresh (which full-reloaded the page and restarted the playing clip) with a
// quiet background poll of /clips/asset/<id>/status:
//   - transcoding/pending: reload ONCE when it goes ready/failed (just a placeholder → video swap)
//   - caption baking:      leave the playing clip alone; when the burn finishes, just remove the
//                          "baking…" note (the GIF/download are updated server-side at the same key)
(function () {
  "use strict";

  const root = document.getElementById("clip-detail");
  if (!root) return;

  const url = root.dataset.statusUrl;
  const status = root.dataset.status;
  const burning = root.dataset.captionBurning === "1";
  const transcoding = status === "pending" || status === "transcoding";
  if (!url || (!transcoding && !burning)) return;   // nothing to wait for

  const note = document.getElementById("caption-burning-note");
  let tries = 0;

  const timer = setInterval(async function () {
    if (++tries > 200) { clearInterval(timer); return; }   // ~10 min safety cap
    let data;
    try {
      const res = await fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!res.ok) return;        // transient — keep polling
      data = await res.json();
    } catch (e) {
      return;                     // network blip — keep polling
    }

    if (transcoding) {
      // Placeholder is showing; a reload swaps in the real <video> (or the failed state).
      if (data.status === "ready" || data.status === "failed") {
        clearInterval(timer);
        window.location.reload();
      }
    } else if (burning) {
      // Don't reload — the clip is already playing with its live caption overlay. Just drop the note.
      if (!data.caption_burning) {
        clearInterval(timer);
        if (note) note.hidden = true;
      }
    }
  }, 3000);
})();
