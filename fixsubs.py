#!/usr/bin/env python3
"""fixsubs - fix OCR errors in .srt subtitle files using Claude (via Claude Code / Pro plan).

Usage:
  fixsubs movie.srt            -> movie.srt (fixed), plus fixsubs/movie.original.srt (untouched)
                                  and fixsubs/movie.fixes.txt in a "fixsubs" folder next to it
  fixsubs /path/to/folder/     -> every *.srt in the folder
  fixsubs --local movie.srt    -> use a local model via LM Studio's server instead of Claude

A file that already has a .original.srt is skipped; --force re-fixes it from the .original.srt.
Older runs that left movie.original.srt / movie.fixes.txt beside the movie are still recognized,
and those files are moved into the fixsubs folder the next time the file is fixed.

Only cue text is sent to Claude; timecodes never leave this script, so they cannot change.
"""

import argparse
import concurrent.futures as cf
import difflib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CHUNK_SIZE = 150        # Claude
LOCAL_CHUNK_SIZE = 40   # small local models / small context windows
LOCAL_URL = "http://localhost:1234/v1"  # LM Studio default
CONTEXT = 5
MIN_SIMILARITY = 0.75
RETRIES = 2
CLAUDE_TIMEOUT = 600

SYSTEM_PROMPT = """You are an OCR proofreader for movie/TV subtitles. The subtitles were produced by OCR of image-based subtitles and contain character-recognition errors. Your ONLY job is to fix those OCR errors.

The subtitles may be in ANY language. First determine the language from the text (a "language_hint" from the filename may be given, but trust the text). Apply that language's own spelling, punctuation and typography conventions, and never translate or change the language.

Fix things like:
- Wrong look-alike characters: "1" / "l" / "|" used for "I" (English "1 know", "lt's", "|'m"; French "ll" for "Il"), "0" for "O" or vice versa, "rn" read as "m" or "m" as "rn", "cl" for "d", "vv" for "w", "ii" for "ll"/"u".
- Lost or wrong diacritics where the word is clearly misspelled without them (French "a" vs "à", "deja" -> "déjà"; Spanish "nino" -> "niño"; German "fur" -> "für", "B" for "ß"; Nordic å/ä/ö/ø/æ; Polish ł/ż; etc.).
- Missing, misplaced, or wrong apostrophes and quotes ("dont" -> "don't", "j ai" -> "j'ai", "won`t" -> "won't"), and mangled language-specific punctuation (Spanish ¿ ¡ read as "i" or missing, French « »). For style choices such as French spacing before ! ? : ; follow whatever the file already does consistently; do not impose a style.
- Broken spacing caused by OCR ("I don 't", "wh at", missing space after punctuation).
- Wrong punctuation clearly caused by OCR (e.g. ",," or "." misread as ",").
- Broken or unbalanced italic tags (<i> ... </i>).
- Music notes. Song lyrics are marked with a note at the start and/or end of the line ("♪ Love is a burning thing ♪"). OCR often misreads the note as "J", "JS", "Jf", "Sf", "S", "f", "j", "#", "¶", "!" or similar junk, or glues a stray letter to the last word of a lyric ("come ond" -> "come on ♪"). In lyric lines (neighbouring cues with notes are a strong hint), replace such junk with the note character given as "music_note". Never remove an existing note.

Do NOT:
- Rephrase, reword, translate, or "improve" dialogue.
- Correct grammar, slang, dialect or intentional spellings the speaker actually used ("gonna", "ain't", "y'all", "lemme", "chais pas", "pa'", etc.).
- Change names, numbers, times, or years unless clearly an OCR glyph error.
- Add, remove, or reorder words. Only split or join words when OCR clearly broke or merged them ("Lew ton" -> "Lewton"). Leave a repeated or odd word alone ("you can feel almost feel" stays).
- Fix grammar or wording even when it looks wrong. If the words are real, correctly spelled words, keep them.
- Change the capital letter at the start of a cue. Sentences often continue from the previous cue ("sorry I'm late", "...which"). Only fix capitals in the middle of a sentence caused by OCR ("I saw It", "I Know").
- Change the spacing of dialogue dashes ("- Hi" vs "-Hi"); keep whatever each line has.
- Add or remove lines, or change where line breaks fall.

Input: a JSON object with optional "language_hint", "music_note" (the note character this file uses), "context" (read-only neighbouring cues, never edit these) and "cues" (id -> text; line breaks are "\\n").
Output: ONLY a JSON object mapping id -> corrected full text, containing ONLY the cues you changed. Keep the same number of "\\n" line breaks as the original. If nothing needs fixing, output {}. No commentary, no markdown fences."""


