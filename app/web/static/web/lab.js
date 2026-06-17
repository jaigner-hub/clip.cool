// Alpine CSP build: components are registered objects (no inline-expression eval),
// so this works under `script-src 'self'` with no 'unsafe-eval'. Loaded before
// alpine-csp.min.js (defer preserves order), so the component is registered when
// Alpine dispatches `alpine:init`.
document.addEventListener('alpine:init', () => {
  Alpine.data('counter', () => ({
    count: 0,
    inc() { this.count++; },
    reset() { this.count = 0; },
  }));
});
