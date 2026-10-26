#!/usr/bin/env python3
"""
Daily Tech Workout
==================
Usage:  python main.py setup | publish | playlist | all

  setup     Build the website only (the Spotify login helper page + RSS feed).
  publish   Fetch tech news, write 3 podcast segments, turn them into audio,
            and build the website (RSS feed + MP3 files) in the "site" folder.
  playlist  Wait for Spotify to pick up today's episodes, then build a
            1-hour playlist: podcast, music, podcast, music, ...
  all       publish, then (after the workflow deploys the site) playlist.
"""
import os
import re
import sys
import base64
import json
import time
import random
import shutil
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from email.utils import format_datetime
from xml.sax.saxutils import escape

import requests
import feedparser
from mutagen.mp3 import MP3


# ----------------------------------------------------------------------------
# Settings (all can be overridden with environment variables in the workflow)
# ----------------------------------------------------------------------------
def env(name, default=""):
    value = os.getenv(name, "")
    return value.strip() if value and value.strip() else default


TZ = ZoneInfo(env("TZ_NAME", "America/New_York"))
PODCAST_TITLE = env("PODCAST_TITLE", "My Morning Tech Brief")
PODCAST_EMAIL = env("PODCAST_EMAIL", "")
HOST_NAME = env("HOST_NAME", "Alex")                      # the voice's on-air name
PLAYLIST_NAME = env("PLAYLIST_NAME", "Morning Workout")
TOTAL_MINUTES = int(env("TOTAL_MINUTES", "60"))           # playlist length
NUM_SEGMENTS = int(env("NUM_SEGMENTS", "3"))              # podcast parts
SEGMENT_MINUTES = float(env("SEGMENT_MINUTES", "7"))      # length of each part
RETAIN_DAYS = int(env("RETAIN_DAYS", "7"))                # keep old episodes online
BUILD_WAIT_MINUTES = int(env("BUILD_WAIT_MINUTES", "150"))
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")
# Google Cloud voices; the first that works is used. Chirp 3 HD and Neural2 each have
# 1 million free characters per month (this show uses roughly 600,000).
TTS_VOICES = [v for v in [env("TTS_VOICE", ""), "en-US-Chirp3-HD-Charon", "en-US-Neural2-D"] if v]
WORDS_PER_MINUTE = 150

repo = env("GITHUB_REPOSITORY", "you/tech-workout")
owner, repo_name = repo.split("/", 1)
SITE_URL = env("SITE_URL", f"https://{owner.lower()}.github.io/{repo_name}").rstrip("/")

SITE = Path("site")

NEWS_FEEDS = [
    ("Hacker News", "https://hnrss.org/frontpage?points=150"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
]


def log(*a):
    print(*a, flush=True)


def http(method, url, retries=4, **kw):
    """requests with simple retry for rate limits and server hiccups."""
    kw.setdefault("timeout", 60)
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kw)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            log(f"  network problem ({e}); retrying...")
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "5")) + 1
            log(f"  rate limited; waiting {wait}s")
            time.sleep(min(wait, 120))
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
            continue
        return r
    return r


def target_date():
    """The date of the morning this playlist is for (local time)."""
    now = dt.datetime.now(TZ)
    return (now + dt.timedelta(days=1)).date() if now.hour >= 15 else now.date()


def spoken_date(d):
    return f"{d:%A, %B} {d.day}"


# ----------------------------------------------------------------------------
# 1. NEWS
# ----------------------------------------------------------------------------
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z#0-9]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_news(max_age_hours=36, limit=70):
    cutoff = time.time() - max_age_hours * 3600
    items = []
    for source, url in NEWS_FEEDS:
        try:
            r = http("GET", url, retries=2, timeout=30, headers={"User-Agent": "tech-workout/1.0"})
            feed = feedparser.parse(r.content)
        except Exception as e:
            log(f"  could not read {source}: {e}")
            continue
        count = 0
        for e in feed.entries[:25]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and time.mktime(ts) < cutoff:
                continue
            items.append({
                "source": source,
                "title": strip_html(e.get("title", "")),
                "summary": strip_html(e.get("summary", ""))[:350],
            })
            count += 1
        log(f"  {source}: {count} recent stories")
    random.shuffle(items)
    return items[:limit]


