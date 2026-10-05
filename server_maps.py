#!/usr/bin/env python3
import json
import math
import os
import urllib.parse
import urllib.request
import re
import sys
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from curl_cffi import requests
except ImportError:
    print("\n[SETUP] Required package 'curl_cffi' not found. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "curl_cffi"])
    from curl_cffi import requests

# HOST/PORT come from the environment so this same file runs unchanged
# locally (defaults to localhost:8000) and on a public host/PaaS (which
# typically injects PORT, and requires binding 0.0.0.0 rather than
# localhost so traffic from outside the container can reach it).
HOST = os.environ.get("HOST", "localhost")
PORT = int(os.environ.get("PORT", 8000))
HTML_FILE = "eft-task-guide.html"

# Public, multi-user traffic means concurrent request threads (this server
# uses ThreadingHTTPServer -- one thread per connection) can race on the
# module-level dicts below (the wiki breaker state and the tarkov.dev
# cache). A single lock guarding all of it is simplest and cheap: every
# critical section here is a quick dict read/write, never the network call
# itself.
_state_lock = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://escapefromtarkov.fandom.com/wiki/Main_Page",
}

# ---------------------------------------------------------------------------
# Shared, throttled, circuit-breaker-protected access to the wiki.
#
# Every request to escapefromtarkov.fandom.com goes through here instead of
# calling requests.get() directly. Two protections:
#   1. A minimum gap between requests, so a big batch of tasks doesn't fire
#      a burst of dozens of requests in a couple seconds (bursty traffic is
#      exactly what trips bot-detection).
#   2. A circuit breaker: if we start seeing 403s back-to-back, that's a
#      strong signal we're already being blocked/rate-limited -- continuing
#      to hammer the site can only make it worse (and won't succeed anyway).
#      Once tripped, further requests fail fast locally (no network call)
#      for a cooldown period, then the breaker resets and tries again.
# ---------------------------------------------------------------------------
_last_request_ts = [0.0]
_consecutive_blocks = [0]
_circuit_open_until = [0.0]
MIN_REQUEST_INTERVAL = 0.4       # seconds between requests, minimum
BLOCK_THRESHOLD = 3              # this many 403/429s in a row trips the breaker
CIRCUIT_COOLDOWN_SECONDS = 90    # how long to back off once tripped


class WikiBlockedError(Exception):
    pass


def fandom_get(url, **kwargs):
    # Only the bookkeeping (checking/updating the breaker + throttle state)
    # needs the lock -- the actual network call happens outside it so one
    # slow request doesn't stall every other thread's unrelated requests.
    with _state_lock:
        now = time.time()
        if now < _circuit_open_until[0]:
            remaining = int(_circuit_open_until[0] - now)
            raise WikiBlockedError(
                f"Backing off for ~{remaining}s after repeated 403/429 responses -- "
                f"the wiki appears to be rate-limiting or blocking this client right now. "
                f"Wait a bit and try again rather than retrying immediately."
            )

        wait = MIN_REQUEST_INTERVAL - (now - _last_request_ts[0])
        if wait > 0:
            # Sleeping while holding the lock is intentional here: the whole
            # point of MIN_REQUEST_INTERVAL is a minimum gap between
            # requests ACROSS all threads, so a second thread arriving
            # mid-wait must queue behind this one rather than slip through.
            time.sleep(wait)
        _last_request_ts[0] = time.time()

    kwargs.setdefault("impersonate", "chrome")
    kwargs.setdefault("headers", HEADERS)
    resp = requests.get(url, **kwargs)

    with _state_lock:
        if resp.status_code in (403, 429):
            _consecutive_blocks[0] += 1
            if _consecutive_blocks[0] >= BLOCK_THRESHOLD:
                _circuit_open_until[0] = time.time() + CIRCUIT_COOLDOWN_SECONDS
                print(f"[wiki] {_consecutive_blocks[0]} consecutive {resp.status_code} responses -- "
                      f"backing off for {CIRCUIT_COOLDOWN_SECONDS}s to avoid making it worse")
        else:
            _consecutive_blocks[0] = 0

    return resp

