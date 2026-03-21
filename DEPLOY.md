# Kalshi Prediction Tracker — Deployment Guide

## Architecture Overview

```
predictiontracker.jylee.co (Squarespace subdomain)
        │  CNAME pointing to
        ▼
  Railway (hosts FastAPI backend + frontend)
        │
        ├── Polls Kalshi public API every 60s
        ├── Detects ≥10% price movements
        ├── Calls Claude API for news analysis
        └── Sends alerts via Telegram Bot API
```

## Step 1: Get Your API Keys Ready

You'll need three things:

| Key | Where to get it |
|-----|-----------------|
| `TELEGRAM_BOT_TOKEN` | You already have this |
| `TELEGRAM_CHAT_ID` | You already have this |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |

## Step 2: Push to GitHub

```bash
# Create a new GitHub repo (e.g., kalshi-tracker)
cd kalshi-tracker
git init
git add .
git commit -m "Initial commit: Kalshi Prediction Tracker"
git remote add origin https://github.com/YOUR_USERNAME/kalshi-tracker.git
git push -u origin main
```

## Step 3: Deploy on Railway

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub Repo"**
3. Select your `kalshi-tracker` repository
4. Railway will auto-detect the Dockerfile and begin building

### Set Environment Variables

In your Railway project dashboard, go to **Variables** and add:

```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ANTHROPIC_API_KEY=sk-ant-...
POLL_INTERVAL_SECONDS=60
MIN_VOLUME_USD=10000
PRICE_CHANGE_THRESHOLD=0.10
```

### Generate a Domain

In **Settings → Networking → Public Networking**, click **"Generate Domain"**.
Railway will give you a URL like `kalshi-tracker-production.up.railway.app`.

Test it by visiting that URL — you should see the tracker dashboard.

## Step 4: Connect predictiontracker.jylee.co

### In Railway:
1. Go to **Settings → Networking → Custom Domain**
2. Add: `predictiontracker.jylee.co`
3. Railway will show you a **CNAME target** (e.g., `abc123.up.railway.app`)

### In Squarespace:
Since `jylee.co` is managed by Squarespace, you need to add a CNAME record:

1. Go to **Squarespace** → **Settings** → **Domains** → **jylee.co** → **DNS Settings**
2. Add a new record:
   - **Type:** CNAME
   - **Host:** `predictiontracker`
   - **Value:** the CNAME target Railway gave you (e.g., `abc123.up.railway.app`)
   - **TTL:** default (or 3600)
3. Save and wait 5–30 minutes for DNS propagation

### Verify:
Once propagated, visit `https://predictiontracker.jylee.co` — it should load your tracker.

Railway provides automatic HTTPS via Let's Encrypt, so SSL will work automatically.

## Step 5: Using the Tracker

1. Visit `predictiontracker.jylee.co`
2. Type a topic (e.g., "Iran war") and click **Start Tracking**
3. The app will:
   - Search all open Kalshi markets matching your keywords
   - Filter to markets with ≥ $10,000 volume
   - Monitor prices every 60 seconds
   - Send you a Telegram alert when any market moves ≥ 10%
   - Include a Claude-powered analysis of what likely caused the move
4. To switch topics, just type a new one and click **Start Tracking** again
5. Click **Stop** to pause monitoring

## Customization

### Adjust thresholds via the API:
```bash
curl -X PATCH https://predictiontracker.jylee.co/api/settings \
  -H "Content-Type: application/json" \
  -d '{"poll_interval": 30, "min_volume": 5000, "price_threshold": 0.05}'
```

### Available settings:
- `poll_interval`: Seconds between checks (default: 60, min: 10)
- `min_volume`: Minimum market volume in USD (default: 10000)
- `price_threshold`: Alert threshold as decimal (0.05 = 5%, default: 0.10 = 10%)

## Costs

- **Railway:** Free tier includes 500 hours/month. Hobby plan ($5/mo) for always-on
- **Anthropic API:** ~$0.01–0.05 per alert (Claude Sonnet with web search)
- **Telegram:** Free
- **Kalshi API:** Free (public market data, no auth needed)

## Troubleshooting

- **No markets found?** Try broader keywords. Kalshi market titles may not match exactly.
- **No Telegram messages?** Verify your bot token and chat ID. Send `/start` to your bot first.
- **Railway deploy fails?** Check the build logs in Railway dashboard.
