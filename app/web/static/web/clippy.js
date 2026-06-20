// Alpine CSP build component for the Clippy assistant (see lab.js for the pattern:
// registered object, no inline-expression eval, so it runs under strict CSP).
// Loaded before alpine-csp.min.js so the component is registered when Alpine
// dispatches `alpine:init`.
//
// Context tips are keyed by the page's `active_page` (passed via data-page on the
// widget root). Anything unknown falls back to the greeting.
document.addEventListener('alpine:init', () => {
  const TIPS = {
    clips:         "It looks like you're searching! Type what's in the clip — we read the text and the pictures, not just the title.",
    clips_browse:  "Browsing the latest. See one you like? Click through for the video, the GIF, or a quick caption.",
    clips_library: "Your clips live here. Click one to caption it, edit it, or copy a share link.",
    clips_record:  "It looks like you're recording a tab! Crop, trim and caption right here — no plugin needed.",
  };
  const DEFAULT_TIP = "Hi, I'm Clippy! It looks like you're hosting clips. Need a hand?";
  const HIDE_KEY = 'clippy-hidden';

  Alpine.data('clippy', () => ({
    open: true,
    hidden: false,
    tip: DEFAULT_TIP,
    init() {
      try { this.hidden = localStorage.getItem(HIDE_KEY) === '1'; } catch (e) {}
      const page = this.$el.dataset.page || '';
      this.tip = TIPS[page] || DEFAULT_TIP;
    },
    get shown() { return !this.hidden; },
    toggle() { this.open = !this.open; },
    close() { this.open = false; },
    hide() {
      this.hidden = true;
      this.open = false;
      try { localStorage.setItem(HIDE_KEY, '1'); } catch (e) {}
    },
    show() {
      this.hidden = false;
      this.open = true;
      try { localStorage.removeItem(HIDE_KEY); } catch (e) {}
    },
  }));
});
