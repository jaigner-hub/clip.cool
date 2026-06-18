// Copy-to-clipboard for [data-copy="#selector"] buttons + confirm-on-submit for
// form[data-confirm] (CSP-clean, delegated — no inline handlers).
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    const form = e.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const el = document.querySelector(btn.dataset.copy);
    if (!el) return;
    const done = function () {
      const prev = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(function () { btn.textContent = prev; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(el.value).then(done).catch(function () { el.select(); });
    } else {
      el.select();
      try { document.execCommand("copy"); done(); } catch (err) { /* user copies manually */ }
    }
  });
})();
