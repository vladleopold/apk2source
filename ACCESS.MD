# ACCESS — CI/CD automation credentials & endpoints

This document describes every credential the `apk2source` automation surface needs.

---

## Cloudflare Worker env vars

| Secret | Value / notes |
|---|---|
| `APK2SOURCE_GITHUB_TOKEN` | Set via: `wrangler secret put GITHUB_TOKEN` |
| `PANEL_ACCESS_KEY` | Set via: `wrangler secret put PANEL_ACCESS_KEY` |
| `APK2SOURCE_WEBHOOK_SECRET` | Set via: `wrangler secret put APK2SOURCE_WEBHOOK_SECRET` |

> **Current PANEL_ACCESS_KEY:** `d6d0e1b39714d1304018c209bbf0e352d57b99ff6de02eac`

---

## GitHub repo secrets (Settings → Secrets → Actions)

| Name | Purpose |
|---|---|
| `PUBLISH_TOKEN` | fine-grained PAT with `contents: write` on `source_spine` and `game_source` |
| `APK2SOURCE_S3_ENDPOINT` | S3 / R2 / MinIO endpoint (optional artifact bus) |
| `APK2SOURCE_S3_BUCKET` | bucket name |
| `APK2SOURCE_S3_ACCESS_KEY_ID` | access key |
| `APK2SOURCE_S3_SECRET_ACCESS_KEY` | secret key |

---

## Endpoints

All paths are relative to the backend base URL.

| Method | Path | Auth | What it does |
|---|---|---|---|
| `GET` | `/api/health` | — | liveness probe |
| `GET` | `/api/config` | — | repo + stage metadata |
| `GET` | `/api/runs?per_page=50` | — | list workflow runs |
| `GET` | `/api/run/<id>` | — | single run + jobs |
| `GET` | `/api/artifacts?run_id=<id>` | — | list artifacts |
| `GET` | `/api/tree?repo=…&path=…` | — | browse destination repos |
| `POST` | `/api/trigger` | `X-Panel-Key` | dispatch workflow |
| `POST` | `/api/stage` | `X-Panel-Key` | dispatch a single stage |
| `GET` | `/api/automation/status` | — | workflows + recent runs |
| `POST` | `/api/automation/selftest` | `X-Panel-Key` | dispatch selftest |
| `POST` | `/api/automation/notify` | `X-Panel-Key` | POST to a webhook (Telegram/Slack) |
| `POST` | `/api/webhook` | HMAC signature | inbound webhook → dispatch automation |

---

## Panel configuration

Open: https://vladleopold.github.io/apk2source/

Settings:
- Backend URL: `https://apk2source-api.leopolds2010.workers.dev`
- Panel access key: `d6d0e1b39714d1304018c209bbf0e352d57b99ff6de02eac`

---

## Quick start

```bash
# 1. Set GitHub token on Worker
wrangler secret put GITHUB_TOKEN

# 2. Open panel
# https://vladleopold.github.io/apk2source/

# 3. Configure panel → Settings → enter backend URL + panel key
```
