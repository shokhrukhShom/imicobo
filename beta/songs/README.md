# iMicobo Sing — adding a song

Each song is **one folder** under `beta/songs/`. The folder name is the song's id
(use lowercase + underscores, e.g. `as_time_goes_by`).

A song only appears in the game when the folder has **all four** files. The scanner
matches files by their **suffix**, so the part before the suffix can be anything
(keep it consistent for tidiness):

```
beta/songs/as_time_goes_by/
  As_Time_Goes_By_Jacques_Renard-instrument.mp3   ← backing track you sing over
  As_Time_Goes_By_Jacques_Renard-original.mp3     ← full track w/ vocals (preview + reference)
  As_Time_Goes_By_Jacques_Renard-pitch.json       ← the scoring answer key (melody notes)
  As_Time_Goes_By_Jacques_Renard-lyrics.lrc       ← timed lyrics that scroll on screen
```

Required suffixes (exact):
| file      | suffix              |
|-----------|---------------------|
| instrument| `-instrument.mp3`   |
| original  | `-original.mp3`     |
| pitch     | `-pitch.json`       |
| lyrics    | `-lyrics.lrc`       |

**Important:** `original.mp3` and `instrument.mp3` must share the **exact same
timeline** (same length / tempo), because the "Hear original" button swaps between
them at the current playback position.

---

## pitch.json format

The scoring answer key — the melody as a list of notes:

```json
{
  "song": "As Time Goes By",
  "offset": 0.0,
  "notes": [
    { "t": 0.50, "dur": 0.50, "midi": 60, "lyric": "You" },
    { "t": 1.00, "dur": 0.75, "midi": 62, "lyric": "must" }
  ]
}
```

- `t`    — note start, seconds from the top of the track
- `dur`  — note length in seconds
- `midi` — pitch as a MIDI number (60 = middle C, 69 = A440). If you accidentally
           store a frequency in Hz (> 127) it's auto-converted.
- `lyric`— optional, the syllable (not used for display yet; `.lrc` drives lyrics)
- `offset` (top level, optional) — shift ALL notes by this many seconds if the
           melody is early/late against the backing track.
- `song` / `title` (optional) — display name; otherwise the folder name is used.

The loader is forgiving about key names — it also accepts `time/start` for `t`,
`duration/len` for `dur`, and `pitch/note` for `midi`.

## lyrics.lrc format

Standard LRC — one timestamp per line:

```
[00:00.50]You must remember this
[00:04.20]A kiss is just a kiss
```

`[mm:ss.xx]` — minutes : seconds . hundredths.