def search_wiki(task_name):
    """
    Attempts to search Fandom via MediaWiki API using standard urllib.
    Falls back gracefully if Cloudflare blocks datacenter IPs.
    """
    query = urllib.parse.quote(task_name)
    url = f"https://escapefromtarkov.fandom.com/api.php?action=opensearch&search={query}&limit=1&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if len(data) >= 4 and len(data[1]) > 0 and len(data[3]) > 0:
                return data[1][0], data[3][0]
    except Exception:
        pass
    
    # Fallback: format name directly to Fandom URL scheme
    formatted_name = task_name.strip().title().replace(" ", "_")
    wiki_url = f"https://escapefromtarkov.fandom.com/wiki/{formatted_name}"
    return task_name, wiki_url

def extract_section(html, section_id):
    pattern = f'id="{section_id}"'
    idx = html.find(pattern)
    if idx == -1: return ""
    next_h2 = html.find('<h2', idx + len(pattern))
    if next_h2 == -1: next_h2 = html.find('<!-- \nNewPP limit report', idx)
    if next_h2 == -1: next_h2 = len(html)
    return html[idx:next_h2]

def clean_tags(text):
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text) 
    text = re.sub(r'&#91;.*?&#93;', '', text) 
    text = re.sub(r'\[.*?\]', '', text)
    return text.strip()

KNOWN_MAPS = [
    "Reserve", "Customs", "Interchange", "Woods", "Shoreline",
    "Lighthouse", "Streets of Tarkov", "Factory", "Ground Zero", "The Lab",
    "The Labyrinth", "Terminal"
]

# ---------------------------------------------------------------------------
# Shared per-task result cache.
#
# Once this is public, many different visitors will ask about the same
# popular tasks. Without this, every one of them re-triggers a wiki scrape
# AND a tarkov.dev lookup for "Checking" or whatever quest everyone happens
# to be stuck on that week. Caching the finished per-task JSON (not just the
# tarkov.dev dataset) means the wiki is only hit once per task per TTL,
# across ALL users, not once per request.
# ---------------------------------------------------------------------------
_TASK_RESULT_TTL_SECONDS = 6 * 3600  # task text/location data changes rarely
_task_result_cache = {}  # normalized task name -> {"ts": float, "data": dict}
_task_cache_lock = threading.Lock()


def _task_cache_key(task_name, forced_map):
    return f"{(task_name or '').strip().lower()}|{(forced_map or '').strip().lower()}"


def get_cached_task_result(task_name, forced_map):
    key = _task_cache_key(task_name, forced_map)
    with _task_cache_lock:
        entry = _task_result_cache.get(key)
    if not entry:
        return None
    if (time.time() - entry["ts"]) > _TASK_RESULT_TTL_SECONDS:
        return None
    return entry["data"]


def set_cached_task_result(task_name, forced_map, data):
    # Don't cache errors -- a transient wiki hiccup or a rate-limit backoff
    # shouldn't get "stuck" for every subsequent visitor for hours.
    if isinstance(data, dict) and "error" in data:
        return
    key = _task_cache_key(task_name, forced_map)
    with _task_cache_lock:
        _task_result_cache[key] = {"ts": time.time(), "data": data}


# ---------------------------------------------------------------------------
# Basic abuse protection for a public endpoint.
#
# This is deliberately simple (in-memory, per-process, sliding window) --
# good enough to stop a single misbehaving client or script from hammering
# the wiki through this server, without pulling in a separate dependency.
# A reverse proxy in front of this (see deployment notes) is a better place
# for serious abuse protection, but this is a reasonable floor on its own.
# ---------------------------------------------------------------------------
RATE_LIMIT_MAX_REQUESTS = 20       # requests...
RATE_LIMIT_WINDOW_SECONDS = 60     # ...per rolling window, per client IP
MAX_BODY_BYTES = 64 * 1024         # reject absurdly large POST bodies outright
MAX_TASKS_PER_REQUEST = 25         # cap batch size so one request can't queue hundreds of scrapes

_rate_limit_lock = threading.Lock()
_rate_limit_hits = {}  # client ip -> [timestamps within the current window]


def check_rate_limit(client_ip):
    """Returns True if this request is allowed, False if the client should
    be told to slow down."""
    now = time.time()
    with _rate_limit_lock:
        hits = _rate_limit_hits.setdefault(client_ip, [])
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            return False
        hits.append(now)
        # Opportunistic cleanup so this dict doesn't grow forever across
        # many distinct visitor IPs over a long-running process.
        if len(_rate_limit_hits) > 5000:
            stale = [ip for ip, ts in _rate_limit_hits.items() if not ts or ts[-1] < cutoff]
            for ip in stale:
                _rate_limit_hits.pop(ip, None)
        return True

