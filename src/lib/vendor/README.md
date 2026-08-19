# Vendored browser assets

`mermaid.min.js` is fetched here by `scripts/vendor-mermaid.sh` (also run from
`scripts/sync.sh`, best-effort) so `render_diagram` can draw Mermaid diagrams
without touching the CDN at render time. The file is gitignored; when it is
missing, `render_diagram` falls back to jsDelivr.

Pinned version: see `MERMAID_VERSION` in `scripts/vendor-mermaid.sh`
(≥ 11.16 for `wardley-beta` and `swimlane-beta`).
