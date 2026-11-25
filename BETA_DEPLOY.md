# Deploying the beta singing game (imicobo.com/beta)

Three parts: the new relay endpoint, a Caddy change, and your song folders.

## 1. relay.py
Deploy the updated `relay.py` and restart:
```
sudo systemctl restart imicobo
```
It adds one route: `GET /api/beta/songs` (scans `beta/songs/*/` for complete songs).

## 2. Caddy — serve /beta static files
Replace `/etc/caddy/Caddyfile` with the updated one (it adds a `handle /beta/*`
block so Caddy serves the game page + .mp3 files directly, with audio seeking):
```
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Why Caddy and not the relay: Caddy supports HTTP Range requests, which `<audio>`
needs to seek/scrub and to stream large mp3s. The relay's tiny file server does not.

## 3. Put the files on the box
```
/opt/imicobo/beta/index.html                 ← the game page
/opt/imicobo/beta/songs/<song_id>/           ← one folder per song (all 4 files)
```
See `beta/songs/README.md` for the exact file naming + pitch.json format.

For your first song:
```
/opt/imicobo/beta/songs/as_time_goes_by/
  As_Time_Goes_By_Jacques_Renard-instrument.mp3
  As_Time_Goes_By_Jacques_Renard-original.mp3
  As_Time_Goes_By_Jacques_Renard-pitch.json
  As_Time_Goes_By_Jacques_Renard-lyrics.lrc
```

## 4. Test
- Open `https://imicobo.com/beta` → your song appears.
- Tap **▶ Preview** → the original plays.
- Open the song → a 6-digit code shows. Pair your phone (iMicobo app or the
  "use this device as a mic" link).
- Tap **Start**, sing → score climbs, notes light up green when you're on pitch.
- Tap **🎧 Hear original** → scoring pauses, you hear the real vocal; tap again to
  resume from the same spot.

Beta is open/free — no paywall, no sign-in.

## Notes / knobs (top of the `<script>` in index.html)
- `TOL = 2.0` — semitones of slack to count as "on pitch". Raise to make it easier.
- `LAT = 0.12` — seconds of mic/network latency compensation. If scoring feels
  consistently early/late, nudge this. You can also set a per-song `offset` in
  pitch.json.
- Scoring is octave-free (singing an octave up/down still counts) so everyone can
  sing in their own range.