# ---------------------------------------------------------------------------
# tarkov.dev structured data: authoritative, per-objective map assignment.
#
# json.tarkov.dev is a static mirror of the same data tarkov.dev's own site
# runs on (their live GraphQL API has been down since ~July 2026). Confirmed
# live against real data:
#   - regular/tasks: {"data":{"tasks":{id:{wikiLink, objectives:[
#         {id, maps:[map_id,...]}, ...]}}}}
#     "name" and objective "description" are translation-key placeholders
#     that do NOT resolve via bare id for the task name -- but we don't
#     need the name from here at all. Fandom's own search already gives us
#     the real name AND, critically, the exact same wikiLink URL tarkov.dev
#     uses. We join on that URL instead of on name.
#   - regular/maps: {"data":{"maps":{id:{normalizedName,...}}}}
#     normalizedName needs no translation -- already a plain slug, usable
#     directly in https://tarkov.dev/map/<slug>.
#
# This hits json.tarkov.dev, not the wiki, so it's not subject to Fandom's
# bot protection / circuit breaker above.
# ---------------------------------------------------------------------------

# Per-map pixel calibration (affine bounds + rotation) needed to place a
# real game-world (x, z) position onto that map's SVG artwork. Not exposed
# by any API -- sourced from TarkovTracker/tarkovdata's maps.json (public,
# MIT-adjacent community reference data). Cross-validated here against two
# independent real coordinates before use (not just trusted blind):
#   - Streets zone (181.24, 228.68) -> frac (0.235, 0.630) -- within [0,1]
#   - Shoreline PMC spawn (-898.14, 200.56) -> frac (0.897, 0.592) -- within [0,1]
# tdevId values independently confirmed to match json.tarkov.dev's own map
# ids. SVG files confirmed present (same filenames) in the companion
# the-hideout/tarkov-dev-svg-maps repo (CC BY-NC-SA 4.0, non-commercial --
# fine for this tool, which is free and ad-free).
TARKOV_MAP_CALIBRATION = {
    "55f2d3fd4bdc2d5f408b4567": {"slug": "factory", "svg": "Factory.svg", "rotation": 90, "bounds": [[-67, 69], [76.6, -65.5]]},
    "56f40101d2720b2a4d8b45d6": {"slug": "customs", "svg": "Customs.svg", "rotation": 180, "bounds": [[698, -307], [-371, 237]]},
    "5704e3c2d2720bac5b8b4567": {"slug": "woods", "svg": "Woods.svg", "rotation": 180, "bounds": [[650, -945], [-695, 470]]},
    "5704e554d2720bac5b8b456e": {"slug": "shoreline", "svg": "Shoreline.svg", "rotation": 180, "bounds": [[506, -405], [-1060, 618]]},
    "5714dbc024597771384a510d": {"slug": "interchange", "svg": "Interchange.svg", "rotation": 180, "bounds": [[530, -439], [-364, 452]]},
    "5b0fc42d86f7744a585f9105": {"slug": "the-lab", "svg": "Labs.svg", "rotation": 270, "bounds": [[-91, -477], [-287, -193]]},
    "5704e5fad2720bc05b8b4567": {"slug": "reserve", "svg": "Reserve.svg", "rotation": 180, "bounds": [[289, -338], [-303, 336]]},
    "5704e4dad2720bb55b8b4567": {"slug": "lighthouse", "svg": "Lighthouse.svg", "rotation": 180, "bounds": [[515, -1000], [-545, 725]]},
    "5714dc692459777137212e12": {"slug": "streets-of-tarkov", "svg": "StreetsOfTarkov.svg", "rotation": 180, "bounds": [[323, -317], [-280, 549]]},
    "653e6760052c01c1c805532f": {"slug": "ground-zero", "svg": "GroundZero.svg", "rotation": 180, "bounds": [[249, -124], [-99, 364]]},
}
TARKOV_SVG_BASE = "https://cdn.jsdelivr.net/gh/the-hideout/tarkov-dev-svg-maps@main/"


