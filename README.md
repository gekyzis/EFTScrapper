# EFT Task Planner 

A compact, standalone web app that lets users input Escape From Tarkov tasks 
— either manually or via OCR from an uploaded image — 
and presents each task’s objectives, tactical guidance, and precise in‑raid map locations on a single page. 
Locations are shown as numbered pins on the actual map, and when multiple tasks are queued, the app suggests an optimal raid order. 
It made managing my EFT tasks far more efficient by eliminating the time spent figuring out what to do, how to do it, and where to go.
I hope you find it usefull too.

Data comes from two live sources, fetched server-side on each lookup:
- the [Escape From Tarkov Fandom wiki](https://escapefromtarkov.fandom.com/wiki/Escape_from_Tarkov_Wiki)
  — objectives, guide text, and a location fallback
- [tarkov.dev](https://tarkov.dev)'s public dataset (via `json.tarkov.dev`)
  — authoritative per-objective map assignment and exact pin coordinates

No database, no build step, no framework — one Python file serving the API
and the static page.

View it here-> https://eftscrapper-production.up.railway.app/
If you’re hitting 403s, try running it locally. Cloudflare/Fandom often blocks anything coming from cloud‑host IP ranges, while normal home IPs usually get through just fine.

<img width="903" height="504" alt="image" src="https://github.com/user-attachments/assets/f16453b3-fc11-4d89-87ae-6b270db59c54" />

<img width="637" height="1020" alt="image" src="https://github.com/user-attachments/assets/ae81971f-9fda-4efb-8955-a8307f657a36" />


## Running it locally

You need Python installed on your machine. You can install it via PowerShell using:
```bash
winget install Python.Python.3.14
```
or download it directly from: [python.org](https://www.python.org/downloads/)

Download server_maps.py & eft-task-guide.html locally, e.g. to Downloads\EFTSCRAPPER. Browse to this folder and run:
```bash
python server_maps.py
```

Then open **http://localhost:8000/**. The first run installs `curl_cffi`
automatically if it isn't already present.

Enter task names (or use "📷 Auto-Read" to OCR them from a screenshot of
your in-game quest log), queue a few, and click **Generate guidance**.

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
