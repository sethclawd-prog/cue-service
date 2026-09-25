#!/usr/bin/env python3
"""The crate: a Mac-side collector that builds a personal mix archive from what can be downloaded legitimately.

  crate.py discover [--terms N]   find candidates (SoundCloud, Mixcloud, archive.org, podcast feeds) for the taste in
                                  ~/Mixtapes/crate/taste.json, score them with Jev, store everything in catalog.sqlite
  crate.py fetch [--max N]        download the best unfetched candidates that are offered for download:
                                    - archive.org items, podcast enclosures, Dropbox/Drive/direct links: straight download
                                    - SoundCloud tracks with the Download button: as the signed-in user (token from
                                      ~/.soundcloud_token or CUE_SC_TOKEN); skipped with status 'needs_sc_login' otherwise
                                    - Hypeddit gates: gates.py in the user's own Chrome (email step gets CUE_GATE_EMAIL)
                                    - stream-only pages (Mixcloud, SoundCloud without Download): recorded, never ripped
  crate.py push                   copy new files into Cue on the phone (devicectl; the app imports and identifies them)
  crate.py status                 counts by status, top unfetched, disk use

Everything lands under ~/Mixtapes/crate: catalog.sqlite (the repo of what exists and why it fits), files/ (audio),
crate.log. Re-running is safe: URLs are unique keys, downloads are skipped when the file exists.
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

DIR = os.path.expanduser("~/Mixtapes/crate"); FILES = os.path.join(DIR, "files"); DB = os.path.join(DIR, "catalog.sqlite")
TASTE = os.path.join(DIR, "taste.json"); LOG = os.path.join(DIR, "crate.log")
DEVICE = os.environ.get("CUE_DEVICE", "9BD90016-EE94-564C-98C4-A925F6B1F94E")
os.makedirs(FILES, exist_ok=True)

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True); open(LOG, "a").write(line + "\n")

# ---------- taste ----------
DEFAULT_TASTE = {
    "djs": [], "genres": [], "years": [],
    "steers": ["Shambhala Fractal Forest mix", "Lightning in a Bottle Woogie set", "Bass Coast mix", "glitch hop funk mix", "sunrise downtempo set", "dubstep mix Levity", "Defunk mix"],
    "festivals": ["Shambhala", "Lightning in a Bottle", "Bass Coast", "Burning Man sunrise", "Envision", "Symbiosis"],
    "min_fit": 1.8, "min_minutes": 20, "max_per_dj": 6,
    "songs_in_library": [], "library_titles": []
}
def taste():
    if not os.path.exists(TASTE):
        t = dict(DEFAULT_TASTE)
        prof = "/tmp/cue-profile.json"
        if os.path.exists(prof):
            p = json.load(open(prof))
            for k in ("djs", "genres", "years", "songs_in_library", "library_titles"): t[k] = S.strip_counts(p.get(k, []))
        json.dump(t, open(TASTE, "w"), indent=1); log(f"wrote {TASTE}; edit steers/festivals there")
    return json.load(open(TASTE))

# ---------- catalog ----------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS mixes (
        url TEXT PRIMARY KEY, source TEXT, title TEXT, dj TEXT, minutes INTEGER, year INTEGER, genre TEXT, tags TEXT, plays INTEGER,
        downloadable INTEGER, download_url TEXT, buy_link TEXT, buy_title TEXT, archive_id TEXT, description TEXT,
        fit REAL, is_mix REAL, own REAL, rank REAL, status TEXT DEFAULT 'new', file TEXT, note TEXT, term TEXT,
        found_at TEXT, fetched_at TEXT, pushed_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS mixes_status ON mixes(status, rank)")
    return c

# ---------- podcasts: iTunes search -> RSS enclosures ----------
def cand_podcast(term):
    out = []
    try:
        s, b = S.http(f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}&media=podcast&limit=6")[0::2]
        shows = json.loads(b).get("results", [])
    except Exception: return out
    for show in shows[:4]:
        feed = show.get("feedUrl")
        if not feed: continue
        try:
            s, b = S.http(feed, timeout=20)[0::2]; root = ET.fromstring(b)
        except Exception: continue
        ch = root.find("channel")
        if ch is None: continue
        for item in ch.findall("item")[:40]:
            enc = item.find("enclosure"); title = (item.findtext("title") or "").strip()
            if enc is None or not title: continue
            dur = item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration") or ""
            mins = None
            if re.match(r"^\d+$", dur): mins = int(dur) // 60
            elif ":" in dur:
                parts = [int(x) for x in dur.split(":") if x.isdigit()]
                mins = (parts[0] * 60 + parts[1]) if len(parts) == 3 else (parts[0] if len(parts) == 2 else None)
            if mins is not None and mins < 20: continue
            pub = item.findtext("pubDate") or ""; year = int(re.search(r"(20\d\d|19\d\d)", pub).group(1)) if re.search(r"(20\d\d|19\d\d)", pub) else None
            out.append({"title": title, "dj": show.get("artistName") or show.get("collectionName"), "page_url": item.findtext("link") or enc.get("url"), "download_url": enc.get("url"),
                        "kind": "audio", "source": f"Podcast: {show.get('collectionName')}", "downloadable": True, "duration_minutes": mins, "year": year,
                        "genre": show.get("primaryGenreName"), "tags": "", "plays": None, "likes": None, "description": re.sub(r"<[^>]+>", " ", item.findtext("description") or "")[:500]})
    return out

# ---------- discover ----------
def discover(max_terms, dj_only=None):
    """dj_only: search just this DJ (Seth asked by name) and queue their own mixes as wanted whatever the taste score says."""
    t = taste(); c = db()
    if dj_only and dj_only not in t["djs"]:
        t["djs"].insert(0, dj_only); json.dump(t, open(TASTE, "w"), indent=1)   # remembered for every nightly run from now on
    listener = {"request": "; ".join(t["steers"][:6]), "djs": t["djs"][:12], "genres": t["genres"][:10], "years": t.get("years", [])[:6],
                "festivals": t["festivals"], "songs_in_library": t.get("songs_in_library", [])[:20]}
    terms = []
    for s in t["steers"]: terms.append(s)
    for dj in t["djs"]: terms += [f"{dj} mix", f"{dj} live set"]
    for f in t["festivals"]: terms += [f"{f} mix", f"{f} live set"]
    for g in t["genres"]: terms.append(f"{g} mix")
    seen_terms, uniq = set(), []
    for x in terms:
        if x.lower() not in seen_terms: seen_terms.add(x.lower()); uniq.append(x)
    done_terms = {r[0] for r in c.execute("SELECT DISTINCT term FROM mixes WHERE found_at > datetime('now','-7 days')")}
    if dj_only: uniq = [f"{dj_only} mix", f"{dj_only} live set", f"{dj_only} mixtape", f"{dj_only} DJ set", f"{dj_only} live"]; done_terms = set()
    todo = [x for x in uniq if x not in done_terms][:max_terms]
    log(f"discover: {len(todo)} terms (of {len(uniq)}; {len(done_terms)} searched this week)")
    have = {re.sub(r"\W+", " ", x.lower()).strip() for x in t.get("library_titles", [])}
    have |= {re.sub(r"\W+", " ", x.split(" – ", 1)[-1].lower()).strip() for x in t.get("library_titles", [])}
    def run(job):
        f, term = job
        try: rows = f(term)
        except Exception as e: log(f"  search failed {f.__name__} {term!r}: {e}"); rows = []
        for r in rows: r["_term"] = term
        return rows
    jobs = [(f, term) for term in todo for f in (S.cand_soundcloud, S.cand_mixcloud, S.cand_archive, cand_podcast)]
    new = []
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(run, jobs):
            for r in rows:
                if not r.get("page_url") or (r.get("duration_minutes") or 999) < t["min_minutes"]: continue
                if re.sub(r"\W+", " ", (r.get("title") or "").lower()).strip() in have: continue
                if c.execute("SELECT 1 FROM mixes WHERE url=?", (r["page_url"],)).fetchone(): continue
                if any(n["page_url"] == r["page_url"] for n in new): continue
                new.append(r)
    log(f"discover: {len(new)} new candidates from {len(jobs)} searches")
    # Jev: fit / is_mix / own, 8 per request
    def judge(batch):
        state = {"listener": listener, "candidates": [{k: b.get(k) for k in ("title", "dj", "source", "duration_minutes", "year", "genre", "tags", "plays", "description")} for b in batch]}
        qs = {}
        for i in range(len(batch)):
            qs[f"fit{i}"] = {"type": "score", "instructions": f"How well does `candidates[{i}]` fit this listener: what they asked for in `listener.request`, the festivals in `listener.festivals`, and their taste in `listener.djs`, `listener.genres`, `listener.songs_in_library`?",
                             "criteria": ["Unrelated, or a different meaning of the words", "Loosely related genre or scene", "Matches the genre, a festival, or an adjacent artist the listener would plausibly like", "Squarely their thing: a named DJ, festival stage, or exact genre in the format asked for"]}
            qs[f"mix{i}"] = {"type": "noul", "instructions": f"Is `candidates[{i}]` a full DJ mix, mixtape, radio show or live set (a continuous long recording of many songs) rather than a single song, album, audiobook, talk or interview?"}
            qs[f"own{i}"] = {"type": "noul", "instructions": f"Is `candidates[{i}]` made by one of the DJs in `listener.djs` or by an artist named in `listener.request` (their own mix or set), rather than someone else's mix that mentions them?"}
        return batch, S.jev(state, qs)
    scored = 0; tokens = 0
    with ThreadPoolExecutor(5) as ex:
        for batch, r in ex.map(judge, [new[i:i + 8] for i in range(0, len(new), 8)]):
            tokens += r["usage"]["input_tokens"] + r["usage"]["output_tokens"]
            for i, m in enumerate(batch):
                fit = r["answers"][f"fit{i}"]["score"]; mix = r["answers"][f"mix{i}"]["noul"]; own = r["answers"][f"own{i}"]["noul"]
                plays = m.get("plays")
                rank = fit * mix + 0.8 * own + (0.5 if m.get("downloadable") else 0) + (0.15 if (plays or 0) > 1000 else 0) - (0.6 if plays is not None and plays < 50 else 0)
                status = "new" if mix >= 0.5 and fit >= t["min_fit"] else "low_fit"
                if dj_only and mix >= 0.5 and (own >= 0.5 or dj_only.lower() in f"{m.get('dj', '')} {m.get('title', '')}".lower()): status, rank = "wanted", 50 + rank
                c.execute("""INSERT OR IGNORE INTO mixes (url, source, title, dj, minutes, year, genre, tags, plays, downloadable, download_url, buy_link, buy_title, archive_id, description,
                             fit, is_mix, own, rank, status, term, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                          (m["page_url"], m.get("source"), m.get("title"), m.get("dj"), m.get("duration_minutes"), m.get("year"), m.get("genre"), m.get("tags"), plays,
                           1 if m.get("downloadable") else 0, m.get("download_url"), m.get("buy_link"), m.get("buy_link_title"), m.get("archive_id"), (m.get("description") or "")[:500],
                           round(fit, 2), round(mix, 2), round(own, 2), round(rank, 3), status, m.get("_term")))
                scored += 1
            c.commit()
    total = c.execute("SELECT COUNT(*) FROM mixes").fetchone()[0]
    worth = c.execute("SELECT COUNT(*) FROM mixes WHERE status='new'").fetchone()[0]
    log(f"discover: scored {scored} with Jev ({tokens:,} tokens); catalog now {total} rows, {worth} worth fetching")