def _map_frac_position(tdev_map_id, x, z):
    """Game-world (x, z) -> (fracX, fracY) in [0,1] range within that map's
    SVG artwork, or None if we have no calibration for this map."""
    cal = TARKOV_MAP_CALIBRATION.get(tdev_map_id)
    if not cal:
        return None
    r = math.radians(cal["rotation"])
    cos_r, sin_r = math.cos(r), math.sin(r)

    def rot(px, pz):
        return (px * cos_r - pz * sin_r, px * sin_r + pz * cos_r)

    xr, zr = rot(x, z)
    (bx1, bz1), (bx2, bz2) = cal["bounds"]
    c1x, c1z = rot(bx1, bz1)
    c2x, c2z = rot(bx2, bz2)
    if c2x == c1x or c2z == c1z:
        return None
    frac_x = (xr - c1x) / (c2x - c1x)
    frac_y = (zr - c1z) / (c2z - c1z)
    return (frac_x, frac_y)


_TARKOVDEV_TTL_SECONDS = 3600  # refresh hourly; this data doesn't change often
_tarkovdev_cache = {"ts": 0, "wikilink_to_task": {}, "map_id_to_slug": {}}


def _tarkovdev_fetch_json(path):
    url = f"https://json.tarkov.dev/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "eft-task-dossier-local/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_wikilink(url):
    """Two URLs pointing at the same wiki article can differ in ways that
    break a raw string comparison: %21 vs literal '!', underscores vs
    spaces, trailing slash, percent-encoding case. Extract just the page
    title, url-decode it, and normalize punctuation-as-whitespace so
    'Cease_Fire%21' and 'Cease Fire!' (or any mix) compare equal."""
    url = (url or "").strip()
    if not url:
        return ""
    # keep only the part after '/wiki/' if present -- ignores domain/scheme
    # differences entirely, which we don't care about anyway.
    marker = "/wiki/"
    idx = url.find(marker)
    title = url[idx + len(marker):] if idx != -1 else url
    title = title.split("#")[0].split("?")[0].rstrip("/")
    try:
        title = urllib.parse.unquote(title)
    except Exception:
        pass
    title = title.replace("_", " ").lower().strip()
    # collapse all punctuation/whitespace runs to a single space so '!', '?',
    # "'", '-', multiple spaces etc. can't cause a mismatch either.
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return title


