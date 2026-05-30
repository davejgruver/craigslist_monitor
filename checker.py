"""
Craigslist Alert Checker — runs via GitHub Actions
Uses Craigslist's internal JSON search API
"""

import os, json, hashlib, requests, time, random
from datetime import datetime, timezone

# ── Cloudflare KV helpers ─────────────────────────────────────────────────────

CF_ACCOUNT_ID      = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN       = os.environ["CF_API_TOKEN"]

KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {"Authorization": f"Bearer {CF_API_TOKEN}"}

def kv_get(key):
    r = requests.get(f"{KV_BASE}/values/{key}", headers=KV_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text

def kv_put(key, value):
    r = requests.put(
        f"{KV_BASE}/values/{key}",
        headers=KV_HEADERS,
        data=value if isinstance(value, str) else json.dumps(value)
    )
    r.raise_for_status()

# ── Twilio ────────────────────────────────────────────────────────────────────

TWILIO_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM  = os.environ["TWILIO_FROM_NUMBER"]

def send_text(to, message):
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"To": to, "From": TWILIO_FROM, "Body": message}
    )
    print(f"  Text status: {r.status_code}")
    if r.status_code != 201:
        print(f"  Text error: {r.text}")

# ── Alert hours ───────────────────────────────────────────────────────────────

def is_alert_hour(setting):
    if not setting or setting == "always":
        return True
    hour = (datetime.now(timezone.utc).hour - 7) % 24
    if setting == "day":      return 8 <= hour < 22
    if setting == "business": return 9 <= hour < 17
    return True

# ── Craigslist JSON API ───────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}

def build_search_url(search):
    region   = search["region"]
    category = search["category"]
    keywords = search.get("keywords", [])
    query    = " ".join(keywords)
    
    # Use Craigslist's JSON search endpoint
    url = f"https://{region}.craigslist.org/search/{category}?format=rss&sort=date"
    
    # Actually use the JSON API
    params = {
        "query": query,
        "sort": "date",
        "srchType": "A",  # all words
    }
    if search.get("min_price"):
        params["min_price"] = search["min_price"]
    if search.get("max_price"):
        params["max_price"] = search["max_price"]
    
    param_str = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"https://{region}.craigslist.org/search/{category}?{param_str}"

def fetch_posts(search):
    region   = search["region"]
    category = search["category"]
    keywords = search.get("keywords", [])
    query    = " ".join(keywords)

    # Craigslist internal JSON API
    params = {
        "query": query,
        "sort": "date",
        "srchType": "A",
        "format": "json",
    }
    if search.get("min_price"):
        params["min_price"] = search["min_price"]
    if search.get("max_price"):
        params["max_price"] = search["max_price"]

    url = f"https://{region}.craigslist.org/search/{category}"
    
    try:
        time.sleep(random.uniform(1, 2))
        print(f"  Fetching JSON: {url}?query={query}")
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        print(f"  Status: {r.status_code}")
        
        if r.status_code != 200:
            print(f"  Response: {r.text[:200]}")
            return []

        # Try parsing as JSON first
        try:
            data = r.json()
            print(f"  JSON keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            
            # Craigslist JSON API returns items in data['data']['items']
            items = []
            if isinstance(data, dict):
                items = (data.get("data", {}).get("items", []) or
                        data.get("items", []) or
                        data.get("results", []))
            elif isinstance(data, list):
                items = data
                
            print(f"  Items found: {len(items)}")
            
            posts = []
            for item in items:
                # Handle different possible structures
                link  = item.get("url") or item.get("link") or ""
                title = item.get("title") or item.get("name") or ""
                price = item.get("price") or item.get("ask") or ""
                
                if not link and item.get("id"):
                    link = f"https://{region}.craigslist.org{item.get('path', '')}"
                
                if not link:
                    continue
                    
                if isinstance(price, (int, float)):
                    price = f"${int(price)}"
                    
                post_id = hashlib.md5(str(link).encode()).hexdigest()
                posts.append({
                    "id": post_id,
                    "title": str(title).strip(),
                    "link": str(link).strip(),
                    "price": str(price).strip() if price else ""
                })
            return posts
            
        except json.JSONDecodeError:
            # Not JSON — print what we got to understand the format
            print(f"  Not JSON. Content-Type: {r.headers.get('Content-Type', 'unknown')}")
            print(f"  First 300 chars: {r.text[:300]}")
            return []
            
    except Exception as e:
        print(f"  Fetch error: {e}")
        return []

# ── Log helper ────────────────────────────────────────────────────────────────

def append_log(message):
    print(f"LOG: {message}")
    try:
        raw = kv_get("log")
        log = json.loads(raw) if raw else []
        log.insert(0, {"time": datetime.now(timezone.utc).isoformat(), "message": message})
        log = log[:50]
        kv_put("log", json.dumps(log))
    except Exception as e:
        print(f"  Log write error: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print(f"Craigslist checker running at {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)

    settings_raw = kv_get("settings")
    settings = json.loads(settings_raw) if settings_raw else {}
    phone = settings.get("phone_number", "")

    if not phone:
        append_log("No phone number set — skipping.")
        return

    if not is_alert_hour(settings.get("alert_hours", "always")):
        append_log("Outside alert hours — skipping.")
        return

    searches_raw = kv_get("searches")
    searches = json.loads(searches_raw) if searches_raw else []
    active = [s for s in searches if s.get("active", True)]

    if not active:
        append_log("No active searches.")
        return

    print(f"Running {len(active)} active search(es)...")

    seen_raw = kv_get("seen_posts")
    seen = set(json.loads(seen_raw)) if seen_raw else set()
    new_seen = set(seen)
    total_new = 0

    for search in active:
        name = search["name"]
        print(f"\nChecking: {name}")
        posts = fetch_posts(search)
        print(f"  Total posts: {len(posts)}")

        new_posts = [p for p in posts if p["id"] not in seen]
        print(f"  New posts: {len(new_posts)}")

        if new_posts:
            total_new += len(new_posts)
            for post in new_posts[:3]:
                price_str = f" — {post['price']}" if post["price"] else ""
                msg = f"🔔 CL Alert: {name}\n{post['title']}{price_str}\n{post['link']}"
                send_text(phone, msg)
                new_seen.add(post["id"])
            if len(new_posts) > 3:
                send_text(phone, f"...and {len(new_posts) - 3} more new listing(s) for '{name}'.")

        for post in posts:
            new_seen.add(post["id"])

    seen_list = list(new_seen)[-5000:]
    kv_put("seen_posts", json.dumps(seen_list))

    msg = (f"Found {total_new} new post(s) across {len(active)} search(es)."
           if total_new > 0
           else f"No new matching posts across {len(active)} search(es).")
    append_log(msg)
    print(f"\n{msg}")

if __name__ == "__main__":
    main()
