# iMicobo Sing — adding a song

Each song is **one folder** under `beta/songs/`. The folder name is the song's id
(lowercase + underscores, e.g. `as_time_goes_by`).

Inside the folder, use these **exact filenames**:

```
beta/songs/as_time_goes_by/
  title.txt       ← the display title, e.g.  As Time Goes By   (one line; optional)
  backing.mp3     ← the instrumental you sing over
  original.mp3    ← the full track with vocals (preview + "Hear original")
  pitch.json      ← the scoring answer key (vocal melody notes)
  lyrics.lrc      ← timed lyrics that scroll on screen
```

A song appears in the game only when **all four** required files are present
(`backing.mp3`, `original.mp3`, `pitch.json`, `lyrics.lrc`). `title.txt` is
optional — without it the title falls back to a `"song"` field in `pitch.json`,
then to the folder name.

**Important:** `backing.mp3` and `original.mp3` must share the **exact same
timeline** (same length / tempo) — the "Hear original" button swaps between them
at the current position.

> The old `<name>-instrument.mp3` / `-original.mp3` / `-pitch.json` / `-lyrics.lrc`
> naming still works as a fallback, so existing songs keep playing. New songs
> should use the fixed names above.

---

## pitch.json format

The melody as a list of notes (made from the isolated **vocal**, via basic-pitch →
MIDI → `pitch_convert.py`):

```json
{
  "offset": 0.0,
  "notes": [
    { "t": 0.50, "dur": 0.50, "midi": 60 },
    { "t": 1.00, "dur": 0.75, "midi": 62 }
  ]
}
```
- `t` seconds from the top · `dur` seconds · `midi` 60 = middle C (Hz > 127 auto-converts)
- `offset` (optional) shifts ALL notes if the melody is early/late.

## lyrics.lrc format

Standard LRC, one timestamp per line (blank `[mm:ss]` lines mark where a line ends):

```
[00:16.73]You must remember this
[00:18.47]
[00:18.67]A kiss is still a kiss
```
