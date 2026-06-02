#!/usr/bin/env python3
"""
torrent_search.py — BTDig P2P Search Tool v2
=============================================
Search BitTorrent DHT via BTDig with live peer scraping and XLSX export.

Sources     : BTDig (Playwright/Chromium, clearnet)
Peer scrape : UDP tracker / WebSocket WSS / none

Scrape methods (--scrape-method):
  auto  DHT first (real peers via BEP5), then UDP/WSS tracker fallback [default]
  dht   DHT only — BEP5 peers + full file list via BEP9 metadata
  udp   UDP tracker (BEP 15) only
  wss   WebSocket tracker (WSS, port 443) only
  none  No peer lookup (fastest)

Usage:
  python torrent_search.py keywords.txt [more_keywords.txt ...]
         [--scrape-method auto|dht|udp|wss|none]
         [--scrape-timeout N]
         [--btdig-pages N] [--download]
         [--delay-btdig N] [--delay-btdig-keyword N]
         [--resume] [--reset-checkpoint]
         [--browser-visible]
"""

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import json
import sqlite3
import datetime
import argparse
import struct
import socket
import threading
import queue
import ssl
import os
import sys
import re
import io
import time
import logging
import random
import asyncio
import hashlib
from urllib.parse import quote

# ────────────────────────────────────────────────────────────────────────────
# UTF-8 stdout (Windows)
# ────────────────────────────────────────────────────────────────────────────
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ────────────────────────────────────────────────────────────────────────────
# Logging: debug file + stderr WARNING
# ────────────────────────────────────────────────────────────────────────────
_LOG_FILE = 'torrent_search_debug.log'
_root = logging.getLogger()
_root.setLevel(logging.DEBUG)

# Console: WARNING and above only (keeps the run output clean)
_ch = logging.StreamHandler()
_ch.setLevel(logging.WARNING)
_ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
_root.addHandler(_ch)

# File: full DEBUG trace
_fh = logging.FileHandler(_LOG_FILE, encoding='utf-8', mode='w')
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)-8s %(funcName)-28s %(message)s', datefmt='%H:%M:%S'
))
_root.addHandler(_fh)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────
DB_NAME         = 'torrents.db'
FOLDER_TORRENT  = 'torrents_downloaded'
FOLDER_MAGNET   = 'magnets'
CHECKPOINT_FILE = 'btdig_checkpoint.json'
BTDIG_CLEARNET  = 'https://btdig.com'

# Public infohash → .torrent caches, tried in order; first valid bencode wins.
# itorrents.org is the only consistently working one (2026); rest are fallbacks.
TORRENT_CACHES = [
    'https://itorrents.org/torrent/{hash}.torrent',
    'https://torrage.info/torrent.php?h={hash}',
    'https://btcache.me/torrent/{hash}',
    'https://torcache.net/{hash}.torrent',
    'https://torcache.pro/torrent/{hash}.torrent',
    'https://www.seedpeer.eu/torrent/magnet/{hash}.torrent',
    'https://cors.btdig.com/{hash}.torrent',
]

# UDP trackers — ports 80 and 451 pass most corporate firewalls
UDP_TRACKERS = [
    ('open.stealth.si',              80),
    ('tracker.torrent.eu.org',      451),
    ('tracker.opentrackr.org',     1337),
    ('tracker.theoks.net',         6969),
    ('explodie.org',               6969),
    ('tracker.openbittorrent.com', 6969),
]

# WebSocket trackers (WSS, port 443) — fallback for UDP-blocked networks
WSS_TRACKERS = [
    'wss://tracker.openwebtorrent.com',
    'wss://tracker.webtorrent.dev',
    'wss://tracker.novage.com.ua',
]

BTDIG_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Mozilla/5.0 (Windows NT 10.0; rv:115.0) Gecko/20100101 Firefox/115.0',
    'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Mozilla/5.0 (Windows NT 10.0; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0',
]

BTDIG_ACCEPT_LANGUAGES = [
    'en-US,en;q=0.5', 'en-GB,en;q=0.5', 'en-US,en;q=0.8', 'en;q=0.9',
]

BTDIG_RESULT_SELECTORS = [
    ('div', {'class': 'one_result'}),
    ('div', {'class': re.compile(r'result')}),
]

HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept':          'text/html,application/xhtml+xml,*/*;q=0.8',
}

# ────────────────────────────────────────────────────────────────────────────
# Scrape — UDP tracker (BEP 15)
# ────────────────────────────────────────────────────────────────────────────

def _udp_scrape_one(host: str, port: int, ih_bytes: bytes, timeout: float) -> tuple[int, int] | None:
    """
    Run UDP scrape against one tracker (connect + scrape).
    Returns (seeders, leechers) or None on failure.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        # Step 1: Connect request
        tid = random.randint(0, 0xFFFFFFFF)
        s.sendto(struct.pack('>QII', 0x41727101980, 0, tid), (host, port))
        resp = s.recv(16)
        action, r_tid, conn_id = struct.unpack('>IIQ', resp)
        if action != 0 or r_tid != tid:
            return None

        # Step 2: Scrape request
        tid2 = random.randint(0, 0xFFFFFFFF)
        s.sendto(struct.pack('>QII', conn_id, 2, tid2) + ih_bytes, (host, port))
        resp2 = s.recv(20)
        action2, r_tid2 = struct.unpack('>II', resp2[:8])
        if action2 != 2 or r_tid2 != tid2:
            return None
        seeders, _completed, leechers = struct.unpack('>III', resp2[8:20])
        return (seeders, leechers)
    except Exception as e:
        logging.debug('udp_scrape %s:%d — %s', host, port, e)
        return None
    finally:
        s.close()


def udp_scrape_sync(info_hash_hex: str, timeout: float = 4.0) -> tuple[int, int]:
    """
    Query all UDP_TRACKERS in parallel.
    Returns (best_seeds, best_leech). -1 if no response.
    """
    ih_bytes = bytes.fromhex(info_hash_hex.lower())
    q: queue.Queue = queue.Queue()

    def worker(host, port):
        result = _udp_scrape_one(host, port, ih_bytes, timeout)
        if result:
            q.put(result)

    threads = [
        threading.Thread(target=worker, args=(h, p), daemon=True)
        for h, p in UDP_TRACKERS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 1.0)

    best_s, best_l = -1, -1
    while not q.empty():
        s, l = q.get_nowait()
        if s > best_s:
            best_s, best_l = s, l

    logging.debug('udp_scrape ih=%s seeds=%d', info_hash_hex[:12], best_s)
    return best_s, best_l


# ────────────────────────────────────────────────────────────────────────────
# Scrape — WebSocket tracker (WSS, port 443)
# ────────────────────────────────────────────────────────────────────────────

def _wss_scrape_one(url: str, ih_hex: str, result_q: queue.Queue, timeout: float):
    """
    Announce to a WSS tracker and put (seeds, leech) in result_q.
    Announces as leecher (left=999999999) so 'complete' equals real seeds.
    """
    try:
        import websocket
    except ImportError:
        logging.warning('websocket-client not installed: pip install websocket-client')
        return

    ih_bytes = bytes.fromhex(ih_hex.lower())
    ih_str   = ih_bytes.decode('latin-1')
    peer_id  = '-WS0001-' + os.urandom(12).hex()[:12]
    received = []

    def on_message(ws, msg):
        received.append(msg)
        ws.close()

    def on_open(ws):
        ws.send(json.dumps({
            'action':     'announce',
            'info_hash':  ih_str,
            'peer_id':    peer_id,
            'uploaded':   0,
            'downloaded': 0,
            'left':       999999999,
            'event':      'started',
            'numwant':    50,
        }))

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    app = websocket.WebSocketApp(
        url, on_open=on_open, on_message=on_message,
        on_error=lambda ws, e: logging.debug('wss %s error: %s', url, e),
    )
    app.run_forever(sslopt={'context': ssl_ctx}, ping_timeout=int(timeout), ping_interval=0)

    if received:
        try:
            data  = json.loads(received[0])
            seeds = int(data.get('complete',   -1))
            leech = int(data.get('incomplete', -1))
            # 'incomplete' includes our own announce — subtract 1
            if leech > 0:
                leech -= 1
            result_q.put((seeds, leech))
            logging.debug('wss %s seeds=%d leech=%d', url, seeds, leech)
        except Exception as e:
            logging.debug('wss parse error %s: %s', url, e)


def wss_scrape_sync(info_hash_hex: str, timeout: float = 6.0) -> tuple[int, int]:
    """
    Query all WSS_TRACKERS in parallel.
    Returns (best_seeds, best_leech). -1 if no response.
    """
    q: queue.Queue = queue.Queue()
    threads = [
        threading.Thread(target=_wss_scrape_one, args=(url, info_hash_hex, q, min(timeout, 5.0)), daemon=True)
        for url in WSS_TRACKERS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=min(timeout, 5.0) + 1.0)

    best_s, best_l = -1, -1
    while not q.empty():
        s, l = q.get_nowait()
        if s > best_s:
            best_s, best_l = s, l

    logging.debug('wss_scrape ih=%s seeds=%d', info_hash_hex[:12], best_s)
    return best_s, best_l


# ────────────────────────────────────────────────────────────────────────────
# Scrape — unified entry point
# ────────────────────────────────────────────────────────────────────────────

def scrape_peers(info_hash: str, method: str = 'auto',
                 timeout: float = 5.0) -> tuple[int, int]:
    """
    Get (seeders, leechers) using the selected method.

    method:
      'auto' — UDP first; if all return -1, WSS as fallback
      'udp'  — UDP tracker BEP 15 only
      'wss'  — WebSocket tracker (port 443) only
      'none' — returns (-1, -1) without querying
    """
    if method == 'none':
        return -1, -1

    if method in ('udp', 'auto'):
        udp_t = min(timeout, 4.0)
        s, l  = udp_scrape_sync(info_hash, timeout=udp_t)
        if s >= 0:
            logging.debug('scrape_peers UDP ok ih=%s s=%d', info_hash[:12], s)
            return s, l
        if method == 'udp':
            return -1, -1
        logging.debug('scrape_peers UDP miss → WSS ih=%s', info_hash[:12])

    return wss_scrape_sync(info_hash, timeout=timeout)


# ────────────────────────────────────────────────────────────────────────────
# DHT (Kademlia) — direct peer + metadata resolution (BEP5 + BEP9, pure Python)
# ────────────────────────────────────────────────────────────────────────────
# One UDP socket multiplexes all infohashes concurrently (total time ≈ one window).

DHT_BOOTSTRAP = [
    ('router.bittorrent.com',  6881),
    ('router.utorrent.com',    6881),
    ('dht.transmissionbt.com', 6881),
    ('dht.aelitis.com',        6881),
]
DHT_BOOTSTRAP_IPS = [
    ('67.215.246.10',  6881),   # router.bittorrent.com
    ('82.221.103.244', 6881),   # router.utorrent.com
    ('87.98.162.88',   6881),   # dht.transmissionbt.com
]
_OUR_PEER_ID = b'-qB5040-' + ''.join(random.choices(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12)).encode()


def _bencode(obj) -> bytes:
    if isinstance(obj, int):
        return b'i' + str(obj).encode() + b'e'
    if isinstance(obj, (bytes, bytearray)):
        return str(len(obj)).encode() + b':' + bytes(obj)
    if isinstance(obj, str):
        enc = obj.encode()
        return str(len(enc)).encode() + b':' + enc
    if isinstance(obj, list):
        return b'l' + b''.join(_bencode(x) for x in obj) + b'e'
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0])
        return b'd' + b''.join(_bencode(k) + _bencode(v) for k, v in items) + b'e'
    raise TypeError(f'no bencode for {type(obj)}')


def _dht_parse_nodes(raw: bytes) -> list:
    out = []
    for i in range(0, len(raw) - 25, 26):
        try:
            nid  = raw[i:i+20]
            ip   = socket.inet_ntoa(raw[i+20:i+24])
            port = struct.unpack('>H', raw[i+24:i+26])[0]
            if 1 <= port <= 65535:
                out.append((nid, ip, port))
        except Exception:
            pass
    return out


def _dht_parse_peers(values) -> list:
    out = []
    for v in values or []:
        if isinstance(v, (bytes, bytearray)) and len(v) == 6:
            try:
                ip   = socket.inet_ntoa(v[:4])
                port = struct.unpack('>H', v[4:6])[0]
                if 1 <= port <= 65535:
                    out.append((ip, port))
            except Exception:
                pass
    return out


def _xor_dist(a: bytes, b: bytes) -> int:
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), 'big')


def _bloom_count(bf: bytes) -> int:
    """BEP33: estimate the element count of a 256-byte bloom filter."""
    if len(bf) != 256:
        return 0
    try:
        import math
        bits_zero = sum(bin(b).count('0') for b in bf)
        m, k = 2048, 2
        c = max(1, bits_zero)
        if c >= m:
            return 0
        return max(0, int(round(math.log(c / m) / (k * math.log(1.0 - 1.0 / m)))))
    except Exception:
        return 0


def _dht_decode(b) -> str:
    if not isinstance(b, (bytes, bytearray)):
        return str(b) if b is not None else ''
    if not b:
        return ''
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('latin-1', errors='replace')


def _dht_info_files(info: dict):
    """Extract {'name', 'files':[(path,size),...]} from a torrent info-dict."""
    name = _dht_decode(info.get(b'name') or b'')
    if not name:
        return None
    files = []
    if b'files' in info and isinstance(info[b'files'], list):
        for f in info[b'files']:
            parts = f.get(b'path', []) if isinstance(f, dict) else []
            path  = name + '/' + '/'.join(_dht_decode(p) for p in parts)
            files.append((path, int(f.get(b'length', 0))))
    else:
        files.append((name, int(info.get(b'length', 0))))
    return {'name': name, 'files': files}


async def _dht_fetch_metadata(ip: str, port: int, ih_bytes: bytes):
    """BEP9 ut_metadata fetch from one peer → {'name','files'} or None.
    Verifies the assembled info-dict actually hashes to the requested infohash."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=3.0)
    except Exception:
        return None
    try:
        ext_bits = b'\x00\x00\x00\x00\x00\x10\x00\x01'   # BEP10 + DHT + Fast bits
        writer.write(b'\x13BitTorrent protocol' + ext_bits + ih_bytes + _OUR_PEER_ID)
        await writer.drain()
        hs = await asyncio.wait_for(reader.readexactly(68), timeout=5.0)
        if hs[:20] != b'\x13BitTorrent protocol' or not (hs[25] & 0x10):
            return None
        ext_hs = _bencode({b'm': {b'ut_metadata': 1}, b'v': b'qBittorrent/5.0.4', b'reqq': 250})
        writer.write(struct.pack('>IB', len(ext_hs) + 2, 20) + b'\x00' + ext_hs)
        await writer.drain()

        ut_meta_id   = None
        metadata_size = 0
        deadline = time.monotonic() + 6.0
        while ut_meta_id is None:
            if time.monotonic() > deadline:
                return None
            length = struct.unpack('>I', await asyncio.wait_for(reader.readexactly(4), timeout=4.0))[0]
            if length == 0:
                continue
            if length > (1 << 20):
                return None
            body = await asyncio.wait_for(reader.readexactly(length), timeout=4.0)
            if body[0] != 20 or body[1] != 0:
                continue
            try:
                ext_data, _ = _bdecode(body[2:])
            except Exception:
                continue
            if not isinstance(ext_data, dict):
                continue
            m = ext_data.get(b'm', {})
            ut_meta_id    = m.get(b'ut_metadata') if isinstance(m, dict) else None
            metadata_size = int(ext_data.get(b'metadata_size', 0))
        if not ut_meta_id or metadata_size <= 0 or metadata_size > (10 << 20):
            return None

        num_pieces = (metadata_size + 16383) // 16384
        for piece in range(num_pieces):
            req = _bencode({b'msg_type': 0, b'piece': piece})
            writer.write(struct.pack('>IB', len(req) + 2, 20) + bytes([ut_meta_id]) + req)
        await writer.drain()

        pieces = {}
        deadline2 = time.monotonic() + 8.0
        while len(pieces) < num_pieces:
            if time.monotonic() > deadline2:
                break
            try:
                length = struct.unpack('>I', await asyncio.wait_for(reader.readexactly(4), timeout=4.0))[0]
                if length == 0:
                    continue
                if length > metadata_size + 4096:
                    break
                body = await asyncio.wait_for(reader.readexactly(length), timeout=4.0)
            except asyncio.TimeoutError:
                break
            if len(body) < 2 or body[0] != 20 or body[1] not in (1, ut_meta_id):
                continue
            try:
                resp, end = _bdecode(body[2:])
                if not isinstance(resp, dict) or resp.get(b'msg_type') != 1:
                    continue
                pn = int(resp.get(b'piece', -1))
                if 0 <= pn < num_pieces:
                    pieces[pn] = body[2 + end:]
            except Exception:
                continue
        if len(pieces) < num_pieces:
            return None

        raw = b''.join(pieces[i] for i in range(num_pieces))
        if hashlib.sha1(raw).digest() != ih_bytes:    # metadata must match the infohash
            return None
        info, _ = _bdecode(raw)
        if not isinstance(info, dict):
            return None
        return _dht_info_files(info)
    except Exception:
        return None
    finally:
        try:
            writer.close()
        except Exception:
            pass


_DHT_ALPHA       = 3     # concurrent queries per round per infohash
_DHT_MAX_QUERIES = 80    # hard cap of nodes queried per infohash


class _DHTClient(asyncio.DatagramProtocol):
    """Convergent iterative Kademlia get_peers lookup for many infohashes over one
    UDP socket. Per infohash: real peers (values) + BEP33 seed/leech estimates."""

    def __init__(self, our_id: bytes):
        self.our_id    = our_id
        self.transport = None
        self._tid      = 0
        self.pending   = {}    # tid -> ih_hex
        self.st        = {}    # ih_hex -> state

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass                   # ignore ICMP port-unreachable etc.

    def add_target(self, ih_hex: str):
        self.st[ih_hex] = {'ih': bytes.fromhex(ih_hex), 'peers': set(),
                           'seeds': -1, 'leech': -1,
                           'cand': {}, 'queried': set()}   # cand: (ip,port)->xor_dist

    def add_candidate(self, ih_hex: str, nid, ip: str, port: int):
        s = self.st.get(ih_hex)
        if s is None:
            return
        key = (ip, port)
        if key in s['queried'] or key in s['cand']:
            return
        s['cand'][key] = _xor_dist(nid, s['ih']) if nid else (1 << 161)  # bootstrap = far

    def send_query(self, ih_hex: str, ip: str, port: int):
        s = self.st.get(ih_hex)
        if s is None or self.transport is None or (ip, port) in s['queried']:
            return
        s['queried'].add((ip, port))
        s['cand'].pop((ip, port), None)
        self._tid = (self._tid + 1) & 0xFFFFFFFF
        tid = struct.pack('>I', self._tid)
        self.pending[tid] = ih_hex
        q = _bencode({b't': tid, b'y': b'q', b'q': b'get_peers',
                      b'a': {b'id': self.our_id, b'info_hash': s['ih'], b'scrape': 1}})
        try:
            self.transport.sendto(q, (ip, port))
        except Exception:
            pass

    def step(self, ih_hex: str) -> int:
        """Query the ALPHA closest still-unqueried candidates. Returns count sent."""
        s = self.st.get(ih_hex)
        if s is None or not s['cand'] or len(s['queried']) >= _DHT_MAX_QUERIES:
            return 0
        closest = sorted(s['cand'].items(), key=lambda kv: kv[1])[:_DHT_ALPHA]
        for (ip, port), _dist in closest:
            self.send_query(ih_hex, ip, port)
        return len(closest)

    def datagram_received(self, data, addr):
        try:
            msg, _ = _bdecode(data)
        except Exception:
            return
        if not isinstance(msg, dict) or msg.get(b'y') != b'r':
            return
        r = msg.get(b'r', {})
        if not isinstance(r, dict):
            return
        ih_hex = self.pending.pop(msg.get(b't', b''), None)
        if ih_hex is None:
            return
        s = self.st.get(ih_hex)
        if s is None:
            return
        bfsd = r.get(b'BFsd', b'')
        bfpe = r.get(b'BFpe', b'')
        if (isinstance(bfsd, (bytes, bytearray)) and len(bfsd) == 256 and
                isinstance(bfpe, (bytes, bytearray)) and len(bfpe) == 256):
            s['seeds'] = max(s['seeds'], _bloom_count(bytes(bfsd)))
            s['leech'] = max(s['leech'], _bloom_count(bytes(bfpe)))
        for ip, port in _dht_parse_peers(r.get(b'values', [])):
            s['peers'].add((ip, port))
        for nid, ip, port in _dht_parse_nodes(r.get(b'nodes', b'') or b''):
            self.add_candidate(ih_hex, nid, ip, port)


async def _dht_resolve_async(ih_list: list, want_metadata: bool, lookup_timeout: float) -> dict:
    our_id = os.urandom(20)
    loop   = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: _DHTClient(our_id), local_addr=('0.0.0.0', 0))
    try:
        boot = []
        for host, port in DHT_BOOTSTRAP:
            try:
                boot.append((socket.gethostbyname(host), port))
            except Exception:
                pass
        for ip, port in DHT_BOOTSTRAP_IPS:
            if (ip, port) not in boot:
                boot.append((ip, port))

        for ih_hex in ih_list:
            h = ih_hex.lower()
            if h not in proto.st:
                proto.add_target(h)
                for ip, port in boot:          # seed: query every bootstrap node now
                    proto.send_query(h, ip, port)

        deadline = time.monotonic() + lookup_timeout
        while time.monotonic() < deadline:
            sent = 0
            for h in list(proto.st.keys()):    # advance each lookup toward its target
                sent += proto.step(h)
            if sent == 0 and not proto.pending:
                break                          # every lookup converged / exhausted
            await asyncio.sleep(0.35)
    finally:
        transport.close()

    out = {}
    for ih_hex in ih_list:
        s = proto.st.get(ih_hex.lower(), {})
        out[ih_hex] = {'peers':    s.get('peers', set()),
                       'seeders':  s.get('seeds', -1),
                       'leechers': s.get('leech', -1),
                       'metadata': None}

    if want_metadata:
        sem = asyncio.Semaphore(10)

        async def _fetch(ih_hex):
            for ip, port in list(out[ih_hex]['peers'])[:6]:
                async with sem:
                    md = await _dht_fetch_metadata(ip, port, bytes.fromhex(ih_hex.lower()))
                if md and md.get('files'):
                    out[ih_hex]['metadata'] = md
                    return

        await asyncio.gather(*[_fetch(h) for h in ih_list if out[h]['peers']],
                             return_exceptions=True)
    return out


def dht_resolve_many(info_hashes: list, want_metadata: bool = False,
                     timeout: float = 5.0) -> dict:
    """Resolve infohashes via the DHT → {ih: {peers, seeders, leechers, metadata}}.
    Safe no-op ({}) on any failure, so the caller can fall back to tracker scrape."""
    if not info_hashes:
        return {}
    # Windows' default Proactor loop drops inbound UDP for datagram endpoints; the
    # selector loop doesn't. (Safe here: Playwright is already closed — no loop clash.)
    try:
        loop = (asyncio.SelectorEventLoop() if sys.platform == 'win32'
                else asyncio.new_event_loop())
    except Exception:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _dht_resolve_async(info_hashes, want_metadata, max(timeout, 12.0)))
    except Exception as e:
        logging.debug('dht_resolve_many: %s', e)
        return {}
    finally:
        try:
            loop.close()
        except Exception:
            pass


def store_dht_contents(info_hash: str, files: list) -> int:
    """Store the full file list from DHT/BEP9 metadata (replaces any prior excerpt)."""
    if not files:
        return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute('DELETE FROM torrent_contents WHERE info_hash=?', (info_hash,))
    conn.executemany(
        'INSERT INTO torrent_contents (info_hash, file_path, file_size) VALUES (?,?,?)',
        [(info_hash, p, s) for p, s in files]
    )
    conn.commit(); conn.close()
    return len(files)


# ────────────────────────────────────────────────────────────────────────────
# Checkpoint
# ────────────────────────────────────────────────────────────────────────────

def checkpoint_load() -> set:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, encoding='utf-8') as f:
                return set(json.load(f).get('done', []))
        except Exception:
            pass
    return set()


def checkpoint_save(done: set) -> None:
    try:
        with open(CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'done': sorted(done), 'ts': datetime.datetime.utcnow().isoformat()}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        logging.debug('checkpoint_save: %s', e)


# ────────────────────────────────────────────────────────────────────────────
# Database
# ────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS torrents (
            info_hash    TEXT PRIMARY KEY,
            name         TEXT,
            seeders      INTEGER DEFAULT NULL,
            leechers     INTEGER DEFAULT NULL,
            size_bytes   TEXT,
            num_files    TEXT,
            category     TEXT,
            source       TEXT,
            keyword      TEXT,
            added_utc    TEXT,
            magnet       TEXT,
            torrent_file TEXT,
            captured_at  TEXT,
            detail_url   TEXT,
            scrape_method TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS torrent_contents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            info_hash  TEXT NOT NULL,
            file_path  TEXT,
            file_size  INTEGER,
            FOREIGN KEY (info_hash) REFERENCES torrents(info_hash)
        )
    ''')
    # Idempotent migrations
    for col, defn in [('detail_url', 'TEXT'), ('scrape_method', 'TEXT')]:
        try:
            cur.execute(f'ALTER TABLE torrents ADD COLUMN {col} {defn}')
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def upsert_torrent(row: dict) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()
    now  = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    try:
        seeds = row.get('seeders', -1)
        leech = row.get('leechers', -1)
        cur.execute('''
            INSERT OR IGNORE INTO torrents
            (info_hash,name,seeders,leechers,size_bytes,num_files,
             category,source,keyword,added_utc,magnet,torrent_file,
             captured_at,detail_url,scrape_method)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            row['info_hash'], row.get('name'), seeds, leech,
            row.get('size_bytes'), row.get('num_files'),
            row.get('category', 'Unknown'), row.get('source', 'BTDig'),
            row.get('keyword'), row.get('added_utc', now),
            row.get('magnet'), row.get('torrent_file'),
            now,
            row.get('detail_url'), row.get('scrape_method'),
        ))
        conn.commit()
        inserted = cur.rowcount > 0
        if inserted and row.get('magnet'):
            write_magnet_file(row['info_hash'], row['magnet'])
        return inserted
    except Exception as e:
        logging.warning('DB insert error: %s', e)
        return False
    finally:
        conn.close()


def update_torrent_file_path(info_hash: str, path: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    conn.execute('UPDATE torrents SET torrent_file=? WHERE info_hash=?', (path, info_hash))
    conn.commit()
    conn.close()


# ────────────────────────────────────────────────────────────────────────────
# Utilities: folders, magnets
# ────────────────────────────────────────────────────────────────────────────

def ensure_folder(*parts) -> str:
    path = os.path.join(os.getcwd(), *parts)
    os.makedirs(path, exist_ok=True)
    return path


def init_folders() -> None:
    ensure_folder(FOLDER_TORRENT)
    ensure_folder(FOLDER_MAGNET)


def build_magnet(info_hash: str, name: str = '') -> str:
    ih = info_hash.lower()
    trs = (
        '&tr=udp://open.stealth.si:80/announce'
        '&tr=udp://tracker.opentrackr.org:1337/announce'
        '&tr=udp://tracker.torrent.eu.org:451/announce'
        '&tr=udp://tracker.openbittorrent.com:6969/announce'
        '&tr=wss://tracker.openwebtorrent.com'
    )
    return f'magnet:?xt=urn:btih:{ih}&dn={quote(name)}{trs}'


def write_magnet_file(info_hash: str, magnet_uri: str) -> None:
    try:
        folder = ensure_folder(FOLDER_MAGNET)
        dest   = os.path.join(folder, f'{info_hash.lower()}.magnet')
        if not os.path.exists(dest):
            with open(dest, 'w', encoding='utf-8') as fh:
                fh.write(magnet_uri)
    except Exception as e:
        logging.debug('write_magnet_file: %s', e)


# ────────────────────────────────────────────────────────────────────────────
# Download .torrent from public caches
# ────────────────────────────────────────────────────────────────────────────

def _bdecode(data: bytes, idx: int = 0):
    ch = data[idx:idx+1]
    if ch == b'd':
        idx += 1; d = {}
        while data[idx:idx+1] != b'e':
            k, idx = _bdecode(data, idx)
            v, idx = _bdecode(data, idx)
            if isinstance(k, bytes): d[k] = v
        return d, idx + 1
    elif ch == b'l':
        idx += 1; lst = []
        while data[idx:idx+1] != b'e':
            v, idx = _bdecode(data, idx); lst.append(v)
        return lst, idx + 1
    elif ch == b'i':
        end = data.index(b'e', idx)
        return int(data[idx+1:end]), end + 1
    else:
        colon  = data.index(b':', idx)
        length = int(data[idx:colon])
        s      = data[colon+1:colon+1+length]
        return s, colon + 1 + length


def parse_torrent_contents(torrent_path: str) -> list:
    try:
        import torf as _torf
        t = _torf.Torrent.read(torrent_path)
        files = [(str(f), int(f.size) if f.size else 0) for f in t.files]
        if files:
            return files
    except Exception:
        pass
    try:
        with open(torrent_path, 'rb') as f:
            raw = f.read()
        torrent, _ = _bdecode(raw)
        info = torrent.get(b'info', {})
        if b'files' in info:
            root = info.get(b'name', b'').decode('utf-8', errors='replace')
            return [
                (root + '/' + '/'.join(
                    p.decode('utf-8', errors='replace') for p in fd.get(b'path', [])
                ), int(fd.get(b'length', 0)))
                for fd in info[b'files']
            ]
        elif b'name' in info:
            return [(info[b'name'].decode('utf-8', errors='replace'),
                     int(info.get(b'length', 0)))]
    except Exception as e:
        logging.debug('parse_torrent_contents %s: %s', torrent_path, e)
    return []


def store_torrent_contents(info_hash: str, torrent_path: str) -> int:
    files = parse_torrent_contents(torrent_path)
    if not files:
        return 0
    conn = sqlite3.connect(DB_NAME)
    conn.execute('DELETE FROM torrent_contents WHERE info_hash=?', (info_hash,))
    conn.executemany(
        'INSERT INTO torrent_contents (info_hash, file_path, file_size) VALUES (?,?,?)',
        [(info_hash, fp, fs) for fp, fs in files]
    )
    conn.commit(); conn.close()
    return len(files)


def store_excerpt_contents(info_hash: str, excerpt_files: list) -> int:
    if not excerpt_files:
        return 0
    conn = sqlite3.connect(DB_NAME)
    existing = conn.execute(
        'SELECT COUNT(*) FROM torrent_contents WHERE info_hash=?', (info_hash,)
    ).fetchone()[0]
    if existing > 0:
        conn.close(); return 0
    conn.executemany(
        'INSERT INTO torrent_contents (info_hash, file_path, file_size) VALUES (?,?,?)',
        [(info_hash, fp, None) for fp in excerpt_files]
    )
    conn.commit(); conn.close()
    return len(excerpt_files)


def _is_valid_torrent(content: bytes) -> bool:
    """True if content looks like a real bencoded .torrent file (not an error page)."""
    if not content or len(content) < 40:
        return False
    if content[:1] != b'd':          # bencode dict
        return False
    return b'4:info' in content or b'6:pieces' in content or b'5:files' in content


def download_torrent_file(info_hash: str, session: requests.Session,
                          dest_folder: str, name: str = '') -> str | None:
    ih_upper  = info_hash.upper()
    ih_lower  = info_hash.lower()
    safe_name = re.sub(r'[\\/*?:"<>|]', '_', name)[:80] if name else ih_upper
    dest_path = os.path.join(dest_folder, f'{safe_name}.torrent')

    # Reuse a cached local copy only if it is a valid .torrent
    if os.path.exists(dest_path):
        try:
            with open(dest_path, 'rb') as f:
                if _is_valid_torrent(f.read()):
                    store_torrent_contents(ih_lower, dest_path)
                    return dest_path
            logging.warning('download_torrent: existing file invalid, re-downloading %s', ih_upper)
            os.remove(dest_path)
        except Exception:
            pass

    for tpl in TORRENT_CACHES:
        url = tpl.format(hash=ih_upper)
        try:
            r = session.get(url, timeout=12, allow_redirects=True, verify=False)
            if r.status_code == 200 and _is_valid_torrent(r.content):
                with open(dest_path, 'wb') as f:
                    f.write(r.content)
                logging.info('download_torrent: OK from %s (%d bytes)', url, len(r.content))
                store_torrent_contents(ih_lower, dest_path)
                return dest_path
            logging.debug('download_torrent cache %s: status=%s size=%d valid=%s',
                          url, r.status_code, len(r.content or b''),
                          _is_valid_torrent(r.content or b''))
        except Exception as e:
            logging.debug('download_torrent cache %s: %s', url, e)

    # A dead torrent (0 live seeders) can't be fetched from any cache — expected;
    # log quietly so the console isn't flooded when many results are dead.
    logging.debug('download_torrent: no .torrent for %s (not cached / no live seeders)', ih_upper)
    return None


# ────────────────────────────────────────────────────────────────────────────
# BTDig — page fetch with network retries
# ────────────────────────────────────────────────────────────────────────────

def _btdig_page_fetch(page, url: str) -> bytes | None:
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        return None

    for attempt in range(5):
        try:
            page.goto(url, timeout=30000, wait_until='domcontentloaded')
            try:
                page.wait_for_selector('.one_result, .no_result, #search_result', timeout=15000)
            except PWTimeout:
                pass
            return page.content().encode('utf-8')
        except Exception as e:
            err = str(e)
            if 'ERR_INTERNET_DISCONNECTED' in err or 'net::ERR_' in err:
                wait = 30 * (attempt + 1)
                print(f'\n  [NET] No connection — attempt {attempt+1}/5. Waiting {wait}s...',
                      flush=True)
                time.sleep(wait)
                continue
            logging.warning('Playwright BTDig: %s', e)
            return b''

    print('  [NET] No connection after all retries.', flush=True)
    return None


# ────────────────────────────────────────────────────────────────────────────
# BTDig — search with Playwright
# ────────────────────────────────────────────────────────────────────────────

class BrowserUnavailable(RuntimeError):
    """No Chromium-based browser could be launched."""


def _launch_browser(pw, headless: bool):
    """
    Launch a Chromium-based browser. Tries the system Google Chrome and
    Microsoft Edge first (no `playwright install` needed, and sidesteps the
    occasionally-broken bundled-Chromium download), then Playwright's own
    bundled Chromium as a last resort. Raises BrowserUnavailable if none work.
    """
    attempts = [
        ('system Chrome',    {'channel': 'chrome'}),
        ('system Edge',      {'channel': 'msedge'}),
        ('bundled Chromium', {}),
    ]
    last_err = None
    for label, opts in attempts:
        try:
            browser = pw.chromium.launch(headless=headless, **opts)
            logging.debug('launched %s', label)
            return browser
        except Exception as e:
            first = str(e).splitlines()[0] if str(e) else repr(e)
            last_err = first
            logging.debug('launch %s failed: %s', label, first)
    raise BrowserUnavailable(
        'No usable browser found (tried system Chrome, system Edge, bundled Chromium).\n'
        '    Fix: install Google Chrome or Microsoft Edge, or run:  playwright install chromium\n'
        f'    Last error: {last_err}')


def search_btdig_browser(term: str, max_pages: int = 15,
                         delay: float = 1.5, headless: bool = True) -> list | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        logging.error('playwright not installed')
        return []

    _non_ascii  = bool(re.search(r'[^\x00-\x7F]', term))
    encoded     = quote(term) if _non_ascii else quote(f'"{term}"')
    all_res     = []
    net_failed  = False

    with sync_playwright() as pw:
        browser = _launch_browser(pw, headless)
        ctx = browser.new_context(
            user_agent=random.choice(BTDIG_USER_AGENTS),
            locale='en-US',
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()
        page.set_extra_http_headers({'Accept-Language': random.choice(BTDIG_ACCEPT_LANGUAGES)})

        for pg in range(max_pages):
            url  = f'{BTDIG_CLEARNET}/search?q={encoded}&order=0&p={pg}'
            html = _btdig_page_fetch(page, url)

            if html is None:
                net_failed = True; break
            if not html:
                break

            if b'grecaptcha' in html or b'Checking your browser' in html:
                print(f'  [browser] Challenge active on p{pg+1}, waiting...', flush=True)
                try:
                    page.wait_for_selector('.one_result', timeout=20000)
                    html = page.content().encode('utf-8')
                except PWTimeout:
                    print('  [browser] Challenge not resolved.', flush=True)
                    break

            page_res = _parse_btdig_page(html)
            if not page_res:
                break
            all_res.extend(page_res)
            print(f'          BTDig p{pg+1}: {len(page_res)} results', flush=True)
            if len(page_res) < 10:
                break
            time.sleep(delay * random.uniform(0.7, 1.3))

        browser.close()

    return None if net_failed else all_res


# ────────────────────────────────────────────────────────────────────────────
# BTDig — HTML parser
# ────────────────────────────────────────────────────────────────────────────

def _parse_btdig_page(html: bytes) -> list:
    from bs4 import BeautifulSoup
    from urllib.parse import unquote_plus
    soup       = BeautifulSoup(html, 'html.parser')
    results    = []
    containers = []
    for tag, attrs in BTDIG_RESULT_SELECTORS:
        containers = soup.find_all(tag, attrs)
        if containers:
            break

    for item in containers:
        try:
            magnet_a = item.find('a', href=re.compile(r'magnet:\?xt=urn:btih:', re.I))
            if not magnet_a:
                continue
            magnet    = magnet_a['href']
            m_hash    = re.search(r'btih:([0-9a-fA-F]{40})', magnet, re.I)
            if not m_hash:
                continue
            info_hash  = m_hash.group(1).lower()
            detail_url = None

            name_el = item.find(class_=re.compile(r'torrent_name|name', re.I))
            if name_el:
                name_link = name_el.find('a') or name_el
                name = re.sub(r'\s+', ' ', name_link.get_text(separator=' ', strip=True))
                if name_link.name == 'a' and name_link.get('href', '').startswith('/'):
                    detail_url = BTDIG_CLEARNET + name_link['href']
            else:
                link = item.find('a', href=re.compile(r'^/[0-9a-fA-F]{40}'))
                if link:
                    name = link.get_text(strip=True)
                    detail_url = BTDIG_CLEARNET + link['href']
                else:
                    dn_m = re.search(r'[?&]dn=([^&]+)', magnet)
                    name = unquote_plus(dn_m.group(1)) if dn_m else info_hash

            size_el  = item.find(class_=re.compile(r'torrent_size|size', re.I))
            files_el = item.find(class_=re.compile(r'torrent_files|file', re.I))

            excerpt_files = []
            hidden_count  = 0
            excerpt_el    = item.find(class_=re.compile(r'torrent_excerpt', re.I))
            if excerpt_el:
                for div in excerpt_el.find_all('div'):
                    cls = ' '.join(div.get('class', []))
                    txt = div.get_text(separator=' ', strip=True).lstrip('\xa0').strip()
                    if not txt:
                        continue
                    hm = re.search(r'(\d+)\s+hidden\s+file', txt, re.I)
                    if hm:
                        hidden_count = int(hm.group(1)); continue
                    if 'fa-folder' in cls:
                        excerpt_files.append(f'[DIR] {txt}')
                    elif 'fa-' in cls:
                        excerpt_files.append(f'[FILE] {txt}')

            results.append(dict(
                info_hash     = info_hash,
                name          = name,
                size_raw      = size_el.get_text(strip=True) if size_el else '',
                files         = files_el.get_text(strip=True) if files_el else '',
                magnet        = magnet,
                excerpt_files = excerpt_files,
                hidden_count  = hidden_count,
                detail_url    = detail_url,
            ))
        except Exception as e:
            logging.debug('BTDig parse item: %s', e)

    return results


# ────────────────────────────────────────────────────────────────────────────
# Process BTDig results
# ────────────────────────────────────────────────────────────────────────────

def process_btdig_results(results: list, keyword: str,
                          session: requests.Session,
                          download: bool,
                          scrape_method: str = 'auto',
                          scrape_timeout: float = 5.0) -> list:
    hits = []
    now  = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    # DHT is the first resource for both 'dht' and 'auto'. One socket, concurrent
    # lookups. BEP9 metadata (full file list): always in dht; in auto only with --download.
    dht_data = {}
    if scrape_method in ('dht', 'auto') and results:
        print(f'         [DHT] resolving {len(results)} infohashes via Kademlia...', flush=True)
        dht_data = dht_resolve_many(
            [r['info_hash'] for r in results],
            want_metadata = (scrape_method == 'dht') or download,
            timeout       = scrape_timeout,
        )

    for r in results:
        ih       = r['info_hash']
        name     = r['name']
        magnet   = r.get('magnet', build_magnet(ih, name))

        seeds, leech = -1, -1
        used_method  = scrape_method
        if scrape_method in ('dht', 'auto'):
            d  = dht_data.get(ih, {})
            pc = len(d.get('peers', set()))
            md = d.get('metadata')
            if md and md.get('files'):
                store_dht_contents(ih, md['files'])
                if md.get('name'):
                    name = md['name']            # real name + full file list (beats BTDig excerpt)
                print(f'          [DHT] {len(md["files"])} files (full metadata) — {name[:50]}',
                      flush=True)
            if pc > 0:                           # real peers (seed/leech not split)
                seeds, leech, used_method = pc, -1, 'dht'
                print(f'          [DHT] {pc} peers — {name[:50]}', flush=True)
            elif scrape_method == 'auto':        # DHT found nothing → tracker fallback
                seeds, leech = scrape_peers(ih, method='auto', timeout=scrape_timeout)
                used_method = 'udp' if seeds >= 0 else 'wss'
                if seeds >= 0:
                    print(f'          [{used_method.upper()}] {seeds}s/{leech}l — {name[:50]}',
                          flush=True)
            else:
                used_method = 'dht'
        elif scrape_method != 'none':
            seeds, leech = scrape_peers(ih, method=scrape_method, timeout=scrape_timeout)
            if seeds >= 0:
                print(f'          [{used_method.upper()}] {seeds}s/{leech}l — {name[:50]}',
                      flush=True)

        row = dict(
            info_hash    = ih,
            name         = name,
            seeders      = seeds,
            leechers     = leech,
            size_bytes   = r.get('size_raw', ''),
            num_files    = r.get('files', ''),
            category     = 'Unknown',
            source       = 'BTDig',
            keyword      = keyword,
            added_utc    = now,
            magnet       = magnet,
            torrent_file = None,
            detail_url   = r.get('detail_url'),
            scrape_method= used_method if scrape_method != 'none' else None,
        )
        is_new = upsert_torrent(row)

        if download:
            dest = ensure_folder(FOLDER_TORRENT)
            tf   = download_torrent_file(ih, session, dest, name)
            if tf:
                row['torrent_file'] = tf
                update_torrent_file_path(ih, tf)

        excerpt_files = r.get('excerpt_files', [])
        if excerpt_files:
            stored = store_excerpt_contents(ih, excerpt_files)
            if stored:
                hc = r.get('hidden_count', 0)
                suffix = f' (+{hc} hidden)' if hc else ''
                print(f'          excerpt: {stored} files{suffix} — {name[:50]}', flush=True)

        if is_new:
            hits.append(row)

    return hits


# ────────────────────────────────────────────────────────────────────────────
# Display
# ────────────────────────────────────────────────────────────────────────────

def fmt_size(s: str) -> str:
    try:
        b = int(s)
    except Exception:
        return s or '—'
    if b > 1_073_741_824: return f'{b/1_073_741_824:.2f} GB'
    if b > 1_048_576:     return f'{b/1_048_576:.2f} MB'
    if b > 1024:          return f'{b/1024:.2f} KB'
    return f'{b} B'


def print_hit(item: dict, idx: int) -> None:
    s    = item.get('seeders', -1)
    ss   = str(s) if s >= 0 else 'N/A'
    meth = item.get('scrape_method') or ''
    print(f'\n{"─"*72}')
    print(f'  [{idx:03d}] {item["name"][:70]}')
    print(f'        Seeds: {ss:<6} | Leechers: {item.get("leechers",-1) if item.get("leechers",-1) >= 0 else "N/A":<6} | '
          f'Size: {fmt_size(item.get("size_bytes","0"))}' +
          (f' | via {meth}' if meth else ''))
    print(f'        Keyword: [{item.get("keyword","")}]')
    print(f'        Hash: {item.get("info_hash","").upper()}')
    if item.get('torrent_file'):
        print(f'        .torrent: {item["torrent_file"]}')


# ────────────────────────────────────────────────────────────────────────────
# XLSX export
# ────────────────────────────────────────────────────────────────────────────

def export_xlsx() -> str:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    conn = sqlite3.connect(DB_NAME)
    cur  = conn.cursor()

    order_sql = 'COALESCE(seeders,-1) DESC, name'

    cols_t = ['info_hash','name','seeders','leechers','size_bytes','num_files',
              'category','source','keyword','added_utc','magnet','torrent_file',
              'captured_at','scrape_method']
    cur.execute(f'SELECT {",".join(cols_t)} FROM torrents ORDER BY {order_sql}')
    rows_t = cur.fetchall()

    cur.execute(f'''
        SELECT t.name, t.info_hash, t.seeders, t.keyword,
               COUNT(tc.id), COALESCE(SUM(tc.file_size),0),
               GROUP_CONCAT(tc.file_path, CHAR(10))
        FROM torrents t
        JOIN torrent_contents tc ON tc.info_hash = t.info_hash
        GROUP BY t.info_hash ORDER BY {order_sql}
    ''')
    rows_c = cur.fetchall()
    conn.close()

    def _sample10(s):
        if not s: return ''
        lines = s.split('\n')
        return '\n'.join(lines[:10]) + (f'\n... (+{len(lines)-10} more)' if len(lines) > 10 else '')

    def _fmt_seeds(v):
        return 'N/A' if (v is None or v < 0) else v

    wb  = Workbook()
    ws1 = wb.active; ws1.title = 'Results'
    ws2 = wb.create_sheet('Torrent_Contents')

    thin   = Side(style='thin', color='AAAAAA')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill('solid', fgColor='2E4057')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)

    def _hdr(ws, cols):
        ws.append(cols)
        for cell in ws[1]:
            cell.fill = hdr_fill; cell.font = hdr_font
            cell.alignment = Alignment(horizontal='center', wrap_text=True)
            cell.border = border
        ws.row_dimensions[1].height = 30

    # Sheet 1: Results
    _hdr(ws1, cols_t)
    s_idx = cols_t.index('seeders')
    l_idx = cols_t.index('leechers')
    for row in rows_t:
        dr = list(row)
        dr[s_idx] = _fmt_seeds(dr[s_idx])
        dr[l_idx] = _fmt_seeds(dr[l_idx])
        ws1.append(dr)
        r    = ws1.max_row
        sv   = row[s_idx]
        bold = sv is not None and sv > 0
        for cell in ws1[r]:
            cell.font = Font(bold=bold, size=9)
            cell.border = border; cell.alignment = Alignment(wrap_text=False)
    for i, w in enumerate([42,45,8,8,12,8,14,8,32,18,60,50,18,10], 1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    if rows_t:
        t1 = Table(displayName='Results',
                   ref=f'A1:{get_column_letter(len(cols_t))}{len(rows_t)+1}')
        t1.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        ws1.add_table(t1)
    ws1.freeze_panes = 'A2'

    # Sheet 2: Contents
    cols_c = ['torrent_name','info_hash','seeders','keyword',
              'num_files','total_size_gb','file_sample_10']
    _hdr(ws2, cols_c)
    for row in rows_c:
        name, ih, seeds, kw, nf, tb, fsample = row
        tb_gb  = round((tb or 0) / 1_073_741_824, 3)
        sample = _sample10(fsample)
        ws2.append([name, ih, _fmt_seeds(seeds), kw, nf, tb_gb, sample])
        r     = ws2.max_row
        bold  = seeds is not None and seeds > 0
        for cell in ws2[r]:
            cell.font = Font(bold=bold, size=9); cell.border = border
        ws2.row_dimensions[r].height = min(15 * (sample.count('\n') + 1), 180)
        ws2[f'G{r}'].alignment = Alignment(wrap_text=True, vertical='top')
    for i, w in enumerate([45,42,8,32,12,14,70], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    if rows_c:
        t2 = Table(displayName='Contents',
                   ref=f'A1:{get_column_letter(len(cols_c))}{len(rows_c)+1}')
        t2.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        ws2.add_table(t2)
    ws2.freeze_panes = 'A2'

    today     = datetime.date.today().strftime('%Y-%m-%d')
    xlsx_name = f'{today}_Results.xlsx'
    wb.save(xlsx_name)
    return xlsx_name


# ────────────────────────────────────────────────────────────────────────────
# Read keywords
# ────────────────────────────────────────────────────────────────────────────

def read_keywords(filepath: str) -> list:
    keywords = []
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                keywords.append(line)
    return keywords


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='torrent_search.py v2 — BTDig + UDP/WSS peer scrape + XLSX export',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic search, automatic scrape (UDP→WSS fallback)
  python torrent_search.py keywords.txt

  # WSS only (UDP-blocked network)
  python torrent_search.py keywords.txt --scrape-method wss

  # No scrape (fastest — peer count always N/A)
  python torrent_search.py keywords.txt --scrape-method none

  # Resume interrupted run
  python torrent_search.py keywords.txt --resume

  # Multiple keyword files in one run
  python torrent_search.py keywords1.txt keywords2.txt --resume

  # Show browser window (debug mode)
  python torrent_search.py keywords.txt --browser-visible
'''
    )
    parser.add_argument('files', nargs='+',
        help='Keyword files (UTF-8, one term per line, # for comments)')
    parser.add_argument('--btdig-pages', type=int, default=15,
        help='BTDig pages per keyword, 10 results/page (default: 15)')
    parser.add_argument('--delay-btdig', type=float, default=1.5,
        help='Seconds between BTDig pages (default: 1.5)')
    parser.add_argument('--delay-btdig-keyword', type=float, default=3.0,
        help='Pause between keywords in seconds (default: 3.0)')
    parser.add_argument('--download', action='store_true',
        help='Download .torrent file for each result')
    parser.add_argument('--scrape-method', default='auto',
        choices=['auto', 'udp', 'wss', 'none', 'dht'],
        help='Peer scrape method (default: auto). '
             'auto = DHT first (real peers via BEP5), then UDP/WSS tracker fallback; '
             'dht = DHT only (BEP5 peers + full file list via BEP9 metadata); '
             'udp/wss = tracker scrape only; none = skip.')
    parser.add_argument('--scrape-timeout', type=float, default=5.0,
        help='Peer scrape timeout in seconds (default: 5.0)')
    parser.add_argument('--resume', action='store_true',
        help='Resume from last checkpoint (skip already-processed keywords)')
    parser.add_argument('--reset-checkpoint', action='store_true',
        help='Delete checkpoint and reprocess all keywords from scratch')
    parser.add_argument('--browser-visible', action='store_true',
        help='Show browser window (debug mode)')
    args = parser.parse_args()

    init_folders()
    init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # Checkpoint
    if args.reset_checkpoint and os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print('[*] Checkpoint deleted.')
    btdig_done = checkpoint_load() if args.resume else set()
    if btdig_done:
        print(f'[*] Checkpoint: {len(btdig_done)} keywords already done, skipping.')

    # Keywords
    all_keywords = []
    for kf in args.files:
        kws = read_keywords(kf)
        all_keywords.extend(kws)
        print(f'[+] {kf}: {len(kws)} keywords')
    all_keywords = list(dict.fromkeys(all_keywords))   # deduplicate, preserve order

    headless = not args.browser_visible

    print(f'\n[*] Total keywords       : {len(all_keywords)}')
    print(f'[*] BTDig pages/keyword  : {args.btdig_pages}')
    print(f'[*] Peer scrape method   : {args.scrape_method}')
    print(f'[*] Scrape timeout       : {args.scrape_timeout:.1f}s')
    print(f'[*] Download .torrent    : {"Yes" if args.download else "No"}')
    print(f'[*] Delay between kws    : {args.delay_btdig_keyword:.1f}s\n')

    total_new = 0
    all_hits  = []

    interrupted = False
    try:
        for i, kw in enumerate(all_keywords, 1):
            print(f'[{i:03d}/{len(all_keywords):03d}] "{kw}"', flush=True)

            if kw in btdig_done:
                print('         BTDig: [checkpoint] already processed, skipping.', flush=True)
                continue

            btdig_raw = search_btdig_browser(
                kw,
                max_pages = args.btdig_pages,
                delay     = args.delay_btdig,
                headless  = headless,
            )

            if btdig_raw is None:
                print('         BTDig: [connection lost] use --resume to continue.', flush=True)
                continue

            print(f'         BTDig: {len(btdig_raw)} total results', flush=True)

            if btdig_raw:
                hits = process_btdig_results(
                    btdig_raw, kw, session,
                    download       = args.download,
                    scrape_method  = args.scrape_method,
                    scrape_timeout = args.scrape_timeout,
                )
                for h in hits:
                    all_hits.append(h)
                    total_new += 1
                    print_hit(h, len(all_hits))

            btdig_done.add(kw)
            checkpoint_save(btdig_done)

            try:
                export_xlsx()
            except Exception as _e:
                logging.debug('XLSX incremental: %s', _e)

            time.sleep(args.delay_btdig_keyword * random.uniform(0.8, 1.2))
    except KeyboardInterrupt:
        interrupted = True
        print('\n[!] Interrupted — finalising with results captured so far...', flush=True)

    # ── Final summary (runs on completion AND on Ctrl+C) ─────────────────────
    print(f'\n{"="*72}')
    print(f'  SUMMARY')
    print(f'  New records in DB : {total_new}')
    print(f'  Total hits        : {len(all_hits)}')
    if all_hits:
        top = sorted(
            [h for h in all_hits if h.get('seeders', -1) >= 0],
            key=lambda x: x['seeders'], reverse=True
        )[:10]
        if top:
            print(f'\n  Top 10 by seeders:')
            for idx2, item in enumerate(top, 1):
                meth = f'[{item.get("scrape_method","")}]' if item.get('scrape_method') else ''
                print(f'    {idx2:2d}. [{item["seeders"]:>5}s] {meth} {item["name"][:60]}')

    xlsx_file = export_xlsx()
    print(f'\n[+] XLSX      : {xlsx_file}')
    print(f'[+] Database  : {DB_NAME}')
    print(f'[+] Torrents  : {FOLDER_TORRENT}/')
    print(f'[+] Magnets   : {FOLDER_MAGNET}/')

    pending = [kw for kw in all_keywords if kw not in btdig_done]
    if pending:
        print(f'\n[!] {len(pending)} keywords pending. Use --resume to continue.')

    sys.exit(130 if interrupted else 0)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n[!] Interrupted. Use --resume to continue.', file=sys.stderr)
        sys.exit(130)
    except BrowserUnavailable as e:
        print(f'\n[X] {e}', file=sys.stderr)
        sys.exit(3)
