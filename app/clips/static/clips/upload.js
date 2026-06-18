// clip.cool upload flow (CSP-clean: external same-origin script, no inline JS).
// 1) POST /clips/upload/presign  -> { key, url, method, headers }
// 2) PUT the file straight to R2 (presigned URL; cross-origin, allowed by CSP connect-src + R2 CORS)
// 3) POST /clips/upload/finalize -> the created asset (processing is async)
(function () {
  "use strict";

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

  const form = document.getElementById("clip-upload");
  if (!form) return;
  const fileInput = document.getElementById("clip-file");
  const titleInput = document.getElementById("clip-title");
  const tagsInput = document.getElementById("clip-tags");
  const statusEl = document.getElementById("clip-status");
  const presignURL = form.dataset.presignUrl;
  const finalizeURL = form.dataset.finalizeUrl;
  const searchURL = form.dataset.searchUrl;

  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = "kg-status" + (kind ? " is-" + kind : "");
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) { setStatus("Pick a file first.", "error"); return; }
    const contentType = file.type || "application/octet-stream";

    try {
      setStatus("Requesting upload URL…");
      let res = await postJSON(presignURL, { filename: file.name, content_type: contentType });
      if (!res.ok) { setStatus("Presign failed: " + (await res.text()), "error"); return; }
      const { key, url } = await res.json();

      setStatus("Uploading to storage…");
      res = await fetch(url, { method: "PUT", headers: { "Content-Type": contentType }, body: file });
      if (!res.ok) { setStatus("Upload to R2 failed (" + res.status + "). Check bucket CORS.", "error"); return; }

      setStatus("Finalizing…");
      const tags = (tagsInput.value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
      res = await postJSON(finalizeURL, { key: key, title: titleInput.value || "", content_type: contentType, tags: tags });
      if (!res.ok) { setStatus("Finalize failed: " + (await res.text()), "error"); return; }
      const asset = await res.json();

      setStatus("Uploaded — opening your clip…", "ok");
      // Land on the asset page; it auto-refreshes while the poster/OCR/AI labels generate.
      window.location.href = "/clips/asset/" + encodeURIComponent(asset.id) + "/";
    } catch (err) {
      setStatus("Error: " + err, "error");
    }
  });
})();