# ---------- fetch ----------
def safe_name(s): return re.sub(r"[\\/:*?\"<>|]+", "-", s or "").strip()[:120]

def download(url, dest, min_minutes=15):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": S.UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype: raise RuntimeError("got an HTML page, not a file")
        while True:
            chunk = r.read(1 << 20)
            if not chunk: break
            f.write(chunk)
    p = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration:format=format_name", "-of", "json", tmp], capture_output=True, text=True)
    info = json.loads(p.stdout or "{}").get("format", {})
    dur = float(info.get("duration") or 0)
    if dur < min_minutes * 60:
        os.remove(tmp); raise RuntimeError(f"not a mix: {dur/60:.1f} min ({info.get('format_name')})")
    os.rename(tmp, dest); return dur

def sc_token():
    return os.environ.get("CUE_SC_TOKEN") or (open(os.path.expanduser("~/.soundcloud_token")).read().strip() if os.path.exists(os.path.expanduser("~/.soundcloud_token")) else None)

def resolve_download(row):
    """Return (download_url, note) or raise; sets status codes via exception messages prefixed with 'status:'."""
    url = row["url"]
    if row["download_url"]: return row["download_url"], "direct"
    if row["archive_id"]:
        u, mins = S.archive_audio_url(row["archive_id"])
        if u: return u, "archive.org"
        raise RuntimeError("status:no_audio archive item has no mp3")
    if "soundcloud.com" in url:
        code, tr = S.sc_api("/resolve", url=url)
        if not tr or tr.get("kind") != "track": raise RuntimeError("status:removed track gone")
        if tr.get("downloadable") and tr.get("has_downloads_left", True):
            tok = sc_token()
            if not tok: raise RuntimeError("status:needs_sc_login Download button needs your SoundCloud token (~/.soundcloud_token)")
            try:
                s, ct, b = S.http(f"https://api-v2.soundcloud.com/tracks/{tr['id']}/download?client_id={S.sc_client_id()}", headers={"Authorization": "OAuth " + tok})
                j = json.loads(b)
                if j.get("redirectUri"): return j["redirectUri"], "SoundCloud Download button"
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"status:needs_sc_login SoundCloud download returned {e.code}")
        link = tr.get("purchase_url"); title = tr.get("purchase_title") or ""
        if link and re.search(r"download|free|dl\b", title, re.I):
            g = S.get_link({"buy_link": link, "page_url": url, "source": "", "downloadable": True})
            if g.get("kind") == "audio": return g["download_url"], f"free link ({title})"
            if g.get("get_url") and "hypeddit.com/track/" in g["get_url"]:
                import gates
                r = gates.fetch(g["get_url"], os.environ.get("CUE_GATE_EMAIL", ""))
                if r.get("file"): return "file://" + r["file"], "Hypeddit gate"
                raise RuntimeError("status:gate_failed " + str(r.get("error") or r.get("needs_login")))
            raise RuntimeError(f"status:gate_needs_login {g.get('get_label') or 'gate'} at {g.get('get_url') or link}")
        raise RuntimeError("status:stream_only no download offered")
    if "mixcloud.com" in url: raise RuntimeError("status:stream_only Mixcloud streams only")
    raise RuntimeError("status:unknown_source")

