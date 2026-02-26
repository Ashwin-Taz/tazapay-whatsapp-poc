# Tazapay WhatsApp POC

A WhatsApp-powered Tazapay payment assistant. Merchants send plain English commands via WhatsApp and get real-time responses powered by Claude AI + Tazapay API.

## Architecture

```
Merchant (WhatsApp)
        ↓
  Twilio WhatsApp API
        ↓
  Flask app on Render  ←── GitHub (auto-deploy on push)
        ↓
  Claude AI (claude-sonnet-4-6)
        ↓
  Tazapay API
        ↓
  Reply → WhatsApp
```

## Supported Commands

Merchants send natural language — no rigid syntax needed:

| Intent | Example |
|--------|---------|
| Check balance | "What's my balance?" / "Show USD balance" |
| FX rate | "USD to INR rate for 1000" |
| Payment link | "Create a $250 USD payment link for John (john@email.com, SG) for Invoice #42" |
| Payout status | "Check payout po_abc123" |
| Reset chat | "reset" |

---

## Deployment Guide

### Step 1 — Fork & clone the repo

```bash
git clone https://github.com/YOUR_ORG/tazapay-whatsapp-poc.git
cd tazapay-whatsapp-poc
```

### Step 2 — Deploy to Render

**Option A: One-click via render.yaml (recommended)**

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**
2. Connect your GitHub repo
3. Render will detect `render.yaml` and create the service automatically

**Option B: Manual**

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**
2. Connect your GitHub repo
3. Set:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT --timeout 60`
   - **Health Check Path:** `/health`

### Step 3 — Set environment variables on Render

In your Render service → **Environment** tab, add:

| Key | Value |
|-----|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic key |
| `TWILIO_ACCOUNT_SID` | From Twilio console |
| `TWILIO_AUTH_TOKEN` | From Twilio console |
| `TWILIO_WHATSAPP_NUMBER` | `whatsapp:+14155238886` (sandbox) |
| `TAZAPAY_API_KEY` | Your Tazapay key |
| `TAZAPAY_API_SECRET` | Your Tazapay secret |
| `TAZAPAY_BASE_URL` | `https://api.tazapay.com` |
| `AUTHORIZED_NUMBERS` | Leave empty for POC |

### Step 4 — Get your Render URL

After deploy completes, your app will be live at:
```
https://tazapay-whatsapp-poc.onrender.com
```

Verify it's running:
```
GET https://tazapay-whatsapp-poc.onrender.com/health
→ { "status": "ok" }
```

### Step 5 — Configure Twilio Sandbox

1. Go to [Twilio Console → Messaging → Try WhatsApp](https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn)
2. Join the sandbox: send the join code to `+1 415 523 8886` on WhatsApp
3. In **Sandbox Settings**, set **"When a message comes in"**:
   ```
   https://tazapay-whatsapp-poc.onrender.com/webhook
   ```
   Method: `POST`
4. Save

### Step 6 — Test

Send a WhatsApp to `+1 415 523 8886`:
```
What's my balance?
```

You should get a reply within a few seconds. 

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill env file
cp .env.example .env

# Run locally
python app.py

# In another terminal, expose via ngrok for Twilio webhook
ngrok http 5000
```

---

## CI/CD Flow

```
git push origin main
        ↓
GitHub Actions runs CI (lint + import check)
        ↓
On success → Render auto-deploys (GitHub integration)
        ↓
New version live in ~2 minutes
```

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push and PR to `main`. Render's GitHub integration auto-deploys when CI passes and a new commit lands on `main`.

---

## Project Structure

```
tazapay-whatsapp-poc/
├── app.py                          # Flask app + Claude agent + Tazapay tools
├── requirements.txt                # Python dependencies (incl. gunicorn)
├── Procfile                        # Process definition for Render
├── render.yaml                     # Render Blueprint (one-click deploy)
├── .env.example                    # Environment variable template
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions CI pipeline
└── README.md
```

---

## Adding New Commands

Add a tool to `TOOLS` list and a handler in `execute_tool()` in `app.py`. Claude automatically understands when to use it. Push to `main` and Render deploys it.

```python
# Example: add payout creation
{
    "name": "create_payout",
    "description": "Create a Tazapay payout to a beneficiary...",
    "input_schema": { ... }
}
```

---

## Production Checklist

- [ ] Set `AUTHORIZED_NUMBERS` to restrict access
- [ ] Upgrade Render plan (free tier spins down after 15 min inactivity)
- [ ] Switch Twilio sandbox → Meta Business API (dedicated number)
- [ ] Add Twilio request signature validation
- [ ] Replace in-memory conversation store with Redis
- [ ] Add payout confirmation flow ("Reply YES to confirm")
- [ ] Set up Render alerts for crashes
