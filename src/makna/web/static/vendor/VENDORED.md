# Vendored files

These files are copied unchanged from npm tarballs, so the pages need no Node.js, no bundler, and
no request to a CDN (ARCHITECTURE section 13, D-8). A test (`tests/unit/test_web_security.py`)
checks that each file below still has the SHA-256 recorded here.

To update one: download the tarball from the URL with a newer version (at least two weeks old),
check its SHA-256 against the registry's `dist.integrity`, copy only the files listed, and change
the row. Never edit a vendored file by hand.

| Project | Version | Licence | Tarball (SHA-256 of the `.tgz`) |
|---|---|---|---|
| HTMX (`htmx.org`) | 2.0.11 | 0BSD | https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz (`165a2655073e9e6e55e307a7c74e8ebcdb76afb7701f9c16cfd106a13add408e`) |
| Pico CSS (`@picocss/pico`) | 2.1.1 | MIT | https://registry.npmjs.org/@picocss/pico/-/pico-2.1.1.tgz (`4affbb56f49df6d5325610b06762156f1dcdbc6ef57cea5c136d7e8a00a8cd24`) |
| Ace (`ace-builds`) | 1.45.2 | BSD-3-Clause | https://registry.npmjs.org/ace-builds/-/ace-builds-1.45.2.tgz (`5260354a14533fa8e94572597b1ff53025b1bb0a9b2e25d50ecf2a01e57aa9e0`) |

## Files

Path | Taken from the tarball | SHA-256 of the file
---|---|---
`htmx/htmx.min.js` | `package/dist/htmx.min.js` | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`
`htmx/LICENSE` | `package/LICENSE` | `d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38`
`pico/pico.classless.min.css` | `package/css/pico.classless.min.css` | `61207a40ffc02a42d1e50143651c121beab70ed413c934c1ff84fa263ba436b0`
`pico/LICENSE.md` | `package/LICENSE.md` | `afaff063e044233f917b7807dccab022f09dca474f92b642846bee4850655bbf`
`ace/ace.js` | `package/src-min-noconflict/ace.js` | `6430923a29a3d30f94eb321700a56ec12cb8060270831632087d166bbac7aca9`
`ace/mode-yaml.js` | `package/src-min-noconflict/mode-yaml.js` | `24faa242ac5085656ff0b9fe283edc8d8565c2ad84f5a824b5868eb5a7ca5cb3`
`ace/theme-textmate.js` | `package/src-min-noconflict/theme-textmate.js` | `b6d8bae9abd821bc73ad30efa2b640953dee0132a26169330b80b25f15295937`
`ace/LICENSE` | `package/LICENSE` | `850f545c5254e8c08204edb2a6d50bbb72727c38a82982c17a84bebf6064cdf8`
`ace/ace.css` | `package/css/ace.css` | `2272e3f6e5efac0fed1e2d4815fe2c3223fd6cda22aab6cd397fe6881f49ec13`
`ace/textmate.css` | `package/css/theme/textmate.css` | `7448140ab6e9343d43441e9a39fe67b99b285ba305b7c725e7745969e2b1c94f`
`ace/main-23.png` | `package/css/main-23.png` | `1a020e872a95cde139d1be39a9c9a4905fdf67e9bd7fa53e72d6e83218cdabf6`
`ace/main-24.png` | `package/css/main-24.png` | `5dcf845557b32e1af2c3a19623209bfef6f335f215d19f46f4d152544acc94d3`

## Why these files

- **Pico: `pico.classless.min.css`.** The pages are plain semantic HTML (`nav`, `main`, `article`,
  `form`, `table`), which the classless build styles without a class on every element, so the
  templates stay small. The full build adds the `.container`, `.grid`, and other class-based
  rules, which no page uses. It contains one kind of absolute URL: the SVG namespace
  `http://www.w3.org/2000/svg` inside `data:` images. That is an identifier, not an address, and
  the browser requests nothing from it.
- **HTMX: `htmx.min.js`.** The one script of the pages. The pages set `allowEval` to false in
  the `htmx-config` meta tag, so `hx-on` and `js:` values are off.
- **Ace: `ace.js`, `mode-yaml.js`, `theme-textmate.js`.** The minimum for a YAML editor
  (WP-19): the editor, the YAML mode, and one theme (the editor's default). The YAML worker
  (`worker-yaml.js`, about 120 kB) is left out; add it, with its row, if WP-19 wants the
  editor's own syntax check. Ace loads its modes and themes from the folder of `ace.js`, so set
  `ace.config.set("basePath", "/static/vendor/ace")`.