def fetch_archive_set(tracks, name):
    """A Live Music Archive show: one MP3 per song. Download the parts, join them losslessly into files/<name>.mp3, and write
    the setlist in as chapters (ID3 CHAP in the mp3, a QuickTime chapter track in phone/<name>.m4a) so Cue has the cue marks
    the moment the file lands, with no on-device identification needed. Returns (dest, seconds)."""
    parts_dir = os.path.join(DIR, ".parts", name); os.makedirs(parts_dir, exist_ok=True)
    parts, starts, t = [], [], 0.0
    for i, tr in enumerate(tracks):
        part = os.path.join(parts_dir, f"{i:03d}.mp3")
        if not os.path.exists(part) or os.path.getsize(part) < 1000: download(tr["url"], part, 0)
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", part], capture_output=True, text=True)
        d = float(p.stdout.strip() or tr["seconds"] or 0)
        parts.append(part); starts.append((t, tr["title"])); t += d
    if t < 60: raise RuntimeError("status:no_audio joined show is under a minute")
    lst = os.path.join(parts_dir, "list.txt"); meta = os.path.join(parts_dir, "chapters.txt")
    with open(lst, "w") as f: f.writelines("file '%s'\n" % p.replace("'", "'\\''") for p in parts)
    with open(meta, "w") as f:
        f.write(";FFMETADATA1\n")
        for (s0, title), s1 in zip(starts, [s for s, _ in starts[1:]] + [t]):
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(s0*1000)}\nEND={int(s1*1000)}\ntitle={re.sub(r'([=;#\\\\])', r'\\\\\\1', title.replace(chr(10), ' '))}\n")
    dest = os.path.join(FILES, name + ".mp3"); phone = os.path.join(DIR, "phone", name + ".m4a"); os.makedirs(os.path.dirname(phone), exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", meta, "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
                        "-c", "copy", "-id3v2_version", "3", dest], capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError("status:failed concat: " + r.stderr[-200:])
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", dest, "-i", meta, "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-vn", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", phone], capture_output=True, text=True)
    if r.returncode != 0: log(f"phone transcode failed for {name}: {r.stderr[-200:]}")
    subprocess.run(["rm", "-rf", parts_dir])
    return dest, t

