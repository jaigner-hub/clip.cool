// Copy-to-clipboard for any <button data-copy="<css-selector>"> — copies the target element's
// text. Self-hosted + event-delegated so it's CSP-safe (no inline onclick) and works for
// dynamically-added buttons.
document.addEventListener("click", function (e) {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const target = document.querySelector(btn.getAttribute("data-copy"));
  if (!target || !navigator.clipboard) return;
  navigator.clipboard.writeText(target.textContent.trim()).then(function () {
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(function () { btn.textContent = original; }, 1500);
  });
});
