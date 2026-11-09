#!/usr/bin/env python3
"""
iMicobo relay + karaoke backend (zero third-party dependencies).

Two jobs in one tiny server, so a $5 box runs the whole thing:

1) MIC PAIRING — multi-mic. A screen opens a ROOM (one code). Up to 6 phones
   join the same code; each gets its own mic id and does its own WebRTC
   handshake with the screen. The screen mixes every mic into one output.
   The audio never touches this server — only the little handshake blobs do.

2) KARAOKE LOOKUP — turn a song title into a playable YouTube video, resolved
   ONCE via the free YouTube Data API and cached on disk so every future
   visitor is served for free. That cache is what keeps us inside the free
   daily quota.

Run from this folder:
    YT_API_KEY=your_free_key  python3 relay.py

No key? The server still runs — /search and /resolve just report `nokey`,
and the frontend falls back to opening YouTube directly. Nothing breaks.
"""
import json, os, random, socket, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "8080"))

# Your free YouTube Data API v3 key. Kept SERVER-SIDE on purpose so it is never
# exposed in the browser. See README for the 5-minute, no-cost setup.
YT_API_KEY = os.environ.get("YT_API_KEY", "").strip()

# ---- mic pairing config ----
CODE_DIGITS = 6                # 6 = 1,000,000 combos (safe on the public relay)
SESSION_TTL = 15 * 60          # abandoned rooms expire
MAX_MICS = 6                   # how many phones can share one screen

# Brute-force guard: cap how many /join attempts one IP can make per minute.
# Even a short code is unbreakable when guesses are throttled this hard.
JOIN_LIMIT_PER_MIN = 30
join_hits = {}                 # ip -> [recent attempt timestamps]
join_lock = threading.Lock()

# ---- karaoke cache ----
CACHE_FILE = os.environ.get("CACHE_FILE", "yt_cache.json")
CACHE_TTL  = 30 * 24 * 3600    # re-verify a cached video after ~30 days

# A room is: {"created": ts, "mics": { mic_id: {name, offer, answer, created} }}
sessions = {}
cache = {}                     # "resolve:<q>" / "search:<q>" -> {"v": data, "t": ts}
lock = threading.Lock()
cache_lock = threading.Lock()


# ---------------------------------------------------------------- cache helpers
def load_cache():
    global cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}


def save_cache():
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def cache_get(key):
    with cache_lock:
        e = cache.get(key)
        if not e:
            return None
        if time.time() - e.get("t", 0) > CACHE_TTL:
            cache.pop(key, None)
            return None
        return e.get("v")


def cache_put(key, value):
    with cache_lock:
        cache[key] = {"v": value, "t": time.time()}
    save_cache()


# ---------------------------------------------------------------- youtube calls
def yt_search(query, limit=8):
    """Return a list of {id,title,channel,thumb} for a karaoke search."""
    q = query if "karaoke" in query.lower() else query + " karaoke"
    params = urllib.parse.urlencode({
        "part": "snippet", "type": "video", "videoEmbeddable": "true",
        "maxResults": str(limit), "q": q, "key": YT_API_KEY,
    })
    url = "https://www.googleapis.com/youtube/v3/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "imicobo/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    out = []
    for it in data.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        thumbs = sn.get("thumbnails") or {}
        thumb = ((thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""))
        out.append({"id": vid, "title": sn.get("title", ""),
                    "channel": sn.get("channelTitle", ""), "thumb": thumb})
    return out


def itunes_art(title, artist):
    """Free album artwork from Apple's iTunes Search API — no key, no quota."""
    term = (artist + " " + title).strip()
    params = urllib.parse.urlencode({"term": term, "entity": "song",
                                     "limit": "1", "country": "US"})
    url = "https://itunes.apple.com/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "imicobo/1.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.load(r)
    items = data.get("results") or []
    if not items:
        return None
    art = items[0].get("artworkUrl100") or items[0].get("artworkUrl60")
    if not art:
        return None
    return art.replace("100x100bb", "400x400bb").replace("60x60bb", "400x400bb")


# ---------------------------------------------------------------- room helpers
def _assign_mic_id(room):
    """Smallest free id 1..MAX_MICS as a string, or None if the room is full."""
    used = set(room["mics"].keys())
    for i in range(1, MAX_MICS + 1):
        if str(i) not in used:
            return str(i)
    return None


