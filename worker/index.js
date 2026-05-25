/**
 * Craigslist Alert Monitor — Cloudflare Worker
 * Runs on a schedule, checks Craigslist RSS feeds, texts you via Twilio
 */

export default {

  // ── Scheduled trigger (runs automatically on your cron schedule) ──
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runChecks(env));
  },

  // ── HTTP trigger (lets the UI talk to this worker) ──
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Allow requests from your UI
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Auth-Key",
      "Content-Type": "application/json",
    };

    if (request.method === "OPTIONS") return new Response(null, { headers });

    // Simple API key auth — set UI_PASSWORD in your Worker secrets
    const authKey = request.headers.get("X-Auth-Key");
    if (authKey !== env.UI_PASSWORD) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), { status: 401, headers });
    }

    const path = url.pathname;

    // GET /searches — return all saved searches
    if (request.method === "GET" && path === "/searches") {
      const raw = await env.CL_KV.get("searches");
      const searches = raw ? JSON.parse(raw) : [];
      return new Response(JSON.stringify(searches), { headers });
    }

    // GET /settings — return phone/interval/hours settings
    if (request.method === "GET" && path === "/settings") {
      const raw = await env.CL_KV.get("settings");
      const settings = raw ? JSON.parse(raw) : {
        phone_number: "",
        alert_hours: "day",
      };
      return new Response(JSON.stringify(settings), { headers });
    }

    // POST /searches — save a new search
    if (request.method === "POST" && path === "/searches") {
      const body = await request.json();
      const raw = await env.CL_KV.get("searches");
      const searches = raw ? JSON.parse(raw) : [];
      const newSearch = { ...body, id: Date.now(), active: true };
      searches.push(newSearch);
      await env.CL_KV.put("searches", JSON.stringify(searches));
      return new Response(JSON.stringify({ ok: true, search: newSearch }), { headers });
    }

    // POST /settings — save phone/alert settings
    if (request.method === "POST" && path === "/settings") {
      const body = await request.json();
      await env.CL_KV.put("settings", JSON.stringify(body));
      return new Response(JSON.stringify({ ok: true }), { headers });
    }

    // DELETE /searches/:id — remove a search
    if (request.method === "DELETE" && path.startsWith("/searches/")) {
      const id = parseInt(path.split("/")[2]);
      const raw = await env.CL_KV.get("searches");
      let searches = raw ? JSON.parse(raw) : [];
      searches = searches.filter(s => s.id !== id);
      await env.CL_KV.put("searches", JSON.stringify(searches));
      return new Response(JSON.stringify({ ok: true }), { headers });
    }

    // POST /searches/:id/toggle — pause or resume a search
    if (request.method === "POST" && path.match(/\/searches\/\d+\/toggle/)) {
      const id = parseInt(path.split("/")[2]);
      const raw = await env.CL_KV.get("searches");
      let searches = raw ? JSON.parse(raw) : [];
      searches = searches.map(s => s.id === id ? { ...s, active: !s.active } : s);
      await env.CL_KV.put("searches", JSON.stringify(searches));
      return new Response(JSON.stringify({ ok: true }), { headers });
    }

    // POST /run — manually trigger a check right now
    if (request.method === "POST" && path === "/run") {
      ctx.waitUntil(runChecks(env));
      return new Response(JSON.stringify({ ok: true, message: "Check started" }), { headers });
    }

    // GET /log — return recent activity log
    if (request.method === "GET" && path === "/log") {
      const raw = await env.CL_KV.get("log");
      const log = raw ? JSON.parse(raw) : [];
      return new Response(JSON.stringify(log), { headers });
    }

    return new Response(JSON.stringify({ error: "Not found" }), { status: 404, headers });
  }
};

// ── Core check logic ──────────────────────────────────────────────────────────

