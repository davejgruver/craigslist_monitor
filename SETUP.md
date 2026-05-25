# Craigslist Monitor — Setup Guide

## What you'll end up with
- A worker running in Cloudflare's cloud (no PC needed, always on)
- A bookmarkable webpage to manage your searches from any device
- Texts via Twilio when new posts appear

---

## Step 1 — Create a GitHub repo and upload these files

1. Go to github.com and click the **+** → **New repository**
2. Name it `craigslist-monitor`, make it **Private**, click **Create repository**
3. Upload the files:
   - Click **uploading an existing file**
   - Upload the `worker/` folder contents (index.js and wrangler.toml)
   - Upload the `ui/` folder contents (index.html)
4. Click **Commit changes**

---

## Step 2 — Install Wrangler (Cloudflare's deploy tool)

Open Terminal and run:
```
npm install -g wrangler
```

Then log in to your Cloudflare account:
```
npx wrangler login
```
A browser window will open — click Allow.

---

## Step 3 — Create your KV storage

In Terminal, run:
```
npx wrangler kv:namespace create CL_KV
```

You'll see output like:
```
{ binding = "CL_KV", id = "abc123def456..." }
```

Copy that long ID. Open `worker/wrangler.toml` and replace:
```
id = "REPLACE_WITH_YOUR_KV_ID"
```
with your actual ID.

---

## Step 4 — Set your secrets

Run each of these in Terminal, one at a time.
It will prompt you to type/paste the value after each command:

```
npx wrangler secret put TWILIO_ACCOUNT_SID
npx wrangler secret put TWILIO_AUTH_TOKEN
npx wrangler secret put TWILIO_FROM_NUMBER
npx wrangler secret put UI_PASSWORD
```

- TWILIO_ACCOUNT_SID — from your Twilio dashboard
- TWILIO_AUTH_TOKEN  — from your Twilio dashboard
- TWILIO_FROM_NUMBER — your Twilio phone number, e.g. +14155550100
- UI_PASSWORD        — make up any password, you'll use this to log into the UI

---

## Step 5 — Deploy the Worker

Navigate to your worker folder in Terminal:
```
cd path/to/craigslist-monitor/worker
```

Then deploy:
```
npx wrangler deploy
```

You'll see a URL like:
```
https://craigslist-monitor.yourname.workers.dev
```

Save this URL — you'll need it to log into the UI.

---

## Step 6 — Deploy the UI to Cloudflare Pages

1. Go to dash.cloudflare.com → **Pages** → **Create a project**
2. Choose **Connect to Git** → select your `craigslist-monitor` repo
3. Set:
   - Build command: *(leave empty)*
   - Build output directory: `ui`
4. Click **Save and Deploy**

Cloudflare will give you a URL like:
```
https://craigslist-monitor-abc.pages.dev
```

Bookmark this — it's your management dashboard!

---

## Step 7 — Log in and set up your searches

1. Open your Pages URL in any browser
2. Enter your Worker URL and the UI_PASSWORD you set
3. Add your phone number in Settings
4. Add searches using the form
5. Click **Run check now** to test it immediately

---

## Changing the check interval

Open `worker/wrangler.toml` and change the cron line:
- Every 10 min: `"*/10 * * * *"`
- Every 15 min: `"*/15 * * * *"`  (default)
- Every 30 min: `"*/30 * * * *"`
- Every hour:   `"0 * * * *"`

Then redeploy:
```
npx wrangler deploy
```

---

## Cloudflare free tier limits
- Workers: 100,000 requests/day (more than enough)
- KV: 100,000 reads/day, 1,000 writes/day (more than enough)
- Pages: unlimited

---

## Troubleshooting

**Worker URL not working?**
Make sure you deployed successfully and are using the full https:// URL.

**Not getting texts?**
- Check your Twilio credentials are correct
- Make sure your phone number is in +1XXXXXXXXXX format
- Click "Run check now" in the UI and watch the activity log

**Activity log shows "Outside alert hours"?**
Change alert hours to 24/7 in settings to test, then switch back.
