# EFT Task Planner

A small, self-contained web app that looks up Escape From Tarkov task/quest
names and shows, on one page: the task's objectives, a tactical guide, and
the exact in-raid map location(s) it needs — plotted as numbered pins on the
actual map image, with a suggested raid order when several tasks are queued
at once.

Data comes from two live sources, fetched server-side on each lookup:
- the [Escape From Tarkov Fandom wiki](https://escapefromtarkov.fandom.com/wiki/Escape_from_Tarkov_Wiki)
  — objectives, guide text, and a location fallback
- [tarkov.dev](https://tarkov.dev)'s public dataset (via `json.tarkov.dev`)
  — authoritative per-objective map assignment and exact pin coordinates

No database, no build step, no framework — one Python file serving the API
and the static page.

## Running it locally

```bash
python3 server_maps.py
```

Then open **http://localhost:8000/**. The first run installs `curl_cffi`
automatically if it isn't already present.

Enter task names (or use "📷 Auto-Read" to OCR them from a screenshot of
your in-game quest log), queue a few, and click **Generate guidance**.

## Configuration

Both read from environment variables, with sane local defaults:

| Variable | Default     | Purpose                                              |
|----------|-------------|-------------------------------------------------------|
| `HOST`   | `localhost` | Set to `0.0.0.0` to accept connections from outside the machine (required on most hosts) |
| `PORT`   | `8000`      | Most hosting platforms (Railway included) inject this automatically |


## Built-in protections for public/shared use

Since this is meant to be shared, `server_maps.py` already includes:

- **A shared per-task cache** (6h TTL) — the first visitor to ask about a
  given quest triggers the real wiki/tarkov.dev lookup; everyone else gets
  the cached result for the next 6 hours, so load on those two sites stays
  low as usage grows.
- **A circuit breaker for the wiki** — if the wiki starts returning
  403/429s, the app backs off for 90 seconds instead of hammering it
  further.
- **Per-IP rate limiting** (20 requests/min), a request body size cap, and
  a max-tasks-per-request cap, as a baseline defense against abuse.
- **Thread-safe shared state** — safe under concurrent requests from
  multiple visitors at once (the server is multi-threaded by default).

If usage grows a lot, the first knob to turn is `_TASK_RESULT_TTL_SECONDS`
near the top of `server_maps.py` (currently 6 hours) — raising it further
reduces load on the wiki and tarkov.dev even more, at the cost of staleness
if a task's data genuinely changes (rare — task text/locations don't change
often between game wipes).

## Project structure

```
server_maps.py        # the entire backend: scraping, caching, the HTTP server
eft-task-guide.html   # the entire frontend: one static page, vanilla JS
Procfile              # tells Railway (and similar platforms) how to start it
requirements.txt      # the one runtime dependency
.python-version        # pins the Python version used to build/run it
```

## License / courtesy note

This tool fetches from the Fandom wiki and from tarkov.dev's public
dataset on every uncached lookup. The caching above is what keeps that
polite at scale — please don't remove it or drop the TTL to something very
small on a public deployment. Map artwork is pulled from the
[`the-hideout/tarkov-dev-svg-maps`](https://github.com/the-hideout/tarkov-dev-svg-maps)
repository (CC BY-NC-SA 4.0) — fine for a free, ad-free tool like this one.
