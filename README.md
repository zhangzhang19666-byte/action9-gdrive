# Action 9 — Douyin → Google Drive

Scrape Douyin videos user-by-user and upload to Google Drive.

## Features
- Per-user sequential scraping (scrape all → download videos → next user)
- Parallel downloads with aria2 (8 connections) + 8 concurrent workers
- Resume support: already-scraped users are skipped on next run
- Auto self-trigger after 5h time limit via GitHub Actions

## Required Secrets

Configure these in repo **Settings → Secrets → Actions**:

| Secret | Description |
|--------|-------------|
| `G_CLIENT_ID` | Google OAuth Client ID |
| `G_CLIENT_SECRET` | Google OAuth Client Secret |
| `G_REFRESH_TOKEN` | Google OAuth Refresh Token |
| `GDRIVE_ROOT_FOLDER` | Target GDrive folder ID |
| `DATA_REPO` | Private data repo (`owner/repo`) |
| `DATA_REPO_TOKEN` | PAT with `contents:write` on data repo |
| `GH_PAT` | PAT with `workflow` scope (for self-trigger) |

## Data Repo Structure

The private `DATA_REPO` must contain:
```
input/cookies/douyin_cookie_1.json
output/global_tasks.db   (auto-created on first run)
```

## Usage

Trigger manually: **Actions → Action 9 → Run workflow**