# ----------------------------------------------------------------------------
# 2. SCRIPT (Claude)
# ----------------------------------------------------------------------------
def write_scripts(news, date):
    import anthropic

    words = int(SEGMENT_MINUTES * WORDS_PER_MINUTE)
    stories = "\n".join(f"- [{n['source']}] {n['title']}: {n['summary']}" for n in news)
    prompt = f"""You write a daily spoken tech-news show called "{PODCAST_TITLE}", hosted by {HOST_NAME}.
It is for someone listening while working out in the morning of {spoken_date(date)}. Upbeat, clear, conversational, energetic but not hyper.

Below are recent stories from several tech news sites. Pick the {NUM_SEGMENTS * 3} to {NUM_SEGMENTS * 4} most significant and interesting ones
(mix of AI, big tech, gadgets, science/space, security, and business; skip pure clickbait, deals, and duplicates).
Split them into {NUM_SEGMENTS} segments of about {words} words each. Do not repeat a story across segments.

Rules for the spoken text:
- Plain spoken English only. No markdown, no bullet points, no headings, no stage directions, no URLs, no emojis.
- Say numbers and symbols the way a person would speak them.
- Mention the source naturally ("according to The Verge") when it helps.
- Only state facts supported by the story text below; do not invent details or quotes. If something is unclear, say it is early reporting.
- Segment 1 opens with a short greeting that includes the date. The last segment ends with a short sign-off. Middle segments start and end briefly, as parts of one show (e.g. "Part two").
- Because music plays between segments, each segment should feel self-contained.

Output EXACTLY this format for each segment, nothing before or after:

=== SEGMENT 1 ===
TITLE: <short episode title, under 70 characters, no date>
SUMMARY: <one or two sentence description>
SCRIPT:
<the spoken text>

=== SEGMENT 2 ===
...

STORIES:
{stories}
"""
    client = anthropic.Anthropic()
    for attempt in range(2):
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=12000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        segs = parse_segments(text)
        if len(segs) >= NUM_SEGMENTS:
            return segs[:NUM_SEGMENTS]
        log(f"  Claude returned {len(segs)} usable segments; retrying...")
    raise RuntimeError("Could not get the podcast script from Claude in the expected format.")


def parse_segments(text):
    parts = re.split(r"^=== SEGMENT (\d+) ===\s*$", text, flags=re.M)
    segs = []
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1]
        t = re.search(r"^TITLE:\s*(.+)$", body, flags=re.M)
        s = re.search(r"^SUMMARY:\s*(.+)$", body, flags=re.M)
        sc = re.search(r"^SCRIPT:\s*\n(.+)", body, flags=re.M | re.S)
        if not (t and sc):
            continue
        script = clean_script(sc.group(1))
        if len(script.split()) < 100:
            continue
        segs.append({
            "title": t.group(1).strip()[:100],
            "summary": (s.group(1).strip() if s else t.group(1).strip()),
            "script": script,
        })
    return segs


def clean_script(s):
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[*#_`>]+", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ----------------------------------------------------------------------------
# 3. AUDIO (Google Cloud text-to-speech)
# ----------------------------------------------------------------------------
def chunk_text(text, limit=3000):
    chunks, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sentence) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += (" " if cur else "") + sentence
    if cur:
        chunks.append(cur)
    return chunks


def tts_request(text, voice, key):
    return http(
        "POST", f"https://texttospeech.googleapis.com/v1/text:synthesize?key={key}",
        json={
            "input": {"text": text},
            "voice": {"languageCode": "en-US", "name": voice},
            "audioConfig": {"audioEncoding": "MP3"},
        },
        timeout=180,
    )


def synthesize(text, out_path):
    key = env("GOOGLE_TTS_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_TTS_API_KEY is missing.")
    chunks = chunk_text(text)
    voice_ok = None
    audio = b""
    for chunk in chunks:
        r = None
        for voice in ([voice_ok] if voice_ok else TTS_VOICES):
            r = tts_request(chunk, voice, key)
            if r.status_code == 200:
                voice_ok = voice
                break
            log(f"  voice {voice} failed ({r.status_code}): {r.text[:200]}")
            if r.status_code in (401, 403):
                break  # key / billing problem: other voices will not help
        if r is None or r.status_code != 200:
            raise RuntimeError(
                f"Google text-to-speech failed ({r.status_code if r is not None else '?'}). "
                "Check that billing and the Text-to-Speech API are enabled and the key is correct.")
        audio += base64.b64decode(r.json()["audioContent"])
    Path(out_path).write_bytes(audio)
    log(f"  voice used: {voice_ok}")
    return MP3(str(out_path)).info.length