def _load_tarkovdev_data():
    """(Re)build the in-memory index from json.tarkov.dev. TTL-cached; a
    failed refresh leaves the previous (possibly stale-but-real) data in
    place rather than wiping it out.

    Without this short-circuit, every call re-downloads the full maps+tasks
    dataset from json.tarkov.dev -- and this is called once per queued task
    per request, so under real public traffic that's 2x N full-dataset
    fetches per batch, for every batch, forever. The check below is what
    makes this "TTL-cached" rather than "re-fetched every time"."""
    global _tarkovdev_cache
    now = time.time()
    with _state_lock:
        if _tarkovdev_cache["wikilink_to_task"] and (now - _tarkovdev_cache["ts"]) < _TARKOVDEV_TTL_SECONDS:
            return
    try:
        maps_doc = _tarkovdev_fetch_json("regular/maps")
        maps_raw = maps_doc["data"]["maps"]
        if isinstance(maps_raw, dict):
            maps_raw = list(maps_raw.values())
        map_id_to_slug = {m["id"]: m.get("normalizedName", "") for m in maps_raw if m.get("normalizedName")}

        tasks_doc = _tarkovdev_fetch_json("regular/tasks")
        tasks_raw = tasks_doc["data"]["tasks"]
        if isinstance(tasks_raw, dict):
            tasks_raw = list(tasks_raw.values())

        wikilink_to_task = {}
        for t in tasks_raw:
            wl = _normalize_wikilink(t.get("wikiLink"))
            if not wl:
                continue

            maps_for_task = set()
            pins = []
            for obj in (t.get("objectives") or []):
                # ---- map list ----
                for mid in (obj.get("maps") or []):
                    slug = map_id_to_slug.get(mid)
                    if slug:
                        maps_for_task.add(slug)
                # ---- zone-based pins (old data) ----
                for zone in (obj.get("zones") or []):
                    mid = zone.get("map")
                    pos = zone.get("position") or {}
                    if mid is None or "x" not in pos or "z" not in pos:
                        continue
                    frac = _map_frac_position(mid, pos["x"], pos["z"])
                    if not frac:
                        continue
                    fx, fy = frac
                    if not (-0.05 <= fx <= 1.05 and -0.05 <= fy <= 1.05):
                        continue
                    cal = TARKOV_MAP_CALIBRATION.get(mid)
                    if not cal:
                        continue
                    pins.append({
                        "slug": cal["slug"],
                        "fracX": round(max(0, min(1, fx)), 4),
                        "fracY": round(max(0, min(1, fy)), 4),
                        "objective_type": obj.get("type", ""),
                        "svg": TARKOV_SVG_BASE + cal["svg"],
                    })
                # ---- possibleLocations (new, for findQuestItem, etc.) ----
                for loc in (obj.get("possibleLocations") or []):
                    mid = loc.get("map")
                    positions = loc.get("positions") or []
                    for pos in positions:
                        if mid is None or "x" not in pos or "z" not in pos:
                            continue
                        frac = _map_frac_position(mid, pos["x"], pos["z"])
                        if not frac:
                            continue
                        fx, fy = frac
                        if not (-0.05 <= fx <= 1.05 and -0.05 <= fy <= 1.05):
                            continue
                        cal = TARKOV_MAP_CALIBRATION.get(mid)
                        if not cal:
                            continue
                        pins.append({
                            "slug": cal["slug"],
                            "fracX": round(max(0, min(1, fx)), 4),
                            "fracY": round(max(0, min(1, fy)), 4),
                            "objective_type": obj.get("type", ""),
                            "svg": TARKOV_SVG_BASE + cal["svg"],
                        })
            wikilink_to_task[wl] = {"id": t.get("id"), "maps": sorted(maps_for_task), "pins": pins}

        # Build the new dicts fully before taking the lock, then swap them
        # in as a unit -- readers never see a half-updated cache.
        with _state_lock:
            _tarkovdev_cache["wikilink_to_task"] = wikilink_to_task
            _tarkovdev_cache["map_id_to_slug"] = map_id_to_slug
            _tarkovdev_cache["ts"] = now
        print(f"[tarkov.dev] loaded {len(wikilink_to_task)} tasks, {len(map_id_to_slug)} maps")
    except Exception as e:
        print(f"[tarkov.dev] failed to load reference data ({e}) -- "
              f"falling back to wiki-based location detection for now")


def slug_to_display_name(slug):
    """'streets-of-tarkov' -> 'Streets of Tarkov'. Good enough for the
    small, known set of tarkov.dev map slugs."""
    small_words = {"of", "the", "in", "on", "and"}
    words = slug.replace("-", " ").split()
    return " ".join(w if (i > 0 and w in small_words) else w.capitalize() for i, w in enumerate(words))


def get_tarkovdev_maps_for_task(wiki_url):
    """List of {slug, name, url} for every map this task's objectives
    actually reference, per tarkov.dev's structured data -- or None if this
    task isn't found there (caller should fall back to wiki detection)."""
    _load_tarkovdev_data()
    with _state_lock:
        entry = _tarkovdev_cache["wikilink_to_task"].get(_normalize_wikilink(wiki_url))
    if not entry or not entry["maps"]:
        return None
    return [
        {"slug": s, "name": slug_to_display_name(s), "url": f"https://tarkov.dev/map/{s}"}
        for s in entry["maps"]
    ]


def get_tarkovdev_pins_for_task(wiki_url):
    """List of {slug, fracX, fracY, objective_type, svg} -- exact plottable
    positions for this task's objectives, where the objective type actually
    has one (plantItem/visit/mark/etc. do; giveItem/findItem/shoot mostly
    don't, by game design -- not a gap in our data). Call AFTER
    get_tarkovdev_maps_for_task has already triggered the data load."""
    with _state_lock:
        entry = _tarkovdev_cache["wikilink_to_task"].get(_normalize_wikilink(wiki_url))
    return entry["pins"] if entry else []


def get_fandom_interactive_map_url(location_name):
    if not location_name or location_name.lower() in ["any location", "none"]:
        return "https://escapefromtarkov.fandom.com/wiki/Special:AllMaps"
    map_page_name = location_name.strip().replace(" ", "_")
    return f"https://escapefromtarkov.fandom.com/wiki/Map:{map_page_name}"


