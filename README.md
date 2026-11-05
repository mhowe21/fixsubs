# fixsubs

Fixes OCR mistakes in `.srt` subtitle files using AI. Use it after Subtitle Edit has OCR'd image subtitles (PGS/VobSub). It repairs the typical OCR errors (`1`/`l`/`|` instead of `I`, `rn` instead of `m`, missing apostrophes, lost accents, mangled `¿ ¡`, music notes misread as `J`/`f`/`JS`, and so on) and leaves the dialogue itself alone.

It uses **Claude through Claude Code**, so it runs on your **Claude Pro subscription**. No API key is needed. As a backup, it can use a **local model through LM Studio**.

---

## Where things are

| What | Location |
|---|---|
| The script (the one that runs) | `~/.local/bin/fixsubs` (full path: `/Users/mah/.local/bin/fixsubs`) |
| Reference copy of the script | `~/Documents/CodeProjects/fixsubs/fixsubs.py` (identical content; `.py` added so editors highlight it) |
| This README | `~/Documents/CodeProjects/fixsubs/README.md` |

**The reference copy is not the one that runs.** If you edit the script, edit `~/.local/bin/fixsubs`, then refresh the copy:
```bash
cp ~/.local/bin/fixsubs ~/Documents/CodeProjects/fixsubs/fixsubs.py
```
To check that the two are still identical: `cmp ~/.local/bin/fixsubs ~/Documents/CodeProjects/fixsubs/fixsubs.py` (no output means identical).
| Claude Code (required) | `/opt/homebrew/bin/claude`, which must be logged in to your Pro account |
| LM Studio CLI (optional) | `~/.lmstudio/bin/lms` |

`~/.local/bin` is on your PATH, so you can type `fixsubs` from any folder in Terminal.

The script is plain Python 3 and uses only the standard library, so there's nothing to install. It has no `.py` extension, but you can open it in any text editor. In Finder the folder is hidden: press **Cmd+Shift+G**, type `~/.local/bin`, and press Return.

---

## Quick start

**One file:**
```bash
fixsubs "/Volumes/SSK SSD/RAW MKV/Final Destination/Final Destination.srt"
```
Tip: type `fixsubs ` (with a trailing space), then drag the `.srt` from Finder into the Terminal window. That pastes the full path with the correct quoting.

**Several specific files** (for example every language for one movie):
```bash
fixsubs "Movie.srt" "Movie Spanish.srt" "Movie French.srt"
```

**Every `.srt` in a folder:**
```bash
fixsubs "/Volumes/SSK SSD/RAW MKV/Final Destination"
```

**Several folders at once:**
```bash
fixsubs "/Volumes/SSK SSD/RAW MKV/"*/
```

Files are processed one after another. A movie usually takes under a minute per language with Claude.

---

## What it produces

For `Movie.srt`, you end up with:

```
Movie.srt                     ← the FIXED subtitles (same name, so Jellyfin and Subtitle Edit pick it up)
fixsubs/Movie.original.srt    ← your untouched original, byte-for-byte
fixsubs/Movie.fixes.txt       ← a log of every change
```

The original and the log go in a `fixsubs` folder next to the subtitles, so the movie folder only holds the fixed `.srt`. Every subtitle in that folder shares the same `fixsubs` folder.

Older versions put `Movie.original.srt` and `Movie.fixes.txt` right next to `Movie.srt`. Those are still recognised, and they are moved into the `fixsubs` folder the next time you fix that file (for example with `--force`).

The log (`fixsubs/Movie.fixes.txt`) looks like this:
```
1874 cues, 391 fixed, 235 music note(s) restored, 1 rejected, 0 chunk(s) failed

== APPLIED ==  (♪ = music notes restored, ⚠ = whole words added/removed, worth a check)
♪ #16 [00:01:14,740]  "Jf And it makes a fiery ring f" → "♪ And it makes a fiery ring ♪"
#183 [00:17:28,755]  "ll stay." → "I'll stay."
#336 [00:29:45,700]  "-Miss Lew ton--" → "-Miss Lewton--"
⚠ #464 [00:49:15,118]  "...par ti en do le la cara..." → "...por andar echándole la culpa..."
```
- `#183` is the subtitle number and `[00:17:28,755]` is when it appears, so you can jump to it in Subtitle Edit or the video.
- Each line shows the subtitle as it was in the original file and as it is now, even when both the music repair and the AI changed it.
- **♪ lines** had music notes restored (see "Music notes" below).
- **⚠ lines** are fixes that add or remove whole words, usually where the OCR text was badly garbled and the AI had to reconstruct it. These are the ones worth checking.
- **REJECTED** lists fixes the script refused to apply (see the safety checks below). Fix those by hand if needed.
- **FAILED CHUNKS** lists any section the AI couldn't process. That section is left unchanged.

---

## Options

