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
import sqlite3, hmac, hashlib, base64
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



# ============================================================ accounts + paywall
APP_SECRET            = os.environ.get("APP_SECRET", "dev-insecure-change-me").encode()
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
PRICE_DAY     = os.environ.get("STRIPE_PRICE_DAY",     "price_1Ua1xhEOL6Jd9NbKzE2Ric2A")
PRICE_QUARTER = os.environ.get("STRIPE_PRICE_QUARTER", "price_1Ua1xhEOL6Jd9NbKz5YmPQ99")
# Google Sign-In — Client ID is public (safe in the page); no client secret needed
# for the ID-token flow. Verified server-side against Google's tokeninfo endpoint.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID",
    "218796516810-rjvhmkc177g7elicnh1f6616td6jrekr.apps.googleusercontent.com").strip()
PLAN_SECONDS  = {"day": 24*3600, "quarter": 90*24*3600}
FREE_SONGS    = int(os.environ.get("FREE_SONGS", "5"))   # advertised to the frontend
DB_FILE       = os.environ.get("DB_FILE", "imicobo.db")

_db = sqlite3.connect(DB_FILE, check_same_thread=False)
_db.execute("""CREATE TABLE IF NOT EXISTS grants(
    checkout_id TEXT PRIMARY KEY, email TEXT NOT NULL, product TEXT,
    expires_at REAL NOT NULL, created_at REAL NOT NULL)""")
_db.execute("""CREATE TABLE IF NOT EXISTS consents(
    email TEXT PRIMARY KEY, opted_in INTEGER, updated_at REAL)""")
_db.commit()
_db_lock = threading.Lock()

def grant_access(email, product, checkout_id, seconds):
    now = time.time()
    with _db_lock:
        _db.execute("INSERT OR IGNORE INTO grants(checkout_id,email,product,expires_at,created_at)"
                    " VALUES(?,?,?,?,?)", (checkout_id, email.lower(), product, now+seconds, now))
        _db.commit()

def entitlement(email):
    if not email: return (False, 0)
    with _db_lock:
        row = _db.execute("SELECT MAX(expires_at) FROM grants WHERE email=?", (email.lower(),)).fetchone()
    until = row[0] if row and row[0] else 0
    return (until > time.time(), until)

def set_consent(email, opted_in):
    with _db_lock:
        _db.execute("INSERT INTO consents(email,opted_in,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(email) DO UPDATE SET opted_in=excluded.opted_in, updated_at=excluded.updated_at",
                    (email.lower(), 1 if opted_in else 0, time.time()))
        _db.commit()

# signed session cookie — identity only; entitlement ALWAYS comes from the DB
def _b64(b):  return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _unb64(s):return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def make_session(email):
    payload = _b64(json.dumps({"email": email.lower(), "iat": int(time.time())}).encode())
    sig = hmac.new(APP_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig
def read_session(cookie):
    if not cookie or "." not in cookie: return None
    payload, _, sig = cookie.partition(".")
    good = hmac.new(APP_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig): return None
    try:    return json.loads(_unb64(payload)).get("email")
    except Exception: return None

# Stripe REST, stdlib only (no SDK dependency)
def stripe_request(method, path, params=None):
    url = "https://api.stripe.com/v1/" + path
    data = None
    if method == "POST" and params:
        data = urllib.parse.urlencode(params).encode()
    elif method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + STRIPE_SECRET_KEY)
    if data: req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=12) as rsp:
        return json.load(rsp)