class UsageLimitError(Exception):
    pass


@dataclass
class Cue:
    index: str
    timing: str
    lines: list
    original: str = ""      # text as read from the file
    after_music: str = ""   # text after the music-note pass (what the AI sees)

    @property
    def text(self):
        return "\n".join(self.lines)


@dataclass
class Result:
    applied: list = field(default_factory=list)   # (cue, old, new)
    rejected: list = field(default_factory=list)  # (cue_id, old, new, reason)
    failed: list = field(default_factory=list)    # (first_id, last_id, error)


# ---------- SRT I/O ----------

def read_srt(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    timing_re = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        lines = block.split("\n")
        # Locate the timing line (usually line 2; tolerate missing index).
        t = next((i for i, l in enumerate(lines[:3]) if timing_re.match(l)), None)
        if t is None:
            if cues:  # stray text block: treat as continuation of previous cue
                cues[-1].lines.extend([""] + lines)
            continue
        index = lines[t - 1].strip() if t >= 1 else str(len(cues) + 1)
        cues.append(Cue(index, lines[t].strip(), lines[t + 1:]))
    return cues, newline


def write_srt(path, cues, newline):
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{c.timing}\n{c.text}\n")
    path.write_text("\n".join(out).replace("\n", newline), encoding="utf-8")


# ---------- music notes ----------

NOTE_CHARS = "♪♫"
# Whole-word tokens OCR produces in place of a music note, at the start / end of a line.
NOTE_START = {"J", "JS", "Jf", "Sf", "S", "SJ", "J_", "f", "j", "JJ", "Jj", "Jl", "J'", "fJ", "ff",
              "#", "¶", "&", "}", "~", "£"}
NOTE_END = {"f", "J", "j", "Jf", "fJ", "ff", "JJ", "Lf", "Sf", "S", "#", "¶", "&", "}", "£"}
# In a confirmed music cue, a note glued to the first word: "JA little bit", "fA little", "£A little"
_GLUED_RE = re.compile(r"^(?P<pre>(?:<i>)?(?:-\s?)?(?:\[[^\]]*\]\s+)?(?:<i>)?)[Jf£](?=[A-Z](?:[a-z]|\s))")
# optional <i>, dialogue dash, [speaker] tag before the note
_START_RE = re.compile(r"^(?P<pre>(?:<i>)?(?:-\s?)?(?:\[[^\]]*\]\s+)?(?:<i>)?)(?P<tok>\S+)\s+(?P<rest>\S.*)$")
_END_RE = re.compile(r"^(?P<rest>.*\S)\s+(?P<tok>\S+?)(?P<post>(?:</i>)?)$")
# SDH "music playing" marker, a note alone in brackets: "[♪]" read as "[F5J]", "[J]", "[Jf]"...
# The junk has no lowercase letters or vowels, so real tags like "[man]" or "[OOH]" never match.
_BRACKET_NOTE_RE = re.compile(r"^(?P<pre>(?:<i>)?(?:-\s?)?)\[\s*(?P<tok>[JjfFS5#¶&}£l1I'_♪♫ ]{1,5}?)\s*\](?P<post>(?:</i>)?)$")


def note_char(cues):
    text = "".join(c.text for c in cues)
    return "♫" if text.count("♫") > text.count("♪") else "♪"


def _note_tokens(line):
    """(start_match, end_match) for suspect note tokens on this line, or None for each."""
    st = _START_RE.match(line)
    st = st if st and st.group("tok") in NOTE_START else None
    en = _END_RE.match(line)
    en = en if en and en.group("tok") in NOTE_END else None
    if st and en and st.end("pre") + len(st.group("tok")) > en.start("tok"):
        en = None  # same token, e.g. a two-word line "J Hey"
    return st, en


def _convert(cue, note):
    n = 0
    for i, line in enumerate(cue.lines):
        st, en = _note_tokens(line)
        if en:
            line = f"{en.group('rest')} {note}{en.group('post')}"
            n += 1
        if st:
            m = _START_RE.match(line)
            line = f"{m.group('pre')}{note} {m.group('rest')}"
            n += 1
        elif _GLUED_RE.match(line):
            line = _GLUED_RE.sub(lambda m: f"{m.group('pre')}{note} ", line, count=1)
            n += 1
        cue.lines[i] = line
    return n


def _bracket_note(line):
    m = _BRACKET_NOTE_RE.match(line)
    if not m or set(m.group("tok")) <= set(NOTE_CHARS + " "):
        return None  # no match, or already a real note
    if not set(m.group("tok")) & set("Jjf#¶5"):
        return None  # e.g. "[I]", "[S]" on their own are too unlike a note
    return m


def fix_bracket_notes(cues, note):
    """Turn a lone bracketed note misread by OCR ("[F5J]") into "[♪]". Returns how many."""
    n = 0
    for c in cues:
        for i, line in enumerate(c.lines):
            m = _bracket_note(line)
            if m:
                c.lines[i] = f"{m.group('pre')}[{note}]{m.group('post')}"
                n += 1
    return n


def fix_music_notes(cues, note):
    """Turn OCR junk like "J ... f" back into music notes. Returns how many notes were restored.
    A cue counts as music if it has a suspect token at both a line start and a line end, or
    already has a real note. Cues with a single suspect token are only converted when a
    music cue is within 2 cues of them, so stray "J"/"f" in normal dialogue is left alone."""
    starts, ends = [], []
    for c in cues:
        toks = [_note_tokens(l) for l in c.lines]
        starts.append(sum(1 for st, _ in toks if st))
        ends.append(sum(1 for _, en in toks if en))
    music = [bool(starts[i] and ends[i]) or any(ch in c.text for ch in NOTE_CHARS)
             for i, c in enumerate(cues)]
    changed = True
    while changed:
        changed = False
        for i in range(len(cues)):
            if not music[i] and (starts[i] or ends[i]) and any(music[max(0, i - 2):i + 3]):
                music[i] = changed = True
    restored = 0
    for i, c in enumerate(cues):
        if music[i] and (starts[i] or ends[i]):
            restored += _convert(c, note)
    # after the lyric pass, so a "[♪]" background-music cue never marks its neighbours as lyrics
    return restored + fix_bracket_notes(cues, note)


def only_notes_changed(old, new):
    """True if the only differences are music notes replacing OCR junk / being added."""
    junk = set("JjSf#¶&}!' ")
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if not set(new[j1:j2]) <= set(NOTE_CHARS + " ") or not set(old[i1:i2]) <= junk:
            return False
    return True


# ---------- Claude ----------

def call_claude(payload, model):
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    proc = subprocess.run(
        cmd, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
        text=True, timeout=CLAUDE_TIMEOUT,
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    if re.search(r"usage limit|rate limit|limit reached|resets? at", blob, re.I) and proc.returncode != 0:
        raise UsageLimitError(blob.strip()[-400:])
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {blob.strip()[-400:]}")
    result = envelope.get("result", "")
    if envelope.get("is_error"):
        if re.search(r"usage limit|limit reached|resets? at", str(result), re.I):
            raise UsageLimitError(str(result))
        raise RuntimeError(f"claude error: {str(result)[-400:]}")
    return parse_fixes(result)


def parse_fixes(reply):
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)  # reasoning models
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        raise RuntimeError(f"no JSON in reply: {reply[:200]!r}")
    fixes = json.loads(m.group(0))
    if not isinstance(fixes, dict):
        raise RuntimeError("reply JSON is not an object")
    return fixes


def local_models(url):
    with urllib.request.urlopen(f"{url}/models", timeout=10) as r:
        return [m["id"] for m in json.load(r)["data"]]


def call_local(payload, model, url):
    """OpenAI-compatible chat endpoint (LM Studio, Ollama, llama.cpp server, ...)."""
    body = {
        "model": model,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},  # Qwen-style models; ignored by others
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"local server HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    return parse_fixes(data["choices"][0]["message"]["content"] or "")


def language_hint(path):
    """Language tag from names like movie.fr.srt / movie.French.forced.srt, else None."""
    names = {"english", "french", "spanish", "german", "italian", "portuguese", "dutch", "swedish",
             "norwegian", "danish", "finnish", "polish", "czech", "hungarian", "romanian", "greek",
             "turkish", "russian", "ukrainian", "japanese", "chinese", "korean", "arabic", "hebrew"}
    for part in reversed(path.stem.split(".")[1:]):
        base = part.split("-")[0].split("_")[0]  # pt-BR, en_US
        if base.isalpha() and (len(base) in (2, 3) and base.lower() not in ("hi", "cc", "sdh")
                               or base.lower() in names):
            return part
    return None


def process_chunk(cues, start, end, ask, lang=None, note="♪"):
    ids = {str(i): cues[i] for i in range(start, end)}
    payload = {
        **({"language_hint": lang} if lang else {}),
        "music_note": note,
        "context": {str(i): cues[i].text for i in list(range(max(0, start - CONTEXT), start))
                    + list(range(end, min(len(cues), end + CONTEXT)))},
        "cues": {k: c.text for k, c in ids.items()},
    }
    last_err = None
    for _ in range(RETRIES + 1):
        try:
            return ids, ask(payload)
        except UsageLimitError:
            raise
        except Exception as e:  # noqa: BLE001 - retry anything else
            last_err = e
    raise RuntimeError(str(last_err))


def _letters(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if ch.isalnum())


def needs_review(old, new):
    """True if the fix adds or drops 3+ letters in one place (a whole word, not a glyph swap)."""
    a, b = _letters(old), _letters(new)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal" and abs((i2 - i1) - (j2 - j1)) >= 3:
            return True
    return False


def keep_leading_case(old, new):
    """Sentences often run across cues, so never change the case of a cue's first letter.
    Only applies when it's the same letter (so "ll stay" -> "I'll stay" still goes through)."""
    o = next((i for i, ch in enumerate(old) if ch.isalpha()), None)
    n = next((i for i, ch in enumerate(new) if ch.isalpha()), None)
    if o is None or n is None or old[o] == new[n] or old[o].lower() != new[n].lower():
        return new
    return new[:n] + old[o] + new[n + 1:]


def small_swap(old, new, limit=2):
    """True if the fix keeps the length, swaps at most `limit` characters and keeps at least
    half the line ("エ場" -> "工場", "HI." -> "Hi."). On short lines one character is a big
    share, so the similarity ratio alone would wrongly reject these."""
    swaps = sum(a != b for a, b in zip(old, new))
    return len(old) == len(new) and swaps <= limit and swaps * 2 <= len(old)


def validate(cue, new):
    if not isinstance(new, str):
        return "not a string"
    old = cue.text
    if new == old:
        return None
    if new.count("\n") != old.count("\n"):
        return "line count changed"
    if sum(new.count(ch) for ch in NOTE_CHARS) < sum(old.count(ch) for ch in NOTE_CHARS):
        return "removed a music note"
    if only_notes_changed(old, new) or small_swap(old, new):
        return None
    if difflib.SequenceMatcher(None, old, new).ratio() < MIN_SIMILARITY:
        return "changed too much (looks like a rewrite)"
    return None


# ---------- per-file ----------

SIDE_DIR = "fixsubs"  # folder next to the .srt that holds the .original.srt and .fixes.txt


def original_of(target):
    return target.parent / SIDE_DIR / (target.stem + ".original.srt")


def log_of(target):
    return target.parent / SIDE_DIR / (target.stem + ".fixes.txt")


def legacy_original_of(target):
    """Where older versions put the original: right beside the movie."""
    return target.with_name(target.stem + ".original.srt")


def has_original(target):
    return original_of(target).exists() or legacy_original_of(target).exists()


def migrate_legacy(target):
    """Move an old-style movie.original.srt / movie.fixes.txt into the fixsubs folder."""
    for old, new in ((legacy_original_of(target), original_of(target)),
                     (target.with_name(target.stem + ".fixes.txt"), log_of(target))):
        if old.exists() and not new.exists():
            new.parent.mkdir(exist_ok=True)
            old.rename(new)


def target_of(path):
    """movie.original.srt -> movie.srt (also from inside the fixsubs folder); anything else is its own target."""
    if path.name.endswith(".original.srt"):
        name = path.name[: -len(".original.srt")] + ".srt"
        folder = path.parent.parent if path.parent.name == SIDE_DIR else path.parent
        return folder / name
    return path


def fix_file(path, args):
    original = original_of(path)
    if original.exists():
        source = original
    elif legacy_original_of(path).exists():
        source = legacy_original_of(path)
    else:
        source = path
    cues, newline = read_srt(source)
    if not cues:
        print(f"  no cues found, skipping")
        return None
    for c in cues:
        c.original = c.text
    note = note_char(cues)
    notes = 0 if args.no_music else fix_music_notes(cues, note)
    for c in cues:
        c.after_music = c.text
    if notes:
        print(f"  {notes} music note(s) restored")
    lang = language_hint(path)
    size = args.chunk_size
    chunks = [(s, min(s + size, len(cues))) for s in range(0, len(cues), size)]
    res = Result()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(process_chunk, cues, s, e, args.ask, lang, note): (s, e) for s, e in chunks}
        try:
            for fut in cf.as_completed(futs):
                s, e = futs[fut]
                done += 1
                try:
                    ids, fixes = fut.result()
                except UsageLimitError:
                    for f in futs:
                        f.cancel()
                    raise
                except Exception as err:  # noqa: BLE001
                    res.failed.append((cues[s].index, cues[e - 1].index, str(err)))
                    print(f"  chunk {done}/{len(chunks)}: FAILED ({err})")
                    continue
                n_ok = 0
                for k, new in fixes.items():
                    cue = ids.get(str(k))
                    if cue is None:
                        continue  # context cue (fixed by its own chunk) or a bogus id

                    if isinstance(new, str):
                        new = keep_leading_case(cue.text, new)
                    reason = validate(cue, new)
                    if reason:
                        res.rejected.append((cue.index, cue.text, str(new), reason))
                    elif new != cue.text:
                        res.applied.append((cue, cue.text, new))
                        n_ok += 1
                print(f"  chunk {done}/{len(chunks)}: {n_ok} fixes")
        except KeyboardInterrupt:
            pool.shutdown(cancel_futures=True)
            raise

    for cue, _old, new in res.applied:
        cue.lines = new.split("\n")

    if not args.dry_run:
        migrate_legacy(path)
        if source != path:
            source = original
    log_path = log_of(path)
    log_path.parent.mkdir(exist_ok=True)
    n_fixed = write_log(log_path, path, res, cues, notes)
    print(f"  {n_fixed} cues fixed, {len(res.rejected)} rejected, {len(res.failed)} chunk(s) failed")
    if args.dry_run:
        print(f"  -> {SIDE_DIR}/{log_path.name} (dry run, no files renamed)")
    elif len(res.failed) == len(chunks):
        print(f"  -> {SIDE_DIR}/{log_path.name} (every chunk failed, file left as is)")
    else:
        # Write to a temp file first so nothing is lost if writing fails part-way.
        tmp = path.with_name(path.name + ".tmp")
        write_srt(tmp, cues, newline)
        if source == path:
            path.rename(original)
        tmp.replace(path)
        print(f"  -> {path.name} (fixed), {SIDE_DIR}/{original.name}, {SIDE_DIR}/{log_path.name}")
    return res