# ----------------------------------------------------------------------------
# 4. WEBSITE: RSS feed, cover art, old episodes
# ----------------------------------------------------------------------------
def make_cover(path):
    from PIL import Image, ImageDraw, ImageFont
    size = 3000
    img = Image.new("RGB", (size, size), (14, 22, 45))
    d = ImageDraw.Draw(img)
    for i in range(size):  # simple vertical gradient
        c = int(i / size * 60)
        d.line([(0, i), (size, i)], fill=(14 + c // 2, 22 + c // 3, 45 + c))
    big = ImageFont.load_default(size=260)
    small = ImageFont.load_default(size=150)
    words = PODCAST_TITLE.upper().split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) > 12 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    y = size // 2 - len(lines) * 170
    for ln in lines:
        wd = d.textlength(ln, font=big)
        d.text(((size - wd) / 2, y), ln, font=big, fill=(255, 255, 255))
        y += 330
    tag = "DAILY TECH NEWS"
    wd = d.textlength(tag, font=small)
    d.text(((size - wd) / 2, y + 120), tag, font=small, fill=(110, 200, 255))
    img.save(path, "JPEG", quality=88)


def build_feed(episodes):
    items = ""
    for e in sorted(episodes, key=lambda e: e["pub"], reverse=True):
        pub = dt.datetime.fromisoformat(e["pub"])
        items += f"""
    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['summary'])}</description>
      <itunes:summary>{escape(e['summary'])}</itunes:summary>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">{escape(e['guid'])}</guid>
      <enclosure url="{SITE_URL}/audio/{e['file']}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{int(e['seconds'])}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
    </item>"""
    email = escape(PODCAST_EMAIL or "you@example.com")
    desc = escape("A daily spoken roundup of the latest tech news, made for the morning workout.")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(PODCAST_TITLE)}</title>
    <link>{SITE_URL}/</link>
    <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{desc}</description>
    <language>en</language>
    <itunes:author>{escape(HOST_NAME)}</itunes:author>
    <itunes:owner><itunes:name>{escape(HOST_NAME)}</itunes:name><itunes:email>{email}</itunes:email></itunes:owner>
    <itunes:image href="{SITE_URL}/cover.jpg"/>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>{items}
  </channel>