def verify_google_token(cred):
    """Verify a Google ID token (JWT) via Google's tokeninfo endpoint, which
    checks the signature and expiry for us. Returns a verified lowercased email
    or None. Keeps the relay dependency-free (no crypto library needed)."""
    if not cred or not GOOGLE_CLIENT_ID:
        return None
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": cred})
        req = urllib.request.Request(url, headers={"User-Agent": "imicobo/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            info = json.load(r)
    except Exception:
        return None
    if info.get("aud") != GOOGLE_CLIENT_ID:
        return None
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return None
    if str(info.get("email_verified")).lower() not in ("true", "1"):
        return None
    email = (info.get("email") or "").strip().lower()
    return email or None

def stripe_webhook_ok(raw_bytes, sig_header):
    if not STRIPE_WEBHOOK_SECRET or not sig_header: return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1: return False
    signed = t.encode() + b"." + raw_bytes
    good = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, v1)


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

    def _json_cookie(self, obj, cookie, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name: return v
        return None

    def _session_email(self):
        return read_session(self._cookie("imb_session"))

    def _base_url(self):
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto}://{self.headers.get('Host', 'localhost')}"

    def _redirect(self, location, set_cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie: self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _session_set_cookie(self, email):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return (f"imb_session={make_session(email)}; Path=/; Max-Age=15552000; "
                f"HttpOnly; SameSite=Lax{secure}")

    def do_OPTIONS(self):
        self._json({"ok": True})

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        # ---- accounts / paywall ----
        if p == "/api/me":
            email = self._session_email()
            ok, until = entitlement(email)
            return self._json({"entitled": ok, "until": until,
                               "email": email, "free_songs": FREE_SONGS,
                               "google_client_id": GOOGLE_CLIENT_ID})

        # Stripe returns the paid user here; confirm payment, unlock this browser.
        if p == "/paid":
            sid = self._q(u, "session_id")
            if not sid or not STRIPE_SECRET_KEY:
                return self._redirect(self._base_url() + "/?checkout=error")
            try:
                s = stripe_request("GET", "checkout/sessions/" + sid)
            except Exception:
                return self._redirect(self._base_url() + "/?checkout=error")
            if s.get("payment_status") == "paid":
                email = ((s.get("customer_details") or {}).get("email") or "").lower()
                product = (s.get("metadata") or {}).get("product", "day")
                if email:
                    grant_access(email, product, sid, PLAN_SECONDS.get(product, 86400))
                    if (s.get("metadata") or {}).get("consent") == "1":
                        set_consent(email, True)
                    return self._redirect(self._base_url() + "/?welcome=1",
                                          self._session_set_cookie(email))
            return self._redirect(self._base_url() + "/?checkout=incomplete")

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

        # ---- universal-link plumbing (smart QR) ----
        # Apple fetches this to learn the app owns imicobo.com/j links.
        if p in ("/.well-known/apple-app-site-association", "/apple-app-site-association"):
            try:
                with open("aasa.json", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers(); self.wfile.write(body); return
            except Exception:
                return self._json({"error": "aasa missing"}, 404)

        # The QR points here. If the iOS app is installed, iOS opens it directly
        # (this page never loads). Otherwise this page routes to the App Store
        # (iPhone) or the browser mic (Android / desktop).
        if p == "/j":
            try:
                with open("join.html", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers(); self.wfile.write(body); return
            except Exception:
                return self._json({"error": "join page missing"}, 404)

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

        # pretty URLs for the legal pages (linked from the paywall + footer,
        # and required by the Google OAuth consent screen)
        LEGAL = {"/privacy-policy": "privacy.html", "/privacy": "privacy.html",
                 "/terms": "terms.html", "/terms-of-service": "terms.html"}
        if p in LEGAL:
            try:
                with open(LEGAL[p], "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers(); self.wfile.write(body); return
            except Exception:
                return self._json({"error": "page missing"}, 404)

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
        raw_bytes = self.rfile.read(n) if n else b""
        raw = raw_bytes.decode("utf-8", "replace")

        # ---- sign in with Google (ID-token flow) ----
        if p == "/api/auth/google":
            try:    body = json.loads(raw) if raw else {}
            except Exception: body = {}
            email = verify_google_token(body.get("credential"))
            if not email:
                return self._json({"error": "Sign-in failed."}, 401)
            ok, until = entitlement(email)
            return self._json_cookie(
                {"ok": True, "entitled": ok, "until": until, "email": email},
                self._session_set_cookie(email))

        # ---- sign out: clear the session cookie ----
        if p == "/api/logout":
            return self._json_cookie(
                {"ok": True},
                "imb_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

        # ---- paywall: create a Stripe Checkout session ----
        if p == "/api/checkout":
            if not STRIPE_SECRET_KEY:
                return self._json({"error": "Payments aren't set up yet."}, 503)
            try:    body = json.loads(raw) if raw else {}
            except Exception: body = {}
            plan = "quarter" if body.get("plan") == "quarter" else "day"
            price = PRICE_DAY if plan == "day" else PRICE_QUARTER
            base = self._base_url()
            params = {
                "mode": "payment",
                "line_items[0][price]": price,
                "line_items[0][quantity]": "1",
                "success_url": base + "/paid?session_id={CHECKOUT_SESSION_ID}",
                "cancel_url":  base + "/?checkout=cancel",
                "metadata[product]": plan,
                "metadata[consent]": "1" if body.get("consent") else "0",
                "allow_promotion_codes": "true",
            }
            # If signed in, tie the purchase to that account's email.
            signed_in = self._session_email()
            if signed_in:
                params["customer_email"] = signed_in
            try:
                s = stripe_request("POST", "checkout/sessions", params)
                return self._json({"url": s["url"]})
            except Exception as e:
                return self._json({"error": "Couldn't start checkout.", "detail": str(e)}, 502)

        # ---- paywall: Stripe webhook (reliable backstop for granting access) ----
        if p == "/api/stripe/webhook":
            if not stripe_webhook_ok(raw_bytes, self.headers.get("Stripe-Signature", "")):
                return self._json({"error": "bad signature"}, 400)
            try:    event = json.loads(raw)
            except Exception: return self._json({"error": "bad json"}, 400)
            if event.get("type") == "checkout.session.completed":
                s = event["data"]["object"]
                email = ((s.get("customer_details") or {}).get("email") or "").lower()
                product = (s.get("metadata") or {}).get("product", "day")
                if email and s.get("payment_status") == "paid":
                    grant_access(email, product, s.get("id", "wh_%f" % time.time()),
                                 PLAN_SECONDS.get(product, 86400))
                    if (s.get("metadata") or {}).get("consent") == "1":
                        set_consent(email, True)
            return self._json({"received": True})

        # screen opens / resumes a room
        if p == "/session":
            want = self._code(u)
            _ips = lan_ips()
            host = f"{_ips[0]}:{PORT}" if _ips else None
            with lock:
                now = time.time()
                for c in [c for c, s in sessions.items() if now - s["created"] > SESSION_TTL]:
                    sessions.pop(c, None)
                # Resume a remembered code (fresh, empty roster for a reload),
                # recreate it if the server forgot it, else mint a new one.
                if want and want in sessions:
                    sessions[want] = {"created": now, "mics": {}}
                    return self._json({"code": want, "digits": CODE_DIGITS,
                                       "max": MAX_MICS, "host": host, "resumed": True})
                if want and len(want) == CODE_DIGITS and want.isdigit():
                    sessions[want] = {"created": now, "mics": {}}
                    return self._json({"code": want, "digits": CODE_DIGITS,
                                       "max": MAX_MICS, "host": host, "recreated": True})
                lo, hi = 10 ** (CODE_DIGITS - 1), 10 ** CODE_DIGITS - 1
                code = str(random.randint(lo, hi))
                for _ in range(50):
                    if code not in sessions:
                        break
                    code = str(random.randint(lo, hi))
                sessions[code] = {"created": now, "mics": {}}
                return self._json({"code": code, "digits": CODE_DIGITS, "max": MAX_MICS, "host": host})

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