def _roster(room):
    """Public view the screen polls: who's here and whether their offer is ready."""
    return [{"id": mid, "name": m.get("name", ""),
             "hasOffer": m.get("offer") is not None}
            for mid, m in sorted(room["mics"].items(), key=lambda kv: int(kv[0]))]


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _q(self, u, name, default=""):
        return parse_qs(u.query).get(name, [default])[0]

    def _code(self, u):
        return self._q(u, "code")

    def _client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def do_OPTIONS(self):
        self._json({"ok": True})

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        # ---- karaoke: resolve one song title -> one playable video id ----
        if p == "/resolve":
            q = self._q(u, "q").strip()
            if not q:
                return self._json({"error": "missing q"}, 400)
            if not YT_API_KEY:
                return self._json({"nokey": True})
            key = "resolve:" + q.lower()
            hit = cache_get(key)
            if hit is not None:
                return self._json({"id": hit, "cached": True})
            try:
                res = yt_search(q, limit=1)
                vid = res[0]["id"] if res else None
                cache_put(key, vid)
                return self._json({"id": vid})
            except Exception as e:
                return self._json({"error": "youtube unavailable", "detail": str(e)}, 502)

        # ---- karaoke: full search results for the search bar ----
        if p == "/search":
            q = self._q(u, "q").strip()
            if not q:
                return self._json({"results": []})
            if not YT_API_KEY:
                return self._json({"results": [], "nokey": True})
            key = "search:" + q.lower()
            hit = cache_get(key)
            if hit is not None:
                return self._json({"results": hit, "cached": True})
            try:
                res = yt_search(q, limit=10)
                cache_put(key, res)
                return self._json({"results": res})
            except Exception as e:
                return self._json({"results": [], "error": str(e)}, 502)

        # ---- karaoke: album artwork for a card (free, no key) ----
        if p == "/art":
            t = self._q(u, "t").strip()
            a = self._q(u, "a").strip()
            if not t:
                return self._json({"url": None})
            ck = "art:" + (t + "|" + a).lower()
            hit = cache_get(ck)
            if hit is not None:
                return self._json({"url": hit or None, "cached": True})
            try:
                art = itunes_art(t, a)
            except Exception:
                art = None
            cache_put(ck, art or "")
            return self._json({"url": art})

        # ---- mic pairing ----
        if p == "/ping":
            return self._json({"app": "imicobo", "version": 2,
                               "digits": CODE_DIGITS, "maxMics": MAX_MICS})

        # screen polls the room: who's connected + whose offer is ready
        if p == "/session":
            code = self._code(u)
            with lock:
                room = sessions.get(code)
                if not room:
                    return self._json({"active": False})
                room["created"] = time.time()        # heartbeat: open screen never expires
                return self._json({"active": True, "max": MAX_MICS,
                                   "mics": _roster(room)})

        # one mic's offer (screen reads) / answer (phone reads)
        if p in ("/offer", "/answer"):
            code = self._code(u)
            mic = self._q(u, "mic") or "1"
            with lock:
                room = sessions.get(code)
                m = room["mics"].get(mic) if room else None
                return self._json({"sdp": m.get(p.strip("/")) if m else None})

        # ---- static files (index.html, connect.html, qr.js, ...) ----
        name = p.lstrip("/") or "index.html"
        name = os.path.normpath(name)
        ALLOWED = (".html", ".js", ".css", ".svg", ".png", ".ico", ".jpg", ".webmanifest")
        if (name.startswith("..") or os.path.isabs(name) or "/" in name
                or not name.endswith(ALLOWED)):
            return self._json({"error": "not found"}, 404)
        if os.path.isfile(name):
            ctype = ("text/html" if name.endswith(".html")
                     else "application/javascript" if name.endswith(".js")
                     else "text/css" if name.endswith(".css")
                     else "image/svg+xml" if name.endswith(".svg")
                     else "image/x-icon" if name.endswith(".ico")
                     else "image/png" if name.endswith(".png")
                     else "image/jpeg" if name.endswith(".jpg")
                     else "application/manifest+json" if name.endswith(".webmanifest")
                     else "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(name, "rb") as f:
                self.wfile.write(f.read())
            return
        self._json({"error": "not found"}, 404)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else ""

        # screen opens / resumes a room
        if p == "/session":
            want = self._code(u)
            with lock:
                now = time.time()
                for c in [c for c, s in sessions.items() if now - s["created"] > SESSION_TTL]:
                    sessions.pop(c, None)
                # Resume a remembered code (fresh, empty roster for a reload),
                # recreate it if the server forgot it, else mint a new one.
                if want and want in sessions:
                    sessions[want] = {"created": now, "mics": {}}
                    return self._json({"code": want, "digits": CODE_DIGITS,
                                       "max": MAX_MICS, "resumed": True})
                if want and len(want) == CODE_DIGITS and want.isdigit():
                    sessions[want] = {"created": now, "mics": {}}
                    return self._json({"code": want, "digits": CODE_DIGITS,
                                       "max": MAX_MICS, "recreated": True})
                lo, hi = 10 ** (CODE_DIGITS - 1), 10 ** CODE_DIGITS - 1
                code = str(random.randint(lo, hi))
                for _ in range(50):
                    if code not in sessions:
                        break
                    code = str(random.randint(lo, hi))
                sessions[code] = {"created": now, "mics": {}}
                return self._json({"code": code, "digits": CODE_DIGITS, "max": MAX_MICS})

        # a phone joins the room -> gets its own mic id (or "party full")
        if p == "/join":
            ip = self._client_ip()
            now = time.time()
            with join_lock:
                hits = [t for t in join_hits.get(ip, []) if now - t < 60]
                if len(hits) >= JOIN_LIMIT_PER_MIN:
                    join_hits[ip] = hits
                    return self._json({"ok": False,
                                       "error": "Too many attempts. Please wait a minute."}, 429)
                hits.append(now)
                join_hits[ip] = hits
            code = self._code(u)
            name = self._q(u, "name")[:24]
            with lock:
                room = sessions.get(code)
                if not room:
                    return self._json({"ok": False, "error": "That code isn't valid."}, 404)
                mic = _assign_mic_id(room)
                if mic is None:
                    return self._json({"ok": False,
                                       "error": f"This screen is full ({MAX_MICS} singers)."}, 409)
                room["mics"][mic] = {"name": name, "offer": None,
                                     "answer": None, "created": now}
                return self._json({"ok": True, "mic": mic,
                                   "count": len(room["mics"]), "max": MAX_MICS})

        # a mic posts its offer (phone) or answer (screen)
        if p in ("/offer", "/answer"):
            code = self._code(u)
            mic = self._q(u, "mic") or "1"
            with lock:
                room = sessions.get(code)
                m = room["mics"].get(mic) if room else None
                if not m:
                    return self._json({"ok": False}, 404)
                m[p.strip("/")] = raw
            return self._json({"ok": True})

        # a phone leaves -> free just its slot; others stay connected
        if p == "/leave":
            code = self._code(u)
            mic = self._q(u, "mic")
            with lock:
                room = sessions.get(code)
                if room and mic in room["mics"]:
                    room["mics"].pop(mic, None)
            return self._json({"ok": True})

        # screen tears the whole room down (or one mic if ?mic= given)
        if p == "/cancel":
            code = self._code(u)
            mic = self._q(u, "mic")
            with lock:
                if mic:
                    room = sessions.get(code)
                    if room:
                        room["mics"].pop(mic, None)
                else:
                    sessions.pop(code, None)
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def lan_ips():
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); found.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    ok = lambda i: not (i.startswith("127.") or i.startswith("192.0.0.")
                        or i.startswith("169.254."))
    rank = lambda i: 0 if i.startswith("10.") else (1 if i.startswith("192.168.") else 2)
    return sorted(set(i for i in found if ok(i)), key=rank)


if __name__ == "__main__":
    load_cache()
    ips = lan_ips()
    print("\n  iMicobo — karaoke + multi-mic relay")
    print("  ───────────────────────────────────")
    print(f"  Open on this computer / TV:  http://localhost:{PORT}/")
    if ips:
        print(f"  Or from another device:      http://{ips[0]}:{PORT}/")
    print(f"  Up to {MAX_MICS} phones can share one screen code.")
    print(f"  YouTube key: {'set ✓ (in-app search + playback)' if YT_API_KEY else 'NOT set — search opens YouTube instead'}")
    print(f"  Cache file:  {CACHE_FILE} ({len(cache)} entries)")
    print("\n  Ctrl-C to stop.\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
