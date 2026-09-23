#!/usr/bin/env python3
"""Cue service: the small backend behind "Add from link" and "For you".

  POST /lookup    {"url": "https://soundcloud.com/dj/mix"}
                  -> where can this mix legitimately be downloaded (the page's own Download button, DJ's site, Bandcamp,
                     podcast feed, archive.org, a Dropbox/Drive link in the description ...) + its tracklist text.
  POST /discover  {"profile": {...taste summary from the phone...}, "count": 8, "query": "dubstep, Levity or Defunk"}
                  -> mixes the listener would like, each with a downloadable source when one exists.
  GET  /health

Cost discipline (the whole point of this file):
  1. Facts first, agent second. For SoundCloud and Mixcloud links the service reads the platform's own JSON (title, DJ,
     duration, description with the tracklist, the Download flag, the buy/free-download link) with plain HTTP. If the page
     itself offers the file, the answer is returned with NO model call. Otherwise the facts go into the prompt so the agent
     never spends searches or fetches on a JavaScript shell.
  2. Small tool budgets: few searches, few fetches, short fetched pages, medium effort. The bill is dominated by fetched
     page text re-read on every agentic round, so the fetch cap is the lever.
  3. Every response carries `_usage` with tokens, tool calls, seconds and an estimated dollar figure (token prices only,
     search fees excluded), and the same line goes to the log.

Rules baked in: the fetch tool is blocked from SoundCloud/Mixcloud/YouTube/Spotify stream hosts, so the agent cannot pull
a stream even if asked; only sources that offer the file count as downloadable. Results are cached on disk by request.

Run:  ./run.sh   (credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile)
Env:  CUE_LOOKUP_MODEL (default claude-sonnet-5), CUE_DISCOVER_MODEL (default claude-opus-5), CUE_EFFORT (default medium),
      CUE_CACHE (default ./cache), CUE_FAKE=1 (canned answers, no API)
"""
import collections, hashlib, json, os, re, sys, time, traceback, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOKUP_MODEL = os.environ.get("CUE_LOOKUP_MODEL", "claude-sonnet-5")
DISCOVER_MODEL = os.environ.get("CUE_DISCOVER_MODEL", "claude-opus-5")
EFFORT = os.environ.get("CUE_EFFORT", "medium")
CACHE = os.environ.get("CUE_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))
FAKE = os.environ.get("CUE_FAKE") == "1"
os.makedirs(CACHE, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"

# $/1M tokens (input, output). Cache reads bill at 10%, cache writes at 125%. Search/fetch per-call fees are not included.
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}

# Stream hosts the fetch tool must never touch. Subdomains are covered by the API.
BLOCKED_FETCH = ["sndcdn.com", "soundcloud.cloud", "mixcloud.com", "youtube.com", "googlevideo.com", "spotify.com", "scdn.co"]
def tools(searches, fetches, fetch_tokens):
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches, "blocked_domains": BLOCKED_FETCH, "max_content_tokens": fetch_tokens},
    ]
LOOKUP_TOOLS = tools(searches=4, fetches=3, fetch_tokens=4000)
DISCOVER_TOOLS = tools(searches=8, fetches=6, fetch_tokens=5000)

SYSTEM = """You are the research agent for Cue, a mixtape player that adds song-skip points to long DJ mixes.
You find where a DJ mix can be legitimately downloaded and what songs it contains. Legitimate means the file is offered
for download by its uploader or publisher: a Download button on the SoundCloud/Mixcloud/hearthis page, Bandcamp, the DJ's
own site, a podcast RSS feed with an MP3 enclosure, archive.org, or a Dropbox/Google Drive/Mega link the DJ posted.
Never propose stream URLs, ripping services, or converters. A page that only streams is reported as kind "page".
Prefer the exact same recording; say so when a candidate is a different edit or a re-upload. Report tracklists verbatim
when a page publishes one (description, comments, 1001tracklists, mixesdb), one song per line, with timestamps if given.
Work cheaply: facts the service already verified are given to you, so do not re-fetch that page; run a search only when it
can change the answer; stop as soon as you have a confident answer or have exhausted the likely sources.
Be concise and factual; when nothing legitimate exists, say so plainly."""

LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "artist": {"type": "string"},
        "duration_seconds": {"type": ["integer", "null"]},
        "candidates": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"},
            "kind": {"type": "string", "enum": ["audio", "feed", "page"]},
            "source": {"type": "string"},
            "confidence": {"type": "number"},
            "same_recording": {"type": "boolean"},
            "note": {"type": "string"}},
            "required": ["url", "kind", "source", "confidence", "same_recording", "note"], "additionalProperties": False}},
        "tracklist": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}},
    "required": ["title", "artist", "duration_seconds", "candidates", "tracklist", "summary"], "additionalProperties": False}

DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "dj": {"type": "string"},
            "page_url": {"type": "string"},
            "download_url": {"type": ["string", "null"]},
            "kind": {"type": "string", "enum": ["audio", "feed", "page"]},
            "source": {"type": "string"},
            "duration_minutes": {"type": ["integer", "null"]},
            "year": {"type": ["integer", "null"]},
            "why": {"type": "string"},
            "confidence": {"type": "number"}},
            "required": ["title", "dj", "page_url", "download_url", "kind", "source", "duration_minutes", "year", "why", "confidence"], "additionalProperties": False}},
        "summary": {"type": "string"}},
    "required": ["picks", "summary"], "additionalProperties": False}


