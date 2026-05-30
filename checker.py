"""
Craigslist Alert Checker — runs via GitHub Actions
Reads searches from Cloudflare KV, checks Craigslist, texts via Twilio
Uses HTML scraping instead of RSS to avoid 403 blocks
"""

import os, json, hashlib, requests, re, time, random
from datetime import datetime, timezone
from html.parser import HTMLParser

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

# ── Craigslist HTML scraping ──────────────────────────────────────────────────

def build_search_url(search):
    region   = search["region"]
    category = search["category"]
    keywords = search.get("keywords", [])
    query    = "+".join(k.replace(" ", "+") for k in keywords)
    url = f"https://{region}.craigslist.org/search/{category}?sort=date"
    if query:
        url += f"&query={query}"
    if search.get("min_price"):
        url += f"&min_price={search['min_price']}"
    if search.get("max_price"):
        url += f"&max_price={search['max_price']}"
    return url

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

def fetch_posts(search_url):
    try:
        time.sleep(random.uniform(1, 2))
        print(f"  Fetching: {search_url}")
        r = requests.get(search_url, headers=HEADERS, timeout=20)
        print(f"  Status: {r.status_code}")
        if r.status_code != 200:
            return []
        return parse_html(r.text, search_url)
    except Exception as e:
        print(f"  Fetch error: {e}")
        return []

def parse_html(html, base_url):
    """Extract listings from Craigslist search results page."""
    posts = []
    region_base = "/".join(base_url.split("/")[:3])  # e.g. https://sfbay.craigslist.org

    # Match listing items — Craigslist uses <li class="cl-search-result...">
    # Extract title, link, and price using regex on the HTML
    
    # Find all listing links and titles
    # Pattern matches: <a class="cl-app-anchor..." href="/url/..."><span ...>Title</span>
    link_pattern = re.compile(
        r'href="(/[^"]+/d/[^"]+\.html)"[^>]*>.*?<span[^>]*class="[^"]*label[^"]*"[^>]*>([^<]+)</span>',
        re.DOTALL
    )
    
    # Also try JSON-LD structured data which Craigslist sometimes includes
    json_pattern = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
    
    # Try extracting from JSON data embedded in page
    data_pattern = re.compile(r'"url":"(https://[^"]+craigslist[^"]+)"[^}]*"name":"([^"]+)"')
    
    # Most reliable: find all posting links
    posting_pattern = re.compile(r'href="(https?://[^"]*craigslist\.org/[^"]+/d/[^"]+\.html)"')
    title_pattern   = re.compile(r'<span[^>]+class="[^"]*posting-title[^"]*"[^>]*>.*?<span[^>]+class="[^"]*label[^"]*"[^>]*>([^<]+)</span>', re.DOTALL)
    
    # Try to find listing blocks
    # Craigslist new layout uses gallery items
    gallery_pattern = re.compile(
        r'<li[^>]+class="[^"]*cl-search-result[^"]*"[^>]*>(.*?)</li>',
        re.DOTALL
    )
    
    found_links = set()
    
    for block in gallery_pattern.findall(html):
        link_m  = re.search(r'href="(/[^"]+/d/[^"]+\.html)"', block)
        title_m = re.search(r'class="[^"]*label[^"]*"[^>]*>([^<]+)<', block)
        price_m = re.search(r'class="[^"]*price[^"]*"[^>]*>\s*(\$[\d,]+)', block)
        
        if link_m and title_m:
            link  = region_base + link_m.group(1) if link_m.group(1).startswith("/") else link_m.group(1)
            title = title_m.group(1).strip()
            price = price_m.group(1).strip() if price_m else ""
            
            if link not in found_links:
                found_links.add(link)
                post_id = hashlib.md5(link.encode()).hexdigest()
                posts.append({"id": post_id, "title": title, "link": link, "price": price})

    print(f"  Parsed {len(posts)} listings from HTML")
    
    # If we got nothing, print a snippet of the HTML to help debug
    if len(posts) == 0:
        print(f"  HTML snippet (first 500 chars): {html[:500]}")
    
    return posts

# ── Price matching ────────────────────────────────────────────────────────────

def matches_price(post, min_price, max_price):
    if not min_price and not max_price:
        return True
    if not post["price"]:
        return True
    try:
        price = int(post["price"].replace("$", "").replace(",", "").strip())
    except ValueError:
        return True
    if min_price and price < int(min_price): return False
    if max_price and price > int(max_price): return False
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
        search_url = build_search_url(search)
        posts = fetch_posts(search_url)
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
