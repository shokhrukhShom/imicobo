# iMicobo 🎤 — *Sing your favorite karaoke online!*

A free, no-account karaoke web app. Netflix-style rows of songs, a search bar,
and your iPhone (or any mic) as the microphone. It hosts **nothing** — every
song plays through YouTube's official embedded player.

```
index.html     the karaoke app (song grid, search, player, recent)
connect.html   the iPhone-mic pairing screen (your original receiver)
qr.js          QR encoder used by connect.html
relay.py       tiny Python server: mic pairing + karaoke lookup + cache
```

## Run it locally

```bash
cd imicobo
python3 relay.py
# open http://localhost:8080/
```

Zero dependencies — just Python 3. It runs **with or without** a YouTube key
(see below). Without a key, songs open on YouTube instead of playing in-app.

## The one thing to set up: a free YouTube key

To play a chosen song *inside* the app, the server looks it up once via the
free **YouTube Data API v3**, then caches the answer in `yt_cache.json` and
serves it to everyone for free forever. Getting the key is free and ~5 minutes:

1. Go to <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **Credentials → Create credentials → API key.** Copy it.
4. Run the server with the key:

```bash
YT_API_KEY=YOUR_KEY_HERE python3 relay.py
```

The key stays on the **server** — it is never exposed in the browser.

### Why the cache matters (staying free)

YouTube's free tier is ~10,000 quota units/day and a search costs 100. Because
every lookup is cached on disk and reused for all visitors, your finite song
library warms up once and then costs ~0. Only brand-new searches spend quota.

## Deploy for ~$5/mo

Any tiny Linux VPS works (DigitalOcean/Hetzner/Lightsail, 1 vCPU / 512 MB):

```bash
scp -r imicobo/ user@your-server:/opt/imicobo
ssh user@your-server
cd /opt/imicobo
sudo apt install -y python3
# keep it running + auto-restart with systemd:
```

`/etc/systemd/system/imicobo.service`:

```ini
[Unit]
Description=iMicobo
After=network.target
[Service]
WorkingDirectory=/opt/imicobo
Environment=YT_API_KEY=YOUR_KEY_HERE
Environment=PORT=8080
ExecStart=/usr/bin/python3 relay.py
Restart=always
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now imicobo
```

Then point **imicobo.com** at the server and put **Caddy** or **nginx** in
front for HTTPS (needed: browsers only allow microphone access over `https://`).
Minimal Caddy:

```
imicobo.com {
    reverse_proxy localhost:8080
}
```

## Customize

- **Songs:** edit the `CATALOG` object at the top of the `<script>` in
  `index.html`. Just titles + artists — no video IDs to maintain.
- **Buy Me a Coffee:** change the link on the `#coffee` element in the navbar
  (currently `buymeacoffee.com/imicobo`).
- **Genres:** add/rename keys in `CATALOG`; rows render automatically.

## Notes / good to know

- Some YouTube videos disable embedding — the player catches that and opens the
  song on YouTube instead, so nothing dead-ends.
- Mic access, camera, and WebRTC all require **HTTPS** in production.
- This builds on YouTube's embed permission, not a license you own. Fine for a
  free MVP; keep the audio source swappable if you ever scale or monetize.