</rss>
"""


def load_previous_episodes(today):
    """Fetch the episode list from the live site and re-download recent MP3s."""
    kept = []
    try:
        r = http("GET", f"{SITE_URL}/episodes.json", retries=2, timeout=30)
        if r.status_code != 200:
            log("  no previous episode list (first run?)")
            return kept
        previous = r.json()
    except Exception as e:
        log(f"  could not load previous episodes: {e}")
        return kept
    oldest = (today - dt.timedelta(days=RETAIN_DAYS)).isoformat()
    for e in previous:
        if e["date"] < oldest:
            continue
        try:
            a = http("GET", f"{SITE_URL}/audio/{e['file']}", retries=2, timeout=120)
            if a.status_code == 200:
                (SITE / "audio" / e["file"]).write_bytes(a.content)
                kept.append(e)
        except Exception as ex:
            log(f"  dropped {e['file']}: {ex}")
    log(f"  kept {len(kept)} earlier episodes")
    return kept


def build_site(generate):
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "audio").mkdir(parents=True)
    for f in Path("static").glob("*"):
        shutil.copy(f, SITE / f.name)
    make_cover(SITE / "cover.jpg")

    date = target_date()
    log(f"Target date: {date}")
    episodes = load_previous_episodes(date)
    have_today = [e for e in episodes if e["date"] == date.isoformat()]

    if generate and len(have_today) >= NUM_SEGMENTS:
        log("Episodes for this date already exist; skipping generation.")
    elif generate:
        episodes = [e for e in episodes if e["date"] != date.isoformat()]
        log("Fetching news...")
        news = fetch_news()
        if len(news) < 10:
            raise RuntimeError("Found too few news stories; the news sites may be unreachable.")
        log("Writing scripts with Claude...")
        segments = write_scripts(news, date)
        now = dt.datetime.now(dt.timezone.utc)
        for n, seg in enumerate(segments, 1):
            fname = f"{date.isoformat()}-part{n}.mp3"
            log(f"Creating audio for part {n} ({len(seg['script'].split())} words)...")
            seconds = synthesize(seg["script"], SITE / "audio" / fname)
            episodes.append({
                "guid": f"tech-{date.isoformat()}-{n}",
                "date": date.isoformat(),
                "part": n,
                "title": f"{date.isoformat()} - Part {n}: {seg['title']}",
                "summary": seg["summary"],
                "file": fname,
                "bytes": (SITE / "audio" / fname).stat().st_size,
                "seconds": seconds,
                "pub": (now - dt.timedelta(minutes=n)).isoformat(),
            })
            log(f"  part {n}: {seconds / 60:.1f} minutes")

    (SITE / "episodes.json").write_text(json.dumps(episodes, indent=1))
    (SITE / "feed.xml").write_text(build_feed(episodes), encoding="utf-8")
    log(f"Website ready in '{SITE}/' with {len(episodes)} episodes.")


# ----------------------------------------------------------------------------
# 5. SPOTIFY
# ----------------------------------------------------------------------------
API = "https://api.spotify.com/v1"


class Spotify:
    def __init__(self):
        cid, refresh = env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_REFRESH_TOKEN")
        if not (cid and refresh):
            raise RuntimeError("SPOTIFY_CLIENT_ID / SPOTIFY_REFRESH_TOKEN are missing.")
        r = http("POST", "https://accounts.spotify.com/api/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid})
        if r.status_code != 200:
            raise RuntimeError(f"Spotify login failed ({r.status_code}): {r.text[:300]}. "
                               "Re-run the login helper page and update the SPOTIFY_REFRESH_TOKEN secret.")
        self.h = {"Authorization": "Bearer " + r.json()["access_token"]}

    def get(self, path, **params):
        r = http("GET", API + path, headers=self.h, params=params)
        if r.status_code != 200:
            raise RuntimeError(f"Spotify GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def send(self, method, path, body):
        r = http(method, API + path, headers=self.h, json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Spotify {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ---- podcast side ----
    def find_show_id(self):
        sid = env("SPOTIFY_SHOW_ID")
        if sid:
            return sid
        res = self.get("/search", q=PODCAST_TITLE, type="show", limit=10)
        for s in res.get("shows", {}).get("items", []) or []:
            if s and s.get("name", "").strip().lower() == PODCAST_TITLE.lower():
                return s["id"]
        return None

    def find_episodes(self, show_id, date):
        found = {}
        for offset in (0, 10, 20, 30):
            page = self.get(f"/shows/{show_id}/episodes", limit=10, offset=offset)
            for ep in page.get("items", []) or []:
                if not ep:
                    continue
                m = re.match(rf"{date.isoformat()} - Part (\d+)", ep.get("name", ""))
                if m and int(m.group(1)) not in found:
                    found[int(m.group(1))] = ep
            if not page.get("next"):
                break
        return found

    # ---- music side ----
    def music_pool(self):
        pool = {}

        def add(track):
            if track and track.get("type", "track") == "track" and track.get("id") \
                    and track.get("duration_ms") and not track.get("is_local"):
                pool.setdefault(track["id"], track)

        for rng in ("short_term", "medium_term", "long_term"):
            try:
                for t in self.get("/me/top/tracks", time_range=rng, limit=50).get("items", []):
                    add(t)
            except Exception as e:
                log(f"  top tracks ({rng}) unavailable: {e}")
        log(f"  {len(pool)} tracks from your top tracks")

        try:  # more variety: other songs by your favourite artists
            artists = self.get("/me/top/artists", time_range="medium_term", limit=20).get("items", [])
            random.shuffle(artists)
            for a in artists[:8]:
                res = self.get("/search", q=f'artist:"{a["name"]}"', type="track", limit=10)
                for t in res.get("tracks", {}).get("items", []) or []:
                    if t and any(x["id"] == a["id"] for x in t.get("artists", [])):
                        add(t)
        except Exception as e:
            log(f"  favourite-artist search skipped: {e}")

        if len(pool) < 40:
            try:
                for off in (0, 50):
                    for it in self.get("/me/tracks", limit=50, offset=off).get("items", []):
                        add(it.get("track"))
            except Exception as e:
                log(f"  saved tracks unavailable: {e}")
        if len(pool) < 20:
            try:
                for it in self.get("/me/player/recently-played", limit=50).get("items", []):
                    add(it.get("track"))
            except Exception as e:
                log(f"  recently played unavailable: {e}")
        log(f"  music pool: {len(pool)} tracks")
        if not pool:
            raise RuntimeError("Could not find any music in your Spotify profile.")
        return list(pool.values())

    # ---- playlist ----
    def playlist_id(self, description):
        offset = 0
        while True:
            page = self.get("/me/playlists", limit=50, offset=offset)
            for p in page.get("items", []) or []:
                if p and p.get("name") == PLAYLIST_NAME:
                    return p["id"]
            if not page.get("next"):
                break
            offset += 50
        created = self.send("POST", "/me/playlists",
                            {"name": PLAYLIST_NAME, "public": False, "description": description})
        return created["id"]

    def set_playlist(self, pid, uris, description):
        self.send("PUT", f"/playlists/{pid}/items", {"uris": uris[:100]})
        for i in range(100, len(uris), 100):
            self.send("POST", f"/playlists/{pid}/items", {"uris": uris[i:i + 100]})
        try:
            self.send("PUT", f"/playlists/{pid}", {"description": description})
        except Exception as e:
            log(f"  (could not update description: {e})")


def pick_block(pool, target_s, used, tolerance=60):
    """Randomly choose tracks that add up to about target_s seconds."""
    cands = [t for t in pool if t["id"] not in used]
    random.shuffle(cands)
    block, total = [], 0.0
    for t in cands:
        d = t["duration_ms"] / 1000
        if total + d <= target_s + tolerance:
            block.append(t)
            used.add(t["id"])
            total += d
        if total >= target_s - tolerance:
            break
    return block, total


def plan_playlist(episodes, pool, total_s):
    """episodes: list of dicts with 'uri' and 'duration_ms' in order. Returns list of URIs."""
    pod_s = sum(e["duration_ms"] for e in episodes) / 1000
    music_s = max(total_s - pod_s, 0)
    blocks = max(len(episodes), 1)
    uris, used, remaining = [], set(), music_s
    for i in range(blocks):
        if i < len(episodes):
            uris.append(episodes[i]["uri"])
        block, got = pick_block(pool, remaining / (blocks - i), used)
        if not block:  # ran out of unused songs: allow repeats
            used.clear()
            block, got = pick_block(pool, remaining / (blocks - i), used)
        uris.extend(t["uri"] for t in block)
        remaining -= got
    return uris


def build_playlist():
    random.seed(target_date().toordinal())
    sp = Spotify()
    date = target_date()
    log(f"Building playlist for {date}")
    show_id = sp.find_show_id()
    episodes = {}
    deadline = time.time() + BUILD_WAIT_MINUTES * 60
    if not show_id:
        log("Spotify does not know your podcast yet (see setup step on Spotify for Creators).")
    while show_id:
        episodes = sp.find_episodes(show_id, date)
        log(f"  found {len(episodes)} of {NUM_SEGMENTS} episodes on Spotify")
        if len(episodes) >= NUM_SEGMENTS or time.time() > deadline:
            break
        log("  waiting 5 minutes for Spotify to pick up the episodes...")
        time.sleep(300)

    eps = [{"uri": e["uri"], "duration_ms": e["duration_ms"]} for _, e in sorted(episodes.items())]
    note = "" if len(eps) >= NUM_SEGMENTS else " (podcast episodes were not ready; music only for the missing parts)"
    log("Collecting music from your Spotify profile...")
    pool = sp.music_pool()
    uris = plan_playlist(eps, pool, TOTAL_MINUTES * 60)
    by_uri = {t["uri"]: t["duration_ms"] for t in pool}
    by_uri.update({e["uri"]: e["duration_ms"] for e in eps})
    total = sum(by_uri.get(u, 0) for u in uris) / 60000
    desc = f"Auto-made {date}: {len(eps)} tech news segments + music, about {total:.0f} min{note}"
    pid = sp.playlist_id(desc)
    sp.set_playlist(pid, uris, desc)
    log(f"Done! '{PLAYLIST_NAME}' now has {len(uris)} items, about {total:.1f} minutes.{note}")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "setup":
        build_site(generate=False)
    elif mode == "publish":
        build_site(generate=True)
    elif mode == "playlist":
        build_playlist()
    else:
        print(__doc__)
        sys.exit(1)