def log(msg): sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg)); sys.stderr.flush()

def cache_key(kind, payload):
    return os.path.join(CACHE, kind + "-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24] + ".json")


# ---------- facts: platform JSON with plain HTTP, no model ----------

def http(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()

_sc_client = {"id": None, "at": 0}
def sc_client_id():
    """SoundCloud's web client id, read from the hydration block of any soundcloud.com page (rotates every few weeks)."""
    if _sc_client["id"] and time.time() - _sc_client["at"] < 6 * 3600: return _sc_client["id"]
    _, _, body = http("https://soundcloud.com/discover")
    m = re.search(r'window\.__sc_hydration\s*=\s*(\[.*?\]);\s*</script>', body.decode("utf-8", "replace"), re.S)
    for item in json.loads(m.group(1)) if m else []:
        if item.get("hydratable") == "apiClient":
            _sc_client.update(id=item["data"]["id"], at=time.time()); return _sc_client["id"]
    raise RuntimeError("SoundCloud client id not found")

def sc_api(path, **params):
    params["client_id"] = sc_client_id()
    try:
        status, _, body = http("https://api-v2.soundcloud.com" + path + "?" + urllib.parse.urlencode(params))
    except urllib.error.HTTPError as e:
        return e.code, None
    return status, json.loads(body)

def tracklist_from_text(text):
    """Lines that look like 'Artist - Title' (optionally numbered / timestamped). Returns [] when fewer than 3 match."""
    out = []
    for line in (text or "").splitlines():
        if "http" in line.lower() or "download" in line.lower(): continue
        s = re.sub(r"^\s*(\d{1,3}[.)]?|\[?\d{1,2}:\d{2}(?::\d{2})?\]?)\s+", "", line.strip(" |\t")).strip(" -–—•|\t")
        if re.search(r"\S\s+[-–—]\s+\S", s) and 4 < len(s) < 160: out.append(s)
    return out if len(out) >= 3 else []

def facts_soundcloud(url):
    code, t = sc_api("/resolve", url=url)
    if code == 404 or not t: return {"platform": "SoundCloud", "status": "removed", "detail": "SoundCloud no longer serves this URL (the track was deleted or made private)."}
    if t.get("kind") != "track": return {"platform": "SoundCloud", "status": "not_a_track", "kind": t.get("kind"), "title": t.get("title") or t.get("username")}
    f = {"platform": "SoundCloud", "status": "ok", "title": t.get("title"), "artist": (t.get("user") or {}).get("username"),
         "artist_url": (t.get("user") or {}).get("permalink_url"), "duration_seconds": round((t.get("duration") or 0) / 1000) or None,
         "genre": t.get("genre"), "tags": t.get("tag_list"), "posted": (t.get("created_at") or "")[:10],
         "download_button": bool(t.get("downloadable") and t.get("has_downloads_left", True)),
         "buy_link": t.get("purchase_url"), "buy_link_title": t.get("purchase_title"),
         "description": (t.get("description") or "")[:3000]}
    f["tracklist"] = tracklist_from_text(t.get("description"))
    if f["download_button"]:
        code, d = sc_api(f"/tracks/{t['id']}/download")
        if d and d.get("redirectUri"): f["download_url"] = d["redirectUri"]
    return f

def facts_mixcloud(url):
    path = urllib.parse.urlparse(url).path
    try:
        status, _, body = http("https://api.mixcloud.com" + path)
    except urllib.error.HTTPError as e:
        return {"platform": "Mixcloud", "status": "removed" if e.code == 404 else f"http {e.code}"}
    t = json.loads(body)
    return {"platform": "Mixcloud", "status": "ok", "title": t.get("name"), "artist": (t.get("user") or {}).get("name"),
            "artist_url": (t.get("user") or {}).get("url"), "duration_seconds": t.get("audio_length"),
            "tags": ", ".join(x.get("name", "") for x in t.get("tags", [])), "posted": (t.get("created_time") or "")[:10],
            "download_button": False, "description": (t.get("description") or "")[:3000],
            "tracklist": tracklist_from_text(t.get("description"))}

def facts_generic(url):
    try:
        status, ctype, body = http(url)
    except Exception as e:
        return {"platform": urllib.parse.urlparse(url).netloc, "status": f"unreachable: {e}"}
    if ctype.startswith("audio/"): return {"platform": urllib.parse.urlparse(url).netloc, "status": "ok", "download_button": True, "download_url": url, "content_type": ctype}
    h = body[:400000].decode("utf-8", "replace")
    def meta(*names):
        for n in names:
            m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(n), h, re.I) or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(n), h, re.I)
            if m: return m.group(1)
    title = re.search(r"<title>(.*?)</title>", h, re.S | re.I)
    return {"platform": urllib.parse.urlparse(url).netloc, "status": "ok", "title": (meta("og:title") or (title.group(1).strip() if title else None)),
            "description": (meta("og:description", "description") or "")[:2000], "audio_meta": meta("og:audio"),
            "feed": (re.search(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"', h, re.I) or [None, None])[1],
            "download_button": False}

def facts_for(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    try:
        if "soundcloud.com" in host: return facts_soundcloud(url)
        if "mixcloud.com" in host: return facts_mixcloud(url)
        return facts_generic(url)
    except Exception as e:
        return {"platform": host, "status": f"facts failed: {e}"}


# ---------- the model ----------

def price(model, usage):
    inp, out = PRICES.get(model, (5.0, 25.0))
    return (usage["input"] * inp + usage["cache_read"] * inp * 0.1 + usage["cache_write"] * inp * 1.25 + usage["output"] * out) / 1e6

def ask(prompt, schema, model, tool_set, max_continuations=5):
    """One agentic request: web search/fetch run server-side; resume on pause_turn; JSON-constrained final answer."""
    if FAKE:   # canned answers from ./fake/*.json so the app flow can be exercised without the API
        name = "lookup" if schema is LOOKUP_SCHEMA else "discover"
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake", name + ".json")))
    import anthropic
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0, "fetches": 0, "rounds": 0}
    t0 = time.time()
    for _ in range(max_continuations + 1):
        response = client.messages.create(
            model=model, max_tokens=16000, tools=tool_set, messages=messages,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"}, output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
        )
        u = response.usage
        usage["input"] += u.input_tokens; usage["output"] += u.output_tokens; usage["rounds"] += 1
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0; usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu: usage["searches"] += getattr(stu, "web_search_requests", 0) or 0; usage["fetches"] += getattr(stu, "web_fetch_requests", 0) or 0
        if response.stop_reason == "pause_turn":
            messages = messages + [{"role": "assistant", "content": response.content}]
            continue
        usage.update(model=response.model, seconds=round(time.time() - t0, 1)); usage["cost_usd"] = round(price(model, usage), 3)
        if response.stop_reason == "refusal":
            return {"error": "refused", "detail": response.stop_details and str(response.stop_details), "_usage": usage}
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return {"error": "no answer", "stop_reason": response.stop_reason, "_usage": usage}
        out = json.loads(text); out["_usage"] = usage
        return out
    return {"error": "still paused after continuations", "_usage": usage}


def lookup(url):
    t0 = time.time()
    facts = facts_for(url)
    # The page itself offers the file (Download button, or a free-download link the uploader posted): answer without a model call.
    free_link = bool(facts.get("buy_link") and re.search(r"download|free|dl\b", facts.get("buy_link_title") or "", re.I))
    if facts.get("download_button") or free_link:
        cands = []
        if facts.get("download_url"):
            cands.append({"url": facts["download_url"], "kind": "audio", "source": f"{facts['platform']} Download button", "confidence": 1.0,
                          "same_recording": True, "note": "The uploader enabled downloads on the page itself."})
        elif facts.get("download_button"):   # SoundCloud only hands the file to a signed-in user, so the page is the candidate and the person taps the button in Cue's browser.
            cands.append({"url": url, "kind": "page", "source": f"{facts['platform']} Download button", "confidence": 1.0, "same_recording": True,
                          "note": f"The uploader enabled downloads. Get file opens the page inside Cue; sign in to {facts['platform']} if asked and tap Download.",
                          "get_url": url, "get_label": f"Get file ({facts['platform']} Download)"})
        if facts.get("buy_link"):
            g = get_link({"buy_link": facts["buy_link"], "page_url": url, "source": "", "downloadable": True})
            if g.get("kind") == "audio":
                cands.insert(0, {"url": g["download_url"], "kind": "audio", "source": facts.get("buy_link_title") or "Download link on the page", "confidence": 0.9,
                                 "same_recording": True, "note": "The free-download link on the page leads straight to the file."})
            else:
                cands.append({"url": facts["buy_link"], "kind": "page", "source": facts.get("buy_link_title") or "Link on the page", "confidence": 0.7,
                              "same_recording": True, "note": "The free-download link the uploader put on the page. Get file opens it inside Cue and catches the download.",
                              "get_url": g.get("get_url"), "get_label": g.get("get_label")})
        return {"title": facts.get("title") or url, "artist": facts.get("artist") or "", "duration_seconds": facts.get("duration_seconds"),
                "candidates": cands, "tracklist": facts.get("tracklist", []),
                "summary": f"{facts['platform']} offers this mix for download on the page itself.",
                "facts": facts, "_usage": {"model": None, "cost_usd": 0.0, "seconds": round(time.time() - t0, 1)}}
    shown = {k: v for k, v in facts.items() if v not in (None, "", [], False)}
    prompt = f"""Find legitimate download options and the tracklist for this DJ mix: {url}

Verified facts the service already fetched from the platform (trust these over search snippets; do not fetch this page again):
{json.dumps(shown, indent=1, ensure_ascii=False)}

{"The page is gone, so only copies elsewhere can help. " if facts.get("status") == "removed" else ""}Search for the same mix on the DJ's site, Bandcamp, hearthis.at, archive.org and podcast feeds (many mix series are podcasts),
and follow any buy/free-download link above if it leads to a file host. Return every candidate with a confidence 0-1 and
whether it is the same recording. If the facts include a tracklist, return it as given (cleaned), do not search for one."""
    out = ask(prompt, LOOKUP_SCHEMA, LOOKUP_MODEL, LOOKUP_TOOLS)
    if "error" not in out:
        out["facts"] = facts
        if not out.get("tracklist") and facts.get("tracklist"): out["tracklist"] = facts["tracklist"]
        if facts.get("title") and not out.get("title"): out["title"] = facts["title"]
    return out


def discover(profile, count, query=""):
    steer = f"\nWhat the listener asked for right now, which outranks the profile: \"{query.strip()}\"\n" if query and query.strip() else ""
    prompt = f"""Recommend {count} DJ mixes for this listener, favouring ones that can be legitimately downloaded
(podcast feeds, Bandcamp, DJ sites, archive.org, pages with a Download button).{steer}
Listener profile (from their library: identified songs, DJs, genres, years, lengths, what they finish and what they skip):

{json.dumps(profile, indent=1, ensure_ascii=False)[:6000]}

For each pick give the page, the direct download URL when one exists (an MP3/M4A enclosure or file), the kind, the
source site, length, year, and a one-line 'why' that names the concrete overlap with the request and the profile (shared
songs, same DJ, same festival/series, adjacent artists). Prefer variety across DJs and years over many mixes from one series."""
    return ask(prompt, DISCOVER_SCHEMA, DISCOVER_MODEL, DISCOVER_TOOLS)


# ---------- discovery, Jev engine: free search APIs find candidates, Jev judges them, no browsing loop ----------

JEV_KEY_FILE = os.path.expanduser("~/.typesafe_key")
JEV_MODEL = os.environ.get("CUE_JEV_MODEL", "jev-latest")
DISCOVER_ENGINE = os.environ.get("CUE_DISCOVER_ENGINE", "jev")
MIN_MIX_MINUTES = 20

def jev(state, questions, timeout=30):
    key = os.environ.get("TYPESAFE_API_KEY") or open(JEV_KEY_FILE).read().strip()
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=json.dumps({"state": state, "model": JEV_MODEL, "questions": questions}).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def strip_counts(items): return [re.sub(r"\s*×\d+$", "", x) for x in items if isinstance(x, str)]

def search_terms(profile, query):
    """Which searches to run. The steer is split into its parts; without one, the library's DJs and genres drive it."""
    terms = []
    if query:
        terms.append(query)
        parts = [p.strip(" .") for p in re.split(r",|\bor\b|\band\b|\bwith\b|\bby\b|\bideally\b|\bin it\b", query, flags=re.I)]
        parts = [re.sub(r"^(a|an|the|some)\s+", "", p, flags=re.I) for p in parts if 2 < len(p) < 40]
        parts = [p for p in parts if p.lower() not in {"them", "it", "me", "one", "something", "anything", "mixtape", "mix", "mixes", "set", "sets"}]
        for p in parts:
            terms += [p + " mix", p + " live set"]
    else:
        for dj in strip_counts(profile.get("djs", []))[:5]: terms.append(dj + " mix")
        for g in strip_counts(profile.get("genres", []))[:4]: terms.append(g + " mix")
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if k not in seen: seen.add(k); out.append(t)
    return out[:10]

def cand_soundcloud(term):
    s, b = http(f"https://api-v2.soundcloud.com/search/tracks?q={urllib.parse.quote(term)}&client_id={sc_client_id()}&limit=30&filter.duration=epic")[0::2]
    out = []
    for x in json.loads(b).get("collection", []):
        mins = round((x.get("duration") or 0) / 60000)
        if mins < MIN_MIX_MINUTES or x.get("kind") != "track": continue
        button = bool(x.get("downloadable") and x.get("has_downloads_left", True))
        free_link = bool(x.get("purchase_url") and re.search(r"download|free|dl\b", x.get("purchase_title") or "", re.I))
        out.append({"title": x.get("title"), "dj": (x.get("user") or {}).get("username"), "page_url": x.get("permalink_url"), "download_url": None,
                    "kind": "page", "source": "SoundCloud" + (" (Download enabled)" if button else " (free download link on the page)" if free_link else ""),
                    "downloadable": button or free_link, "duration_minutes": mins,
                    "year": int((x.get("created_at") or "0000")[:4]) or None, "genre": x.get("genre"), "tags": (x.get("tag_list") or "")[:200],
                    "plays": x.get("playback_count"), "likes": x.get("likes_count"), "description": (x.get("description") or "")[:500],
                    "buy_link": x.get("purchase_url"), "buy_link_title": x.get("purchase_title")})
    return out

def cand_mixcloud(term):
    s, b = http(f"https://api.mixcloud.com/search/?q={urllib.parse.quote(term)}&type=cloudcast&limit=20")[0::2]
    out = []
    for x in json.loads(b).get("data", []):
        mins = round((x.get("audio_length") or 0) / 60)
        if mins < MIN_MIX_MINUTES: continue
        out.append({"title": x.get("name"), "dj": (x.get("user") or {}).get("name"), "page_url": x.get("url"), "download_url": None, "kind": "page",
                    "source": "Mixcloud", "downloadable": False, "duration_minutes": mins, "year": int((x.get("created_time") or "0000")[:4]) or None,
                    "genre": None, "tags": ", ".join(t.get("name", "") for t in x.get("tags", []))[:200], "plays": x.get("play_count"), "likes": x.get("favorite_count"), "description": ""})
    return out

def cand_archive(term):
    q = urllib.parse.quote(f'({term}) AND mediatype:(audio) AND (subject:(mix) OR subject:(dj) OR subject:(set) OR title:(mix) OR title:(set))')
    s, b = http(f"https://archive.org/advancedsearch.php?q={q}&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=description&fl[]=downloads&fl[]=year&fl[]=subject&rows=15&output=json&sort[]=downloads+desc")[0::2]
    out = []
    for x in json.loads(b).get("response", {}).get("docs", []):
        d = x.get("description"); d = " ".join(d) if isinstance(d, list) else (d or "")
        subj = x.get("subject"); subj = ", ".join(subj) if isinstance(subj, list) else (subj or "")
        cr = x.get("creator"); cr = ", ".join(cr) if isinstance(cr, list) else (cr or "")
        out.append({"title": x.get("title"), "dj": cr or "unknown", "page_url": f"https://archive.org/details/{x['identifier']}", "download_url": None, "kind": "page",
                    "source": "archive.org (free download)", "downloadable": True, "duration_minutes": None, "year": int(str(x.get("year") or "0")[:4]) or None,
                    "genre": None, "tags": subj[:200], "plays": x.get("downloads"), "likes": None, "description": re.sub(r"<[^>]+>", " ", d)[:500], "archive_id": x["identifier"]})
    return out

def archive_audio_url(identifier):
    """The largest MP3 in an archive.org item, so the app can download it directly."""
    try:
        s, b = http(f"https://archive.org/metadata/{identifier}")[0::2]
        files = [f for f in json.loads(b).get("files", []) if f.get("name", "").lower().endswith((".mp3", ".m4a"))]
        if not files: return None, None
        f = max(files, key=lambda f: int(f.get("size") or 0))
        return f"https://archive.org/download/{identifier}/{urllib.parse.quote(f['name'])}", (int(float(f.get("length") or 0)) // 60 or None)
    except Exception:
        return None, None

def archive_tracks(identifier):
    """Every MP3 in an archive.org item in set order: [{url, title, seconds}]. Live Music Archive shows come as one file per
    song, so the crate concatenates them and writes the setlist in as chapters; a single-file item is just the one track."""
    try:
        s, b = http(f"https://archive.org/metadata/{identifier}")[0::2]
        m = json.loads(b); files = [f for f in m.get("files", []) if f.get("name", "").lower().endswith(".mp3")]
        vbr = [f for f in files if "MP3" in (f.get("format") or "")]
        files = vbr or files
        def secs(f):
            p = str(f.get("length") or "0")
            return float(p) if ":" not in p else sum(float(x) * 60 ** k for k, x in enumerate(reversed(p.split(":"))))
        def order(f):
            t = re.sub(r"\D", "", str(f.get("track") or ""))
            return (0, int(t)) if t else (1, f["name"])
        files.sort(key=order)
        return [{"url": f"https://archive.org/download/{identifier}/{urllib.parse.quote(f['name'])}",
                 "title": (f.get("title") or re.sub(r"\.mp3$", "", f["name"], flags=re.I)).strip(), "seconds": secs(f)} for f in files]
    except Exception:
        return []

GATE_HOSTS = ("hypeddit.com", "toneden.io", "fanlink.to", "linktr.ee", "lnk.to", "ffm.to", "bfan.link", "hypel.ink", "gate.fm", "click.dj")

def get_link(c):
    """Where the person can get the file themselves, for the app's in-app 'Get file' browser. Resolves file-host links to a
    direct download when that needs no interaction (Dropbox, Google Drive, a bare audio URL); labels gates and Download buttons."""
    link = c.get("buy_link")
    if link:
        try:
            req = urllib.request.Request(link, headers={"User-Agent": UA}, method="HEAD")
            r = urllib.request.urlopen(req, timeout=10); final, ctype = r.url, r.headers.get("Content-Type", "")
        except Exception:
            final, ctype = link, ""
        host = urllib.parse.urlparse(final).netloc.lower()
        if ctype.startswith("audio/"):
            c["download_url"], c["kind"], c["downloadable"] = final, "audio", True; return c
        if "dropbox.com" in host:
            u = urllib.parse.urlparse(final); q = dict(urllib.parse.parse_qsl(u.query)); q["dl"] = "1"
            c["download_url"], c["kind"], c["downloadable"] = u._replace(query=urllib.parse.urlencode(q)).geturl(), "audio", True; return c
        m = re.search(r"drive\.google\.com/(?:file/d/([^/]+)|open\?id=([^&]+))", final)
        if m:
            c["download_url"], c["kind"], c["downloadable"] = f"https://drive.google.com/uc?export=download&confirm=t&id={m.group(1) or m.group(2)}", "audio", True; return c
        gate = next((g for g in GATE_HOSTS if g in host), None)
        path = urllib.parse.urlparse(final).path
        # Hypeddit "smart links" (hypeddit.com/<artist>/<slug>) are streaming pages, only /track/<id> is a download gate.
        if gate == "hypeddit.com" and not path.startswith("/track/"):
            return c
        c["get_url"] = final
        c["get_label"] = f"Get file via {gate.split('.')[0].title()}" if gate else "Get file"
        c["downloadable"] = True
        return c
    if c.get("downloadable") and "Download enabled" in (c.get("source") or ""):
        c["get_url"], c["get_label"] = c["page_url"], "Get file (SoundCloud Download)"
    return c


def discover_jev(profile, count, query=""):
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time(); usage = {"engine": "jev", "model": None, "jev_requests": 0, "input": 0, "output": 0, "searches": 0, "candidates": 0, "cost_usd": 0.0}
    terms = search_terms(profile, query)
    jobs = [(f, t) for t in terms for f in (cand_soundcloud, cand_mixcloud, cand_archive)]
    cands, seen = [], set()
    def run(job):
        f, term = job
        try: return f(term)
        except Exception as e: log(f"search failed {f.__name__} {term!r}: {e}"); return []
    with ThreadPoolExecutor(8) as ex:
        for j, rows in enumerate(ex.map(run, jobs)):
            usage["searches"] += 1
            for c in rows:
                if c["page_url"] in seen: continue
                seen.add(c["page_url"]); c["_job"] = j; cands.append(c)
    # Already in the library: drop by title match.
    norm = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()
    have = set()
    for t in profile.get("library_titles", []):
        have.add(norm(t)); have.add(norm(t.split(" – ", 1)[-1]))   # "DJ – Title" and bare title
    cands = [c for c in cands if norm(f"{c['dj']} {c['title']}") not in have and norm(c["title"]) not in have]
    # Cap by taking the best-known rows from every search in turn, so a late search term is not squeezed out.
    by_job, order = collections.defaultdict(list), []
    for c in cands:
        by_job[c.get("_job")].append(c)
    for rows in by_job.values(): rows.sort(key=lambda c: -(c.get("plays") or 0))
    for i in range(30):
        for rows in by_job.values():
            if i < len(rows): order.append(rows[i])
    cands = order[:200]
    usage["candidates"] = len(cands)
    t_search = round(time.time() - t0, 1)
    listener = {"request": query or "(no specific request; go by the library)", "djs": strip_counts(profile.get("djs", []))[:12],
                "genres": strip_counts(profile.get("genres", []))[:10], "years": strip_counts(profile.get("years", []))[:6],
                "songs_in_library": strip_counts(profile.get("songs_in_library", []))[:20], "saved_songs": profile.get("saved_songs", [])[:15]}
    def judge(batch):
        state = {"listener": listener, "candidates": [{k: c.get(k) for k in ("title", "dj", "source", "duration_minutes", "year", "genre", "tags", "plays", "description")} for c in batch]}
        qs = {}
        for i in range(len(batch)):
            qs[f"fit{i}"] = {"type": "score", "instructions": f"How well does `candidates[{i}]` match what the listener asked for in `listener.request`, and secondarily their taste in `listener.djs`, `listener.genres` and `listener.songs_in_library`? A named artist in the request must be the DJ or clearly featured, not just mentioned.",
                             "criteria": ["Unrelated, or a different meaning of the words", "Loosely related genre or scene", "Matches the genre or an adjacent artist the listener would plausibly like", "Matches the request closely: the named artist or exact genre, in the format asked for"]}
            qs[f"mix{i}"] = {"type": "noul", "instructions": f"Is `candidates[{i}]` a full DJ mix, mixtape, radio show or live set (a continuous long recording of many songs) rather than a single song, album, audiobook or talk?"}
            if query:
                qs[f"own{i}"] = {"type": "noul", "instructions": f"Is `candidates[{i}]` made by an artist or DJ that `listener.request` names (the DJ field or title says it is their own mix or set), as opposed to someone else's mix that merely includes their songs?"}
        r = jev(state, qs)
        return batch, r
    scored = []
    with ThreadPoolExecutor(6) as ex:
        for batch, r in ex.map(judge, [cands[i:i + 8] for i in range(0, len(cands), 8)]):
            usage["jev_requests"] += 1; usage["input"] += r["usage"]["input_tokens"]; usage["output"] += r["usage"]["output_tokens"]; usage["model"] = r.get("model")
            for i, c in enumerate(batch):
                fit = r["answers"][f"fit{i}"]["score"]; mix = r["answers"][f"mix{i}"]["noul"]
                own = r["answers"].get(f"own{i}", {}).get("noul", 0.0)
                c["fit"], c["is_mix"], c["own"] = round(fit, 2), round(mix, 2), round(own, 2)
                plays = c.get("plays")
                c["rank"] = fit * mix + 0.8 * own + (0.4 if c["downloadable"] else 0) + (0.15 if (plays or 0) > 1000 else 0) - (0.6 if plays is not None and plays < 50 else 0)
                scored.append(c)
    scored.sort(key=lambda c: -c["rank"])
    picks, per_dj = [], {}
    for c in scored:
        if c["is_mix"] < 0.5 or c["fit"] < 1.2: continue
        if per_dj.get(c["dj"], 0) >= 2: continue
        per_dj[c["dj"]] = per_dj.get(c["dj"], 0) + 1
        picks.append(c)
        if len(picks) >= count: break
    for c in picks:   # resolve a direct file for archive.org picks so Add works; label gates and Download buttons for Get file
        if c.get("archive_id"):
            url, mins = archive_audio_url(c["archive_id"])
            if url: c["download_url"], c["kind"] = url, "audio"
            if mins and not c["duration_minutes"]: c["duration_minutes"] = mins
        else:
            get_link(c)
    def why(c):
        bits = [f"fit {c['fit']:.1f}/3" + (f" for “{query}”" if query else " for your library")]
        if c.get("own", 0) >= 0.6: bits.append(f"by {c['dj']} themselves")
        if c.get("genre"): bits.append(c["genre"])
        if c.get("duration_minutes"): bits.append(f"{c['duration_minutes']} min")
        if c.get("plays"): bits.append(f"{c['plays']:,} plays")
        if c["kind"] == "audio": bits.append("direct download")
        elif c["downloadable"]: bits.append("Download enabled on the page")
        return " · ".join(bits)
    out = {"picks": [{"title": c["title"], "dj": c["dj"], "page_url": c["page_url"], "download_url": c["download_url"], "kind": c["kind"], "source": c["source"],
                      "duration_minutes": c["duration_minutes"], "year": c["year"], "why": why(c), "confidence": round(min(1.0, c["fit"] / 3), 2),
                      "get_url": c.get("get_url"), "get_label": c.get("get_label")} for c in picks],
           "summary": f"{len(cands)} candidates from {usage['searches']} searches ({t_search}s), judged by Jev in {usage['jev_requests']} requests; "
                      f"{sum(1 for c in picks if c['kind'] == 'audio' or c['downloadable'])} of {len(picks)} picks are downloadable."}
    usage["seconds"] = round(time.time() - t0, 1); usage["rounds"] = 0; usage["fetches"] = 0; usage["cache_read"] = 0
    out["_usage"] = usage
    return out


# ---------- fetch through a gate with the Mac's own Chrome (gates.py), then serve the file to the phone ----------

INBOX = os.path.expanduser("~/Mixtapes/inbox")
GATE_EMAIL = os.environ.get("CUE_GATE_EMAIL", "")
FETCH_INDEX = os.path.join(INBOX, ".fetched.json")   # gate url -> file name, so a second Get file is instant
_fetch_lock = __import__("threading").Lock()

def fetch_via_mac(url, email):
    idx = json.load(open(FETCH_INDEX)) if os.path.exists(FETCH_INDEX) else {}
    if url in idx and os.path.exists(os.path.join(INBOX, idx[url])): return {"file": idx[url], "cached": True}
    import gates
    with _fetch_lock:   # one Chrome, one gate at a time
        r = gates.fetch(url, email or GATE_EMAIL)
    if r.get("file"):
        name = os.path.basename(r["file"]); idx[url] = name; json.dump(idx, open(FETCH_INDEX, "w"))
        return {"file": name, "log": r.get("log")}
    return {"error": r.get("error") or "no file", "needs_login": r.get("needs_login"), "log": r.get("log")}


# ---- The crate: mixes the Mac has already collected (crate.py). The phone browses this list and downloads only what Seth picks. ----
CRATE_DIR = os.path.expanduser("~/Mixtapes/crate")
CRATE_FILES = os.path.join(CRATE_DIR, "files"); CRATE_PHONE = os.path.join(CRATE_DIR, "phone"); CRATE_DB = os.path.join(CRATE_DIR, "catalog.sqlite")

def crate_file_path(name):
    """Prefer the phone-friendly transcode (m4a) when the original is lossless or huge."""
    stem = os.path.splitext(name)[0]
    for cand in (os.path.join(CRATE_PHONE, stem + ".m4a"), os.path.join(CRATE_FILES, name)):
        if os.path.isfile(cand): return cand
    return os.path.join(CRATE_FILES, name)

FESTIVALISH = re.compile(r"(festival|records?|recordings|radio|podcast|music|sounds|collective|crew|label|official|fm|live|sessions?|events?|presents|bass|camp)\b", re.I)
NOT_A_NAME = re.compile(r"\b(mix|mixtape|set|live|festival|podcast|radio|episode|ep\.?|vol\.?|volume|session|part|pt\.?|20\d\d|19\d\d|sunrise|sunset|closing|opening)\b", re.I)

def clean_credit(title, uploader):
    """Pull the DJ out of the title when the uploader is a festival, label or podcast, and drop that prefix from the title.
    'JPOD Live at Bass Coast 2022' by 'Bass Coast Festival' -> ('JPOD', 'Live at Bass Coast 2022'); 'www.Stickybuds.com' -> 'Stickybuds'."""
    t = re.sub(r"\s+", " ", title or "").strip(); artist = (uploader or "").strip()
    artist = re.sub(r"^www\.|\.(com|net|fm|io)$", "", artist, flags=re.I).strip()
    m = re.match(r"^(.{2,40}?)\s*(?:\s[-–—|:]\s|\s[-–—]|[–—])\s*(.+)$", t)
    if m and not NOT_A_NAME.search(m.group(1)):
        artist, t = m.group(1).strip(), m.group(2).strip()
    else:
        m = re.match(r"^(.{2,40}?)\s+(?:live\s+)?(?:@|at)\s+.+$", t, re.I)
        if m and not NOT_A_NAME.search(m.group(1)) and (not artist or FESTIVALISH.search(artist)):
            artist, t = m.group(1).strip(), t[len(m.group(1)):].strip()
    if artist and t.lower().startswith(artist.lower()):   # "Stickybuds - Stickybuds - Fractal Forest" style doubling
        rest = t[len(artist):].lstrip(" -–—|:").strip()
        if rest: t = rest
    return artist or (uploader or "").strip(), t or title

def crate_items(q="", limit=500):
    import sqlite3
    if not os.path.exists(CRATE_DB): return []
    db = sqlite3.connect(CRATE_DB); db.row_factory = sqlite3.Row
    rows = db.execute("select url,source,title,dj,minutes,year,genre,tags,plays,rank,fit,file,fetched_at,description from mixes where status='fetched' and file is not null order by rank desc").fetchall()
    out = []; ql = q.lower().strip()
    for r in rows:
        name = os.path.basename(r["file"]); path = crate_file_path(name)
        if not os.path.isfile(path): continue
        hay = " ".join(str(r[k] or "") for k in ("title", "dj", "genre", "tags", "year")).lower()
        if ql and not all(w in hay for w in ql.split()): continue
        served = os.path.basename(path)
        dj, title = clean_credit(r["title"], r["dj"])
        out.append({"page_url": r["url"], "source": r["source"], "title": title, "dj": dj, "uploader": r["dj"], "minutes": r["minutes"], "year": r["year"],
                    "genre": r["genre"], "plays": r["plays"], "rank": round(r["rank"] or 0, 2), "fit": r["fit"], "fetched_at": r["fetched_at"],
                    "name": served, "bytes": os.path.getsize(path), "download_url": "/crate/files/" + urllib.parse.quote(served),
                    "tracklist": tracklist_from_text(r["description"])[:80]})   # the DJ's own tracklist, when the page had one: labels the on-device cue detection
        if len(out) >= limit: break
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): log(fmt % args)
    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def stream_file(self, path):
        if not os.path.isfile(path): return self.send(404, {"error": "no such file"})
        size = os.path.getsize(path); low = path.lower()
        ctype = "audio/mpeg" if low.endswith(".mp3") else "audio/mp4" if low.endswith((".m4a", ".mp4")) else "audio/wav" if low.endswith(".wav") else "audio/flac" if low.endswith(".flac") else "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(size)); self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk: break
                self.wfile.write(chunk)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); qs = urllib.parse.parse_qs(u.query)
        if u.path == "/health": return self.send(200, {"ok": True, "lookup_model": LOOKUP_MODEL, "discover_model": DISCOVER_MODEL, "effort": EFFORT, "fake": FAKE, "mac_fetch": True, "crate": True})
        if u.path.startswith("/files/"):
            return self.stream_file(os.path.join(INBOX, os.path.basename(urllib.parse.unquote(u.path[len("/files/"):]))))
        if u.path.startswith("/crate/files/"):
            name = os.path.basename(urllib.parse.unquote(u.path[len("/crate/files/"):]))
            return self.stream_file(crate_file_path(name))
        if u.path == "/crate/pushed":   # files crate.py push once copied into the phone's library, so the app can move them out of favorites
            pj = os.path.join(CRATE_DIR, "pushed.json")
            return self.send(200, json.load(open(pj)) if os.path.exists(pj) else [])
        if u.path == "/crate/wanted":   # what the phone asked for and where each one stands
            import sqlite3; db = sqlite3.connect(CRATE_DB); db.row_factory = sqlite3.Row
            rows = db.execute("select url,title,dj,status,note,file from mixes where note like 'wanted from phone%' or status in ('wanted','wanted_failed') order by found_at desc").fetchall()
            return self.send(200, {"items": [dict(r) for r in rows]})
        if u.path == "/crate":
            return self.send(200, {"items": crate_items(qs.get("q", [""])[0], int(qs.get("limit", ["500"])[0]))})
        self.send(404, {"error": "not found"})
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/crate/want":
                import crate
                st = crate.want(body["url"], body.get("title", ""), body.get("dj", ""), body.get("source", ""), body.get("minutes"), body.get("year"))
                if st == "wanted" and not glob.glob(os.path.join(CRATE_DIR, ".fetch.lock")):
                    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "crate.py"), "fetch", "--max", "3"],
                                     stdout=open(os.path.join(CRATE_DIR, "want.log"), "ab"), stderr=subprocess.STDOUT)
                return self.send(200, {"status": st})
            if self.path == "/lookup":
                url = (body.get("url") or "").strip()
                if not url.startswith("http"): return self.send(400, {"error": "url required"})
                key = cache_key("lookup", {"url": url})
                if os.path.exists(key) and not body.get("refresh"): return self.send(200, json.load(open(key)))
                out = lookup(url)
            elif self.path == "/fetch":
                url = (body.get("url") or "").strip()
                if not url.startswith("http"): return self.send(400, {"error": "url required"})
                t0 = time.time(); r = fetch_via_mac(url, (body.get("email") or "").strip())
                log(f"/fetch {'ok ' + r['file'] if r.get('file') else 'FAILED ' + str(r.get('error'))} {round(time.time() - t0, 1)}s")
                if r.get("file"): return self.send(200, {"download_url": f"/files/{urllib.parse.quote(r['file'])}", "name": r["file"], "cached": r.get("cached", False)})
                return self.send(502, r)
            elif self.path == "/discover":
                profile = body.get("profile") or {}; count = int(body.get("count") or 8); query = (body.get("query") or "").strip()
                engine = body.get("engine") or DISCOVER_ENGINE
                key = cache_key("discover", {"profile": profile, "count": count, "query": query, "engine": engine})
                if os.path.exists(key) and not body.get("refresh"): return self.send(200, json.load(open(key)))
                out = discover_jev(profile, count, query) if engine == "jev" else discover(profile, count, query)
            else:
                return self.send(404, {"error": "not found"})
            u = out.get("_usage") or {}
            log(f"{self.path} {'ERROR ' + str(out.get('error')) if 'error' in out else 'ok'} model={u.get('model')} in={u.get('input', 0)} out={u.get('output', 0)} "
                f"cache_read={u.get('cache_read', 0)} searches={u.get('searches', 0)} fetches={u.get('fetches', 0)} rounds={u.get('rounds', 0)} {u.get('seconds', 0)}s ~${u.get('cost_usd', 0)}")
            if "error" not in out: json.dump(out, open(key, "w"))
            self.send(200 if "error" not in out else 502, out)
        except Exception as e:
            traceback.print_exc(); self.send(500, {"error": str(e)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    if not FAKE and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.path.isdir(os.path.expanduser("~/.config/anthropic/credentials"))):
        log("warning: no API key and no `ant auth login` profile; requests will fail until one exists")
    log(f"cue-service on 127.0.0.1:{port} lookup={LOOKUP_MODEL} discover={DISCOVER_MODEL} effort={EFFORT} fake={FAKE}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
