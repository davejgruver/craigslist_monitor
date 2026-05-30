"""
Craigslist Alert Checker — runs via GitHub Actions
Reads searches from Cloudflare KV, checks Craigslist, texts via Twilio
"""

import os, json, hashlib, requests
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# ── Cloudflare KV helpers ─────────────────────────────────────────────────────

CF_ACCOUNT_ID      = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN       = os.environ["CF_API_TOKEN"]

KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

def kv_get(key):
    r = requests.get(f"{KV_BASE}/values/{key}", headers=KV_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text

def kv_put(key, value):
    r = requests.put(
        f"{KV_BASE}/values/{key}",
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
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
    print(f"  Text sent: {r.status_code}")

# ── Alert hours ───────────────────────────────────────────────────────────────

def is_alert_hour(setting):
    if not setting or setting == "always":
        return True
    # Pacific Time (UTC-7 in summer, UTC-8 in winter — using UTC-7)
    hour = (datetime.now(timezone.utc).hour - 7) % 24
    if setting == "day":
        return 8 <= hour < 22
    if setting == "business":
        return 9 <= hour < 17
    return True

# ── RSS fetching ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

def build_rss_url(search):
    region   = search["region"]
    category = search["category"]
    url = f"https://{region}.craigslist.org/search/{category}?format=rss&sort=date"
    return url

def fetch_posts(rss_url):
    try:
        # Try without the format=rss parameter
        clean_url = rss_url.replace("?format=rss&", "?").replace("?format=rss", "")
        rss_url_v2 = clean_url + ("&" if "?" in clean_url else "?") + "format=rss"
        
        print(f"  Fetching: {rss_url}")
        import time, random
        time.sleep(random.uniform(1, 3))
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": f"https://{rss_url.split('/')[2]}/",
        }
        r = requests.get(rss_url, headers=headers, timeout=15)
        print(f"  Status: {r.status_code}")
        if r.status_code != 200:
            return []
        return parse_rss(r.text)
    except Exception as e:
        print(f"  Fetch error: {e}")
        return []

def parse_rss(xml_text):
    posts = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        return []

    ns = {"cl": "http://www.craigslist.org/about/namespace"}
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        price_el = item.find("cl:price", ns)

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link  = link_el.text.strip()  if link_el  is not None and link_el.text  else ""
        price = price_el.text.strip() if price_el is not None and price_el.text else ""

        if not link:
            continue

        post_id = hashlib.md5(link.encode()).hexdigest()
        posts.append({"id": post_id, "title": title, "link": link, "price": price})

    return posts

# ── Keyword and price matching ────────────────────────────────────────────────

def matches_keywords(post, keywords):
    if not keywords:
        return True
    text = post["title"].lower()
    return any(kw.lower() in text for kw in keywords)

def matches_price(post, min_price, max_price):
    if not min_price and not max_price:
        return True
    if not post["price"]:
        return True  # include posts with no price
    try:
        price = int(post["price"].replace("$", "").replace(",", "").strip())
    except ValueError:
        return True
    if min_price and price < int(min_price):
        return False
    if max_price and price > int(max_price):
        return False
    return True

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

    # Load settings
    settings_raw = kv_get("settings")
    settings = json.loads(settings_raw) if settings_raw else {}
    phone = settings.get("phone_number", "")

    if not phone:
        append_log("No phone number set — skipping.")
        return

    if not is_alert_hour(settings.get("alert_hours", "always")):
        append_log("Outside alert hours — skipping.")
        return

    # Load searches
    searches_raw = kv_get("searches")
    searches = json.loads(searches_raw) if searches_raw else []
    active = [s for s in searches if s.get("active", True)]

    if not active:
        append_log("No active searches.")
        return

    print(f"Running {len(active)} active search(es)...")

    # Load seen posts
    seen_raw = kv_get("seen_posts")
    seen = set(json.loads(seen_raw)) if seen_raw else set()
    new_seen = set(seen)
    total_new = 0

    for search in active:
        name = search["name"]
        print(f"\nChecking: {name}")
        rss_url = build_rss_url(search)
        posts = fetch_posts(rss_url)
        print(f"  Posts in feed: {len(posts)}")

        matched = [p for p in posts if matches_keywords(p, search.get("keywords", []))]
        print(f"  Keyword matched: {len(matched)}")

        filtered = [p for p in matched if matches_price(p, search.get("min_price"), search.get("max_price"))]
        print(f"  Price filtered: {len(filtered)}")

        new_posts = [p for p in filtered if p["id"] not in seen]
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

        # Mark all fetched posts as seen
        for post in posts:
            new_seen.add(post["id"])

    # Save seen posts (keep last 5000)
    seen_list = list(new_seen)[-5000:]
    kv_put("seen_posts", json.dumps(seen_list))

    msg = (f"Found {total_new} new post(s) across {len(active)} search(es)."
           if total_new > 0
           else f"No new matching posts across {len(active)} search(es).")
    append_log(msg)
    print(f"\n{msg}")

if __name__ == "__main__":
    main()
