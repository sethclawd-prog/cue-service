# cue-service: run a mixtape collector for the Cue app from your own Mac

This is the machine-side half of Cue. The iPhone app plays mixtapes and skips song by song; this service finds mixtapes for your taste, collects the ones that are offered for download, and serves them to the phone one tap at a time. It is meant to be run by *you* (or your Claude Code / local agent) on your own Mac with your own logins. Nothing here rips streams.

## What the app expects

The app talks to one base URL (For you → "Mac or agent address"; default is Seth's Mac). Endpoints, all JSON:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | liveness |
| `GET /crate?q=<words>` | mixes already collected on this Mac (`items`: title, dj, minutes, year, genre, plays, name, bytes, download_url, tracklist). The phone shows these under "On your Mac"; Add downloads one. |
| `GET /crate/files/<name>` | the audio file (Range requests supported; prefers `phone/<stem>.m4a`) |
| `POST /crate/want {url,title,dj,source,minutes,year}` | the listener wants a mix the service has only seen; next `crate.py fetch` tries to get it |
| `GET /crate/wanted` | status of those requests |
| `POST /discover {profile,count,query}` | picks for a taste profile (engine `jev` by default: free-API search + TypeSafe Jev judging; `agent` = Claude browsing) |
| `POST /lookup {url}` | facts about one SoundCloud/Mixcloud page and where a legitimate download is |

The phone never sends audio, only a taste summary (DJs, genres, songs it has identified) and what it wants.

## Setup on a Mac

```sh
git clone <this repo> ~/src/cue-service && cd ~/src/cue-service
python3 -m venv .venv && .venv/bin/pip install anthropic   # everything else is the standard library
mkdir -p ~/Mixtapes/crate
cp fake/taste.example.json ~/Mixtapes/crate/taste.json   # then edit: your DJs, genres, festivals
./run.sh                                                 # 127.0.0.1:8731
```

Expose it to the phone with Tailscale (`tailscale serve --https=8445 --bg http://127.0.0.1:8731`) and paste `https://<your-mac>.<tailnet>.ts.net:8445` into the app. Free-download gates get the email in `CUE_GATE_EMAIL` or `~/.cue_gate_email` (yours; never a shared one). Keys: `~/.typesafe_key` for Jev (discovery), `ANTHROPIC_API_KEY` or `~/.anthropic_key` only for the `agent` engine and `/lookup`; the crate works without either.

## Collecting

```sh
.venv/bin/python crate.py discover --terms 25   # search SoundCloud, Mixcloud, archive.org, podcast feeds for taste.json; Jev scores fit
.venv/bin/python crate.py fetch --max 30        # download what is offered: archive.org, podcast enclosures, Dropbox/Drive links, SoundCloud Download buttons (needs ~/.soundcloud_token from your own logged-in browser), Hypeddit gates
.venv/bin/python crate.py status
```

`crate-nightly.sh` runs both; `launchd/com.sethcosmo.cue-crate.plist` schedules it. Files land in `~/Mixtapes/crate/files`, phone-friendly transcodes in `phone/`, the catalog in `catalog.sqlite` (table `mixes`, `status` = new / fetched / stream_only / gate_needs_login / wanted / failed …).

Live shows: `crate.py` joins an archive.org Live Music Archive show (one file per song) into one mixtape with the setlist as chapters, so the phone skips song to song without identifying anything. Queue one by inserting a row with `archive_id` and `status='wanted'`, or by tapping Want on it in the app.

## Rules the collector follows (keep them)

- Only files the uploader offers: Download buttons, free-download gates you complete yourself, archive.org, podcast enclosures, links the DJ put in the description. `stream_only` rows are recorded and never ripped.
- One taste, one Mac. Do not bulk-push into the phone; the app pulls what the listener taps.
- Keep model spend low: facts-first (platform APIs), Jev for judging, Claude only for the optional browsing engine and lookups.

## Letting Claude Code run it

Point a Claude Code session at this directory and ask for what you want ("find more Animal Collective live shows and queue the best five"). Useful facts for it:

- `crate.py discover` takes `--terms N`; steers come from `taste.json` (`djs`, `genres`, `festivals`, `steers`).
- To hand-queue a specific page: `.venv/bin/python crate.py want <url> [title] [dj]` (same as the app's Want button), or insert into `mixes` with `status='wanted'`.
- Archive.org search: `https://archive.org/advancedsearch.php?q=collection:(AnimalCollective)&fl[]=identifier,title,date&output=json` lists band-sanctioned shows; `service.archive_tracks(identifier)` returns the per-song MP3s in setlist order.
- After a fetch, the phone sees the new item on its next visit to For you; no push step.
