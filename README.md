# torrent_search.py v2

A command-line tool for finding torrents via **BTDig**, then resolving live peers and full file listings straight from the **BitTorrent DHT** (Kademlia), with multi-sheet XLSX export.

This is a major rewrite of the original v1 script. It replaces the APIBay flow with real browser automation against BTDig, queries the DHT directly (BEP5 peers + BEP9 metadata), and falls back to UDP/WSS trackers and public `.torrent` caches.

---

## What's new in v2

| Feature | v1 | v2 |
|---------|----|----|
| Search source | APIBay API | BTDig via Playwright/Chromium |
| Peer count | None | DHT (Kademlia, BEP5) + UDP/WSS tracker fallback |
| File listing | None | Full list via DHT/BEP9 metadata — beyond BTDig's partial excerpt |
| Output | CSV (pipe-delimited) | XLSX (2 sheets) + SQLite |
| Resume / checkpoint | No | Yes (`--resume`) |
| Download `.torrent` | No | Optional (`--download`) |
| File inventory | No | Yes (from torrent file or page excerpt) |
| Error recovery | Basic | Network retries + clean Ctrl+C resume |

---

## Requirements

```
Python 3.11+
pip install requests beautifulsoup4 openpyxl playwright
```

The tool drives a Chromium-based browser. It uses your **system Google Chrome or
Microsoft Edge** if installed; otherwise download Playwright's bundled Chromium:

```
playwright install chromium
```

Optional extras:

```
pip install websocket-client   # WebSocket peer scraping (WSS)
pip install torf               # Better .torrent file parsing
```

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt
# (optional — only if you don't already have Chrome or Edge installed)
playwright install chromium

# 2. Create a keyword file (one term per line)
echo "python programming" > my_keywords.txt

# 3. Run
python torrent_search.py my_keywords.txt
```

Output:
- `torrents.db` — SQLite database with all results
- `YYYY-MM-DD_Results.xlsx` — 2-sheet spreadsheet (Results / Torrent_Contents)
- `magnets/` — `.magnet` files
- `torrents_downloaded/` — `.torrent` files (with `--download`)

---

## Usage

```
python torrent_search.py keywords.txt [more.txt ...] [options]

Positional:
  files                  Keyword files (UTF-8, one term per line, # for comments)

Options:
  --btdig-pages N          BTDig pages per keyword, 10 results/page (default: 15)
  --delay-btdig N          Seconds between pages (default: 1.5)
  --delay-btdig-keyword N  Pause between keywords in seconds (default: 3.0)
  --download               Download .torrent for each result
  --scrape-method          auto|dht|udp|wss|none  (default: auto)
  --scrape-timeout N       Peer scrape timeout in seconds (default: 5.0)
  --resume                 Continue from last checkpoint
  --reset-checkpoint       Delete checkpoint, reprocess everything
  --browser-visible        Show Chromium window (debug)
```

### Scrape methods

| Method | What it does |
|--------|--------------|
| `auto` | **DHT first** (real peers via BEP5), then UDP→WSS tracker fallback — default |
| `dht`  | DHT only: BEP5 peers + **full file list via BEP9 metadata** (no tracker/cache needed) |
| `udp`  | UDP tracker scrape (BEP 15) only |
| `wss`  | WebSocket tracker (WSS, port 443) only |
| `none` | No peer lookup (fastest) |

The DHT path is pure-Python (no extra dependencies) and queries the swarm directly,
so it finds far more live peers than trackers and reconstructs the real file list
from peers even when no public cache has the `.torrent`.

---

## Database schema

```sql
-- Main results
CREATE TABLE torrents (
    info_hash     TEXT PRIMARY KEY,
    name          TEXT,
    seeders       INTEGER,
    leechers      INTEGER,
    size_bytes    TEXT,
    num_files     TEXT,
    category      TEXT,          -- reserved (currently 'Unknown')
    source        TEXT,          -- always 'BTDig'
    keyword       TEXT,
    added_utc     TEXT,
    magnet        TEXT,
    torrent_file  TEXT,          -- local path if --download used
    captured_at   TEXT,
    detail_url    TEXT,
    scrape_method TEXT           -- 'dht' / 'udp' / 'wss' / null
);

-- File inventory (populated from .torrent or page excerpt)
CREATE TABLE torrent_contents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    info_hash  TEXT NOT NULL,
    file_path  TEXT,
    file_size  INTEGER
);
```

---

## Files generated

```
.
├── torrents.db                  # SQLite results database
├── btdig_checkpoint.json        # Resume checkpoint
├── YYYY-MM-DD_Results.xlsx      # Spreadsheet export (2 sheets)
├── torrent_search_debug.log     # Debug log
├── magnets/                     # .magnet files
└── torrents_downloaded/         # .torrent files (with --download)
```

---

## Notes

- **DHT** (`auto` / `dht`): queries the BitTorrent DHT (Kademlia, BEP5) directly for live peers, and pulls the real name + full file list from peers via BEP9 metadata — pure Python, no extra dependencies. `auto` tries the DHT first, then falls back to UDP/WSS trackers. A truly dead torrent (0 live peers) still yields nothing — its metadata lives on no peer.
- **Browser**: the tool tries system Chrome → system Edge → Playwright's bundled Chromium, in that order. `--browser-visible` shows the window (debug) with whichever launches.
- **BTDig** is a public DHT search engine. No API key required.
- **`--download`** also fetches the `.torrent` from public caches (itorrents.org is the only consistently working one as of 2026). In `dht`/`auto` the full file list comes from BEP9 metadata first, so a successful DHT lookup no longer depends on the caches. SSL verification is disabled for cache fetches (self-signed certs are common).
- The checkpoint file records which keywords have been processed — safe to interrupt with Ctrl+C and resume with `--resume`.
- Non-ASCII search terms (e.g. Arabic, Chinese) are sent unquoted; ASCII terms are wrapped in quotes for exact matching.

---

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See the [LICENSE](LICENSE) file for more details.
