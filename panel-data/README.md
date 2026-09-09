# panel-data

Written by CI, never by hand.

- `index.json` — merged run history consumed by the Pages panel when the Vercel
  backend is unreachable. Refreshed by `pages.yml` on push and every 30 minutes.
- `runs/<run_id>.json` — full `pipeline.json` snapshot for one run, produced by
  the `report` job of `pipeline.yml`.

`pages.yml` copies this directory to `_site/data/` and adds a generated
`config.json` (repository, backend URL, destination repos, build time).