| Option | What it does |
|---|---|
| *(none)* | Fix using Claude Sonnet through your Pro plan. This is the normal way to run it. |
| `--force` | Re-fix a file that was already done. It starts again from `fixsubs/Movie.original.srt`, not from the already-fixed file. |
| `--dry-run` | Only writes `fixsubs/Movie.fixes.txt` so you can preview the changes. No files are renamed or changed. |
| `--no-music` | Skips the automatic music-note repair (see "Music notes" below). The AI still knows about notes. Use it only if the repair ever gets something wrong. |
| `--model opus` | Uses Claude Opus: more careful, but it uses more of your Pro limit. |
| `--model haiku` | Uses Claude Haiku: faster and lighter on your Pro limit, but less accurate. |
| `--local` | Uses a local model in LM Studio instead of Claude (see below). |
| `--local-url URL` | Server address for `--local` (default `http://localhost:1234/v1`). Works with any OpenAI-compatible server, such as Ollama (`http://localhost:11434/v1`). |
| `--model <id>` *(with `--local`)* | Picks which loaded local model to use. `lms ps` shows the IDs. By default it uses the first chat model the server lists. |
| `--jobs N` | How many chunks are processed at the same time (default 2 for Claude, 1 for local). |
| `--chunk-size N` | Subtitles per AI request (default 150 for Claude, 40 for local). Lower it if a local model struggles. |
| `-h`, `--help` | Shows the built-in help. |

Examples:
```bash
fixsubs --dry-run "Movie.srt"              # preview only
fixsubs --force "Movie.srt"                # redo it
fixsubs --model opus "Movie Spanish.srt"   # extra care for a messy track
fixsubs --local "/path/to/folder"          # offline / save Pro usage
```

---

## Using a local model (backup)

1. Open LM Studio and load a chat model. A good choice on this Mac (48 GB) is **Qwen3.6 35B-A3B, 4-bit**.
   - If the model "thinks" before answering, turn that off for speed. In **My Models**, select the model, then open **Inference** and **Prompt Template**, and add `{%- set enable_thinking = false %}` as the first line.
2. Start the server: in LM Studio's **Developer** tab, or with `lms server start`.
3. Run `fixsubs --local <file or folder>`.
4. When you're done: `lms server stop`.

Local models are slower and less accurate than Claude, especially on badly garbled lines. Check the log more carefully when you use one.

---

## How it works

1. **Reads the SRT.** It handles UTF-8 (with or without a BOM), Windows or Mac line endings, and older Western-European encodings.
2. **Works out the language** from the text itself. Non-Latin scripts work too: Japanese was tested, with fixes such as `ソれ` → `それ`, `ちよっと` → `ちょっと` and `ラ一メン` → `ラーメン`. Files must be UTF-8, which is Subtitle Edit's default. A tag in the filename like `Movie.fr.srt`, `Movie.French.srt` or `Movie.pt-BR.forced.srt` is passed along as a hint. Any language works, and Claude applies that language's spelling and punctuation rules.
3. **Splits the file into chunks** of about 150 subtitles. For context, each chunk also includes 5 subtitles on either side, which the AI is not allowed to edit.
4. **Sends only the subtitle text, never the timecodes.** The AI returns only the subtitles it changed. Timing can't be altered: the script verified this on real files by comparing every timecode line.
5. **Instructs the AI strictly** to fix OCR glyph errors only, and never to rephrase, fix grammar, change slang (`gonna`, `ain't`, `pa'`, `chais pas`), translate, or add or remove words.
6. **Repairs music notes first, with fixed rules** (before the AI; see "Music notes" below).
7. **Checks every proposed AI fix before applying it** (the safety checks):
   - It rejects a fix if the number of lines in that subtitle changes.
   - It rejects a fix if the text changed too much (less than 75% similar), because that looks like a rewrite rather than an OCR fix.
     - **Exception for short lines:** a fix that keeps the length the same, swaps at most 2 characters and keeps at least half the line is always allowed. In Japanese and other CJK text, one wrong character is a large share of a short line (`エ場` → `工場`, `力ード` → `カード`), and English has cases like `HI.` → `Hi.`. A full swap such as `猫` → `犬` or `ok` → `no` is still rejected.
   - It keeps the original case of a subtitle's first letter, because sentences often carry over from the previous subtitle (`sorry I'm late`, `...which`). Real OCR fixes like `ll stay` → `I'll stay` still go through.
   - It rejects a fix that removes a music note.
   - It allows fixes that only swap OCR junk for a music note, even on very short lines (`f Say f` → `♪ Say ♪`).
   - It marks ⚠ in the log any fix that adds or removes a whole word.
8. **Writes safely.** The fixed file is written to a temporary file first. Only then is the original moved to `fixsubs/Movie.original.srt` and the fixed file put in its place. If every chunk failed, nothing is renamed.