def fetch(max_items):
    lock = os.path.join(DIR, ".fetch.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3 * 3600: log("fetch: another fetch is running"); return
    open(lock, "w").write(str(os.getpid()))
    try: _fetch(max_items)
    finally:
        try: os.remove(lock)
        except OSError: pass

def _fetch(max_items):
    t = taste(); c = db()
    # Wanted first: Seth asked for these from the phone, so they skip the per-DJ cap and a failure is kept visible for a hand-fetch.
    rows = c.execute("SELECT * FROM mixes WHERE status IN ('wanted','new') ORDER BY (status='wanted') DESC, rank DESC").fetchall()
    per_dj = {r[0]: r[1] for r in c.execute("SELECT dj, COUNT(*) FROM mixes WHERE status='fetched' GROUP BY dj")}
    got = 0
    for row in rows:
        if got >= max_items: break
        if row["status"] != "wanted" and per_dj.get(row["dj"], 0) >= t["max_per_dj"]: continue
        name = safe_name(f"{row['dj']} - {row['title']}")
        try:
            tracks = S.archive_tracks(row["archive_id"]) if row["archive_id"] and not row["download_url"] else []
            if len(tracks) > 1:
                dest, secs = fetch_archive_set(tracks, name); how = f"archive.org, {len(tracks)} tracks joined, setlist as chapters"; dur = secs
                c.execute("UPDATE mixes SET minutes=? WHERE url=? AND (minutes IS NULL OR minutes=0)", (int(secs // 60), row["url"]))
                src = None
            else:
                src, how = resolve_download(row)
            if src is None: pass
            elif src.startswith("file://"):
                dest = os.path.join(FILES, name + os.path.splitext(src)[1]); os.replace(src[7:], dest); dur = None
            else:
                ext = ".mp3" if re.search(r"\.mp3(\?|$)", src, re.I) or "audio/mpeg" in how else os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".mp3"
                dest = os.path.join(FILES, name + ext)
                if os.path.exists(dest): dur = None
                else: dur = download(src, dest, t["min_minutes"] * 0.75)
            c.execute("UPDATE mixes SET status='fetched', file=?, note=?, fetched_at=datetime('now') WHERE url=?", (os.path.basename(dest), how, row["url"])); c.commit()
            per_dj[row["dj"]] = per_dj.get(row["dj"], 0) + 1; got += 1
            log(f"fetched: {name} ({how}{'' if dur is None else f', {dur/60:.0f} min'})")
            time.sleep(2)
        except Exception as e:
            msg = str(e); status = "failed"
            m = re.match(r"status:(\w+)\s*(.*)", msg)
            if m: status, msg = m.group(1), m.group(2)
            if row["status"] == "wanted": msg = f"{status}: {msg}"; status = "wanted_failed"
            c.execute("UPDATE mixes SET status=?, note=? WHERE url=?", (status, msg[:300], row["url"])); c.commit()
            log(f"skip [{status}]: {name} — {msg[:120]}")
    log(f"fetch: {got} new files; " + ", ".join(f"{r[0]}={r[1]}" for r in c.execute("SELECT status, COUNT(*) FROM mixes GROUP BY status ORDER BY 2 DESC")))

def want(url, title="", dj="", source="", minutes=None, year=None):
    """Queue one mix from the phone. Known rows are re-armed whatever their last status; unknown pages get a row of their own."""
    c = db()
    if c.execute("SELECT 1 FROM mixes WHERE url=?", (url,)).fetchone():
        c.execute("UPDATE mixes SET status=CASE WHEN status='fetched' THEN 'fetched' ELSE 'wanted' END, rank=99, note='wanted from phone' WHERE url=?", (url,))
    else:
        c.execute("INSERT INTO mixes (url, source, title, dj, minutes, year, status, rank, fit, is_mix, own, found_at, note) VALUES (?,?,?,?,?,?,'wanted',99,3,1,1,datetime('now'),'wanted from phone')",
                  (url, source or ("SoundCloud" if "soundcloud.com" in url else "Mixcloud" if "mixcloud.com" in url else "web"), title, dj, minutes, year))
    c.commit(); log(f"wanted: {dj} - {title} ({url})")
    return c.execute("SELECT status FROM mixes WHERE url=?", (url,)).fetchone()[0]

# ---------- push ----------
def push():
    c = db(); rows = c.execute("SELECT * FROM mixes WHERE status='fetched' AND pushed_at IS NULL AND file IS NOT NULL").fetchall()
    n = 0
    phone_dir = os.path.join(DIR, "phone"); os.makedirs(phone_dir, exist_ok=True)
    for row in rows:
        path = os.path.join(FILES, row["file"])
        if not os.path.exists(path): continue
        # Lossless or oversized downloads go to the phone as 192k AAC; the original stays in the crate.
        if path.lower().endswith((".wav", ".aiff", ".aif", ".flac")) or os.path.getsize(path) > 250_000_000:
            m4a = os.path.join(phone_dir, os.path.splitext(row["file"])[0] + ".m4a")
            if not os.path.exists(m4a):
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", m4a], capture_output=True, text=True)
                if r.returncode != 0: log(f"transcode failed: {row['file']}: {r.stderr[-200:]}"); continue
            path = m4a
        p = subprocess.run(["xcrun", "devicectl", "device", "copy", "to", "--device", DEVICE, "--source", path, "--destination", "Documents/" + os.path.basename(path),
                            "--domain-type", "appDataContainer", "--domain-identifier", "com.sethcosmo.Cue"], capture_output=True, text=True)
        if p.returncode == 0:
            c.execute("UPDATE mixes SET pushed_at=datetime('now') WHERE url=?", (row["url"],)); c.commit(); n += 1; log(f"pushed: {row['file']}")
        else:
            log(f"push failed: {row['file']}: {(p.stderr or p.stdout)[-200:]}"); break
    log(f"push: {n} files sent to the phone (Cue imports them on open/unlock)")

def status():
    c = db()
    print("catalog:", c.execute("SELECT COUNT(*) FROM mixes").fetchone()[0], "rows")
    for r in c.execute("SELECT status, COUNT(*) FROM mixes GROUP BY status ORDER BY 2 DESC"): print(f"  {r[0]:16} {r[1]}")
    print("top unfetched:")
    for r in c.execute("SELECT rank, fit, dj, title, source, minutes FROM mixes WHERE status='new' ORDER BY rank DESC LIMIT 15"):
        print(f"  {r[0]:.2f} fit {r[1]:.1f} | {r[2]} – {(r[3] or '')[:55]} | {r[4]} | {r[5]} min")
    size = sum(os.path.getsize(os.path.join(FILES, f)) for f in os.listdir(FILES)) / 1e9
    print(f"files: {len(os.listdir(FILES))} ({size:.1f} GB) in {FILES}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "want": print(want(sys.argv[2], *(sys.argv[3:5]))); sys.exit()
    arg = lambda flag, default: int(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default
    if cmd == "discover": discover(arg("--terms", 40), sys.argv[sys.argv.index("--dj") + 1] if "--dj" in sys.argv else None)
    elif cmd == "fetch": fetch(arg("--max", 25))
    elif cmd == "push": push()
    else: status()
