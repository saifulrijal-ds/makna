Ace under a policy without `'unsafe-inline'` needs `useStrictCSP`, the two style files linked, and `useWorker: false` set before the mode; then `style-src` stays as it is.

Seen in WP-19 (2026-11-19, Ace 1.45.2, Chromium).

- By default Ace injects `<style>` elements. `ace.config.set("useStrictCSP", true)` stops that, and the page links `css/ace.css` and `css/theme/textmate.css` from the tarball (same folder layout, because the theme names `../textmate-1.png`).
- Passing `mode` before `useWorker: false` in the options of `ace.edit` starts a blob worker first, which `default-src 'self'` blocks (a console error and a `worker-src` violation). Create the editor with `useWorker: false`, then `session.setMode(...)`.
- Fold widgets are off, so the `main-N` images of `ace.css` are never requested.
- Check after any Ace change with Playwright: listen for `securitypolicyviolation`, `console` errors and failed requests, and count `<style>` elements (0 expected).