### Music notes
Subtitle Edit's OCR often misreads the ♪ in song lyrics as letters, for example `Sf Love is a burning thing f` or `JS Bound by wild desire... f`. fixsubs repairs these with fixed rules **before** the AI runs. That makes the repair free, the same every time, and just as good with a local model.

- **What it recognises:**
  - At the start of a line: `J`, `JS`, `Jf`, `Sf`, `S`, `f`, `j`, `J_`, `~`, `#`, `¶`, `£` and similar. These are also recognised after a dialogue dash (`-J`), an `<i>` tag, or a speaker tag (`[singer 1] J`).
  - At the end of a line: `f`, `J`, `Jf`, `Lf`, `Sf`, `S` and similar.
  - A note glued onto the first word (`JA little bit softer now`).
  - A lone note in brackets, the SDH "music playing" marker (`[F5J]`, `[J]`, `[Jf]` → `[♪]`, the brackets are kept). This is always fixed, since a bracket holding only junk like that is never a real sound tag. On *Underworld: Blood Wars* it fixed 45 `[F5J]` cues and touched nothing else in the library.
- **When a line counts as lyrics:** it needs a suspect at **both** the start and the end of the subtitle, or a real ♪ already. A subtitle with only one suspect, such as `could never touch me f`, is only fixed when a confirmed lyric is within 2 subtitles of it. That way a stray `J` or `f` in normal dialogue is never touched. On the two Final Destination tracks the rules changed nothing (0 false positives).
- **Which note it uses:** whatever the file already uses most (♪ or ♫). It defaults to ♪.
- **What the AI cleans up afterwards:** the odd cases the rules leave, such as `come ond` → `come on ♪`, a note read as `!`, `/ fell` → `I fell` and `1 got` → `I got`.
- **The suspect lists** are `NOTE_START` and `NOTE_END` near the top of the script. If you see a new misread shape, add it there.

Tested on *Final Destination: Bloodlines*: 235 notes were restored across 90 lyric subtitles, with every song line fixed.

### Re-running and skipping
- A file that already has a `.original.srt` (in its `fixsubs` folder, or beside it from an older version) counts as done and is **skipped**. Re-running a folder is safe, and after an interruption it picks up where it left off.
- Files ending in `.original.srt` or `.fixed.srt`, macOS `._` hidden files on external drives, and the `fixsubs` folder itself are ignored when you scan a folder.
- You can also pass `fixsubs/Movie.original.srt` directly. It's treated as `Movie.srt`.

### Usage limits
Every run uses some of your **Claude Pro usage**. Pro limits reset every 5 hours. If you hit the limit, the script stops with `Claude Pro usage limit hit; retry later`. Files it already finished are kept, so just run the same command again later.

---

## Undoing a fix

To go back to the original for `Movie.srt`:
```bash
mv "fixsubs/Movie.original.srt" "Movie.srt"
```
(This replaces the fixed version.) You can then delete `fixsubs/Movie.fixes.txt`.

---

## Customising

Everything is in `~/.local/bin/fixsubs`. Make changes there, then refresh the reference copy (see "Where things are"):
- **`NOTE_START` / `NOTE_END`** list the junk tokens treated as misread music notes at the start or end of a line.
- **`_BRACKET_NOTE_RE`** sets which characters count as junk inside a bracketed note (`[F5J]`). Add any new misread character there.
- **`SYSTEM_PROMPT`** (near the top) holds the instructions the AI follows. If it keeps missing a particular OCR error, or keeps changing something it shouldn't, add an example to the "Fix things like" or "Do NOT" lists.
- **Settings at the top:**
  - `CHUNK_SIZE` and `LOCAL_CHUNK_SIZE`: subtitles per request.
  - `CONTEXT`: neighbouring subtitles sent for context.
  - `MIN_SIMILARITY`: how strict the "changed too much" check is. Lower allows bigger changes.
  - `RETRIES`: attempts per chunk.
  - `LOCAL_URL`: default local server.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `'claude' (Claude Code CLI) not found` | Claude Code isn't installed or isn't on PATH. Check with `which claude`. |
| A chunk says `FAILED` | Usually a temporary hiccup. Run it again with `--force`. That section was left unchanged. |
| `Claude Pro usage limit hit` | Wait for your limit to reset (up to 5 hours), then rerun. Finished files are kept. |
| `can't reach local server` | Start LM Studio's server (`lms server start`) and load a model. |
| Local model is very slow | It's probably a "thinking" model. Turn thinking off (see above) or use a different model. |
| A file says `already fixed, skipping` | It has a `.original.srt`. Use `--force` to redo it. |
| Jellyfin shows `.original.srt` as an extra subtitle track | Probably an old-style original left beside the movie. Run with `--force` to move it into the `fixsubs` folder, or move it there yourself. |
| Garbled characters in a non-Western language | The file probably isn't UTF-8. Re-save it as UTF-8 in Subtitle Edit and rerun. |
