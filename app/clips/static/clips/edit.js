// Tag chip editor for the clip edit form (CSP-clean: external same-origin script).
// Chips are the source of truth; we serialize them into the hidden `tags` input on submit.
(function () {
  "use strict";
  const editor = document.getElementById("tag-editor");
  if (!editor) return;
  const input = document.getElementById("tag-input");
  const hidden = document.getElementById("tags-hidden");
  const form = editor.closest("form");

  function chips() {
    return Array.from(editor.querySelectorAll(".tag-chip"));
  }
  function sync() {
    hidden.value = chips().map((c) => c.dataset.tag).join(", ");
  }
  function addTag(raw) {
    const tag = (raw || "").trim();
    if (!tag) return;
    if (chips().some((c) => c.dataset.tag.toLowerCase() === tag.toLowerCase())) return;
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.dataset.tag = tag;
    chip.textContent = tag;
    const x = document.createElement("button");
    x.type = "button";
    x.className = "tag-x";
    x.setAttribute("aria-label", "remove tag");
    x.textContent = "×";
    chip.appendChild(x);
    editor.insertBefore(chip, input);
    sync();
  }

  editor.addEventListener("click", function (e) {
    if (e.target.classList.contains("tag-x")) {
      e.target.parentElement.remove();
      sync();
    } else if (e.target === editor) {
      input.focus();
    }
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(input.value);
      input.value = "";
    } else if (e.key === "Backspace" && !input.value) {
      const cs = chips();
      if (cs.length) {
        cs[cs.length - 1].remove();
        sync();
      }
    }
  });
  // Commit a half-typed tag on blur and on submit so nothing is lost.
  input.addEventListener("blur", function () {
    if (input.value.trim()) {
      addTag(input.value);
      input.value = "";
    }
  });
  if (form) {
    form.addEventListener("submit", function () {
      if (input.value.trim()) {
        addTag(input.value);
        input.value = "";
      }
      sync();
    });
  }
  sync();
})();