async function runChecks(env) {
  const settingsRaw = await env.CL_KV.get("settings");
  const settings = settingsRaw ? JSON.parse(settingsRaw) : {};
  const phone = settings.phone_number;

  if (!phone) {
    await appendLog(env, "No phone number set — skipping check.");
    return;
  }

  if (!isAlertHour(settings.alert_hours)) {
    await appendLog(env, "Outside alert hours — skipping check.");
    return;
  }

  const searchesRaw = await env.CL_KV.get("searches");
  const searches = searchesRaw ? JSON.parse(searchesRaw) : [];
  const active = searches.filter(s => s.active);

  if (active.length === 0) {
    await appendLog(env, "No active searches.");
    return;
  }

  const seenRaw = await env.CL_KV.get("seen_posts");
  const seen = new Set(seenRaw ? JSON.parse(seenRaw) : []);
  const newSeen = new Set(seen);

  let totalNew = 0;

  for (const search of active) {
    const rssUrl = buildRssUrl(search);
    const posts = await fetchPosts(rssUrl);
    const newPosts = posts.filter(p => !seen.has(p.id));

    if (newPosts.length > 0) {
      totalNew += newPosts.length;
      // Send up to 3 texts per search
      for (const post of newPosts.slice(0, 3)) {
        const priceStr = post.price ? ` — ${post.price}` : "";
        const msg = `🔔 CL Alert: ${search.name}\n${post.title}${priceStr}\n${post.link}`;
        await sendText(env, phone, msg);
        newSeen.add(post.id);
      }
      if (newPosts.length > 3) {
        await sendText(env, phone, `...and ${newPosts.length - 3} more new post(s) for "${search.name}".`);
      }
    }

    // Mark all current posts seen even if not new
    posts.forEach(p => newSeen.add(p.id));
  }

  // Keep seen list from growing forever — keep last 5000
  const seenArray = [...newSeen].slice(-5000);
  await env.CL_KV.put("seen_posts", JSON.stringify(seenArray));

  const msg = totalNew > 0
    ? `Found ${totalNew} new post(s) across ${active.length} search(es).`
    : `No new posts across ${active.length} search(es).`;
  await appendLog(env, msg);
}

function buildRssUrl(search) {
  const keywords = encodeURIComponent(search.keywords.join(" "));
  let url = `https://${search.region}.craigslist.org/search/${search.category}?format=rss&query=${keywords}`;
  if (search.min_price) url += `&min_price=${search.min_price}`;
  if (search.max_price) url += `&max_price=${search.max_price}`;
  return url;
}

async function fetchPosts(rssUrl) {
  try {
    const resp = await fetch(rssUrl, {
      headers: { "User-Agent": "Mozilla/5.0" },
      cf: { cacheTtl: 60 }
    });
    if (!resp.ok) return [];
    const text = await resp.text();
    return parseRss(text);
  } catch (e) {
    return [];
  }
}

function parseRss(xml) {
  const posts = [];
  const items = xml.match(/<item>([\s\S]*?)<\/item>/g) || [];
  for (const item of items) {
    const title = (item.match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/) ||
                   item.match(/<title>(.*?)<\/title>/) || [])[1] || "";
    const link  = (item.match(/<link>(.*?)<\/link>/) || [])[1] || "";
    const price = (item.match(/<cl:price>(.*?)<\/cl:price>/) || [])[1] || "";
    if (!link) continue;
    // Use a hash of the URL as a stable ID
    const id = btoa(link).replace(/[^a-zA-Z0-9]/g, "").slice(0, 32);
    posts.push({ id, title: title.trim(), link: link.trim(), price: price.trim() });
  }
  return posts;
}

async function sendText(env, to, message) {
  const sid   = env.TWILIO_ACCOUNT_SID;
  const token = env.TWILIO_AUTH_TOKEN;
  const from  = env.TWILIO_FROM_NUMBER;
  if (!sid || !token || !from) return;

  const body = new URLSearchParams({ To: to, From: from, Body: message });
  await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
    method: "POST",
    headers: {
      "Authorization": "Basic " + btoa(`${sid}:${token}`),
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: body.toString(),
  });
}

function isAlertHour(setting) {
  if (!setting || setting === "always") return true;
  const hour = new Date().getUTCHours() - 7; // Adjust to PT — change offset for your timezone
  const h = ((hour % 24) + 24) % 24;
  if (setting === "day")      return h >= 8  && h < 22;
  if (setting === "business") return h >= 9  && h < 17;
  return true;
}

async function appendLog(env, message) {
  const raw = await env.CL_KV.get("log");
  const log = raw ? JSON.parse(raw) : [];
  log.unshift({ time: new Date().toISOString(), message });
  if (log.length > 50) log.length = 50; // keep last 50 entries
  await env.CL_KV.put("log", JSON.stringify(log));
}