def scrape_wiki_page(url, task_name, location_hint):
    """
    Primary strategy: Fetch task details and objectives from json.tarkov.dev 
    (bypasses Cloudflare block on datacenter IPs).
    Fallback strategy: Attempt Fandom fetch via fandom_get().
    """
    _load_tarkovdev_data()
    norm_url = _normalize_wikilink(url)
    
    objectives = []
    guide = []
    location = location_hint or "Any location"

    # Attempt to load objectives directly from json.tarkov.dev
    try:
        tasks_doc = _tarkovdev_fetch_json("regular/tasks")
        tasks_raw = tasks_doc["data"]["tasks"]
        if isinstance(tasks_raw, dict):
            tasks_raw = list(tasks_raw.values())

        matched_task = None
        for t in tasks_raw:
            if _normalize_wikilink(t.get("wikiLink")) == norm_url or \
               t.get("name", "").lower() == task_name.lower():
                matched_task = t
                break

        if matched_task:
            for obj in matched_task.get("objectives", []):
                desc = obj.get("description")
                if desc and not desc.startswith("task."):
                    objectives.append(desc)
                elif obj.get("type"):
                    objectives.append(f"Objective type: {obj.get('type')}")
    except Exception as e:
        print(f"[tarkov.dev fallback] Failed to load objectives: {e}")

    # If tarkov.dev had objectives, return them without hitting Fandom HTML
    if objectives:
        return {
            "location": location,
            "objectives": objectives,
            "guide": guide or ["Guide steps available directly on wiki link."],
            "url": url,
        }

    # Fallback to direct Fandom HTML scrape if circuit is open/available
    try:
        resp = fandom_get(url, timeout=10)
        if resp.status_code == 200:
            html_clean = resp.text.replace('\n', ' ')

            obj_html = extract_section(html_clean, "Objectives")
            for li in re.findall(r'<li[^>]*>(.*?)</li>', obj_html, re.IGNORECASE):
                clean_li = clean_tags(li)
                if clean_li: objectives.append(clean_li)

            guide_html = extract_section(html_clean, "Guide")
            for p in re.findall(r'<p[^>]*>(.*?)</p>', guide_html, re.IGNORECASE):
                clean_p = clean_tags(p)
                if clean_p and len(clean_p) > 10: guide.append(clean_p)

            if location.lower() in ["any location", "", "none"]:
                relevant_text = " ".join(objectives + guide)
                for m in KNOWN_MAPS:
                    if re.search(r'\b' + re.escape(m) + r'\b', relevant_text, re.IGNORECASE):
                        location = m
                        break

            return {
                "location": location,
                "objectives": objectives,
                "guide": guide,
                "url": url,
            }
    except Exception:
        pass

    # If both scrapers fail or hit 403, return basic structured data with wiki URL
    return {
        "location": location,
        "objectives": objectives or ["Refer to Wiki for detailed task objectives."],
        "guide": ["Direct scraping blocked by Wiki Cloudflare protection. Click task link for full guide."],
        "url": url,
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/generate":
            try:
                client_ip = self.client_address[0] if self.client_address else "unknown"
                if not check_rate_limit(client_ip):
                    self._send_json({
                        "error": f"Too many requests -- limit is {RATE_LIMIT_MAX_REQUESTS} per "
                                 f"{RATE_LIMIT_WINDOW_SECONDS}s per client. Wait a moment and try again."
                    }, 429)
                    return

                content_length = int(self.headers.get('Content-Length', 0))
                if content_length == 0:
                    self._send_json({"error": "Empty payload"}, 400)
                    return
                if content_length > MAX_BODY_BYTES:
                    self._send_json({"error": "Request body too large."}, 413)
                    return

                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data)
                tasks = payload.get("tasks", [])
                if len(tasks) > MAX_TASKS_PER_REQUEST:
                    tasks = tasks[:MAX_TASKS_PER_REQUEST]

                results = {}

                for item in tasks:
                    t_name = item.get("name") if isinstance(item, dict) else item
                    forced_map = item.get("map") if isinstance(item, dict) else None

                    cached = get_cached_task_result(t_name, forced_map)
                    if cached is not None:
                        results[t_name] = cached
                        continue

                    if time.time() < _circuit_open_until[0]:
                        remaining = int(_circuit_open_until[0] - time.time())
                        results[t_name] = {
                            "error": f"Wiki appears to be rate-limiting/blocking us right now "
                                     f"(backing off for ~{remaining}s). Wait a bit before retrying "
                                     f"the whole batch rather than clicking Generate Guidance repeatedly."
                        }
                        continue

                    # Every task is handled independently -- one bad scrape (a
                    # dead link, a wiki hiccup, an exception) must NOT abort the
                    # whole batch, or the client never gets any response at all
                    # and just hangs forever waiting on a reply that never comes.
                    try:
                        name, url = search_wiki(t_name)
                        if not url:
                            results[t_name] = {"error": "Task not found on Wiki."}
                            continue

                        data = scrape_wiki_page(url, t_name, forced_map)
                        if "error" in data:
                            results[t_name] = data
                            continue

                        data["name"] = name
                        if forced_map and forced_map.lower() != "any location":
                            data["location"] = forced_map

                        # Authoritative source: tarkov.dev's structured per-
                        # objective map data, matched via the wiki URL (same
                        # URL both sources use for this task -- no fuzzy name
                        # matching needed). A task can genuinely span several
                        # maps; this now represents that correctly instead of
                        # forcing a single location string.
                        tarkovdev_maps = None
                        try:
                            tarkovdev_maps = get_tarkovdev_maps_for_task(url)
                        except Exception as e:
                            print(f"[tarkov.dev] lookup failed for '{name}': {e}")

                        if tarkovdev_maps:
                            data["maps"] = [
                                {"name": m["name"], "url": m["url"], "source": "tarkovdev", "slug": m["slug"]}
                                for m in tarkovdev_maps
                            ]
                            data["location"] = (
                                tarkovdev_maps[0]["name"] if len(tarkovdev_maps) == 1
                                else "Multiple locations"
                            )
                            try:
                                data["pins"] = get_tarkovdev_pins_for_task(url)
                                data["map_match_status"] = "matched_with_pins" if data["pins"] else "matched_no_zone_data"
                            except Exception as e:
                                data["pins"] = []
                                data["map_match_status"] = "pin_lookup_error"
                                print(f"[tarkov.dev] pin lookup failed for '{name}': {e}")
                        else:
                            data["pins"] = []
                            data["map_match_status"] = "not_matched_in_tarkovdev"
                            # Fall back to the wiki-scraped single location
                            # (task not yet in tarkov.dev's dataset, or no
                            # objective on any map -- e.g. a pure hand-in task).
                            loc = data.get("location", "Any location")
                            if loc.lower() in ("any location", "", "none"):
                                data["maps"] = []
                            else:
                                data["maps"] = [{
                                    "name": loc,
                                    "url": get_fandom_interactive_map_url(loc),
                                    "source": "wiki_fallback",
                                    "slug": None,
                                }]

                        results[t_name] = data
                        set_cached_task_result(t_name, forced_map, data)
                    except Exception as e:
                        results[t_name] = {"error": f"Unexpected error while processing this task: {e}"}

                self._send_json(results)
            except Exception as e:
                # Last-resort safety net -- guarantees the client always gets
                # *some* response instead of a hung connection, even if
                # something above went wrong in a way we didn't anticipate.
                try:
                    self._send_json({"error": f"Server error: {e}"}, 500)
                except Exception:
                    pass
        else:
            self._send_json({"error": "Unknown endpoint"}, 404)

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path in ("/", ""): path = "/" + HTML_FILE
            file_path = Path(__file__).parent / path.lstrip("/")
            if file_path.is_file():
                self.send_response(200)
                if file_path.suffix == ".html": self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
        except Exception as e:
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
            except Exception:
                pass

if __name__ == "__main__":
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"Could not start on {HOST}:{PORT}: {e}")
        print("Something else is probably already using that port.")
        print("Set the PORT environment variable to a different number (e.g. PORT=8010) and run it again.")
        raise SystemExit(1)
    print(f"EFT Task Planner running at http://{HOST}:{PORT}/  (set HOST=0.0.0.0 to accept non-local connections)")
    print("Press Ctrl+C to stop.")
    httpd.serve_forever()