def write_log(log_path, src, res, cues, notes=0):
    """Log every cue whose final text differs from the file's original. Returns that count."""
    def show(s):
        return s.replace("\n", " / ")

    def key(i):
        return int(i) if str(i).isdigit() else 0

    changed = [c for c in cues if c.text != c.original]
    out = [f"fixsubs log for {src.name}", f"{len(cues)} cues, {len(changed)} fixed, "
           f"{notes} music note(s) restored, {len(res.rejected)} rejected, "
           f"{len(res.failed)} chunk(s) failed", ""]
    out.append("== APPLIED ==  (♪ = music notes restored, ⚠ = whole words added/removed, worth a check)")
    flagged = 0
    for c in changed:
        music = "♪ " if c.after_music != c.original else ""
        warn = "⚠ " if needs_review(c.after_music, c.text) else ""
        flagged += bool(warn)
        out.append(f'{warn}{music}#{c.index} [{c.timing.split("-->")[0].strip()}]  '
                   f'"{show(c.original)}" → "{show(c.text)}"')
    if flagged:
        out.insert(2, f"{flagged} fix(es) marked ⚠ add or remove whole words; worth a quick check.")
    if res.rejected:
        out += ["", "== REJECTED (not applied, review manually) =="]
        for cid, old, new, reason in sorted(res.rejected, key=lambda r: key(r[0])):
            out.append(f'#{cid} ({reason})  "{show(old)}" → "{show(new)}"')
    if res.failed:
        out += ["", "== FAILED CHUNKS (left unchanged) =="]
        for a, b, err in res.failed:
            out.append(f"cues #{a}-#{b}: {err}")
    log_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(changed)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Fix OCR errors in SRT subtitles using Claude.")
    ap.add_argument("paths", nargs="+", type=Path, help=".srt files or folders")
    ap.add_argument("--force", action="store_true", help="re-fix files that already have a .original.srt")
    ap.add_argument("--model", help="Claude: sonnet (default), opus, haiku. Local: model id "
                    "(default: first model the server lists)")
    ap.add_argument("--local", action="store_true",
                    help="use a local model (LM Studio server) instead of Claude")
    ap.add_argument("--local-url", default=LOCAL_URL,
                    help=f"OpenAI-compatible server URL for --local (default {LOCAL_URL})")
    ap.add_argument("--jobs", type=int, help="chunks processed in parallel (default 2 Claude, 1 local)")
    ap.add_argument("--chunk-size", type=int,
                    help=f"cues per request (default {CHUNK_SIZE} Claude, {LOCAL_CHUNK_SIZE} local)")
    ap.add_argument("--dry-run", action="store_true", help="write only the log")
    ap.add_argument("--no-music", action="store_true",
                    help="skip the automatic music-note repair (J/f/JS -> ♪); the AI still knows about notes")
    args = ap.parse_args()

    if args.local:
        url = args.local_url.rstrip("/")
        try:
            models = local_models(url)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"error: can't reach local server at {url} ({e}).\n"
                     "Start it in LM Studio (Developer tab) or run: lms server start")
        if not models:
            sys.exit(f"error: local server at {url} lists no models; load one in LM Studio")
        model = args.model or next((m for m in models if "embed" not in m.lower()), models[0])
        print(f"Using local model {model} at {url}")
        args.ask = lambda payload: call_local(payload, model, url)
        args.jobs = args.jobs or 1
        args.chunk_size = args.chunk_size or LOCAL_CHUNK_SIZE
    else:
        if not shutil.which("claude"):
            sys.exit("error: 'claude' (Claude Code CLI) not found on PATH")
        model = args.model or "sonnet"
        args.ask = lambda payload: call_claude(payload, model)
        args.jobs = args.jobs or 2
        args.chunk_size = args.chunk_size or CHUNK_SIZE

    files = []
    for p in args.paths:
        if p.is_dir():
            files += sorted(f for f in p.glob("*.srt")
                           if not f.name.startswith("._")
                           and not f.name.endswith((".fixed.srt", ".original.srt")))
        elif p.is_file():
            files.append(target_of(p))
        else:
            print(f"warning: {p} not found", file=sys.stderr)
    files = list(dict.fromkeys(files))

    total = 0
    for i, f in enumerate(files, 1):
        if has_original(f) and not args.force and not args.dry_run:
            print(f"[{i}/{len(files)}] {f.name}: already fixed, skipping (use --force)")
            continue
        print(f"[{i}/{len(files)}] {f.name}")
        try:
            res = fix_file(f, args)
        except UsageLimitError as e:
            sys.exit(f"\nClaude Pro usage limit hit; retry later. Files finished so far are kept.\n{e}")
        except KeyboardInterrupt:
            sys.exit("\ninterrupted")
        if res:
            total += len(res.applied)
    if len(files) > 1:
        print(f"\nDone: {total} fixes across {len(files)} file(s).")


if __name__ == "__main__":
    main()
