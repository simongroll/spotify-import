#!/usr/bin/env python3
"""Import monthly playlists from playlists.csv into Spotify.

CSV format: <month>,<artist>,<track>  (header row optional)
Month may be "2016 - Sep" or "Sep-2016"; playlists are named "Sep-2016".
Playlists over 250 tracks are split into "Sep-2016 #1", "Sep-2016 #2", ...

No third-party dependencies. Credentials are read from .env (or environment):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI

Usage:
    python3 import_playlists.py --dry-run              # search only, write matches/unmatched CSVs
    python3 import_playlists.py                        # search + create playlists (data/playlists.csv)
    python3 import_playlists.py data/lastfm_2009.csv   # import another CSV
"""

import argparse
import base64
import csv
import difflib
import http.server
import json
import os
import re
import secrets
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")   # personal files (CSVs, tokens, cache); git-ignored
TOKEN_FILE = os.path.join(DATA_DIR, ".spotify_token.json")
CACHE_FILE = os.path.join(DATA_DIR, "search_cache.json")
STATE_FILE = os.path.join(DATA_DIR, "import_state.json")
API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private playlist-modify-public"
MATCH_THRESHOLD = 0.75
MIN_REQUEST_INTERVAL = 0.5   # seconds between API calls; Spotify bans dev-mode apps for bursts
MAX_RETRY_WAIT = 300         # longer Retry-After => stop and resume later instead of sleeping
PAGE_SIZE = 250              # max tracks per playlist; longer ones split into "Name #1", "Name #2", ...


def _ssl_context():
    # python.org builds on macOS ship without CA certs; fall back to certifi or the system bundle
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if not ssl.get_default_verify_paths().cafile and os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


SSL_CTX = _ssl_context()


# ---------------------------------------------------------------- config

def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- auth

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in qs.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Spotify authorised. You can close this tab.</h2>")

    def log_message(self, *args):
        pass


def _token_request(data):
    cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(data).encode(),
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, context=SSL_CTX) as resp:
        tok = json.load(resp)
    tok["expires_at"] = time.time() + tok["expires_in"] - 60
    return tok


def authorize():
    redirect = os.environ["SPOTIFY_REDIRECT_URI"]
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": os.environ["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
    })
    parsed = urllib.parse.urlparse(redirect)
    server = http.server.HTTPServer((parsed.hostname, parsed.port), _CallbackHandler)
    print("Opening browser for Spotify login. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in _CallbackHandler.result and "error" not in _CallbackHandler.result:
        server.handle_request()
    server.server_close()
    res = _CallbackHandler.result
    if "error" in res:
        sys.exit(f"Authorisation failed: {res['error']}")
    if res.get("state") != state:
        sys.exit("Authorisation failed: state mismatch")
    tok = _token_request({"grant_type": "authorization_code",
                          "code": res["code"], "redirect_uri": redirect})
    save_json(TOKEN_FILE, tok)
    return tok


class Spotify:
    def __init__(self):
        self.tok = load_json(TOKEN_FILE, None) or authorize()
        self.last_request = 0.0

    def _access_token(self):
        if time.time() >= self.tok["expires_at"]:
            new = _token_request({"grant_type": "refresh_token",
                                  "refresh_token": self.tok["refresh_token"]})
            new.setdefault("refresh_token", self.tok["refresh_token"])
            self.tok = new
            save_json(TOKEN_FILE, self.tok)
        return self.tok["access_token"]

    def request(self, method, path, params=None, body=None, retries=6):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries):
            time.sleep(max(0.0, self.last_request + MIN_REQUEST_INTERVAL - time.time()))
            self.last_request = time.time()
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "5")) + 1
                    if wait > MAX_RETRY_WAIT:
                        raise RateLimited(wait)
                    print(f"  rate limited, sleeping {wait}s", flush=True)
                    time.sleep(wait)
                elif e.code == 401 and attempt == 0:
                    self.tok["expires_at"] = 0  # force refresh
                elif e.code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    raise SpotifyError(e.code, e.read().decode(errors="replace"), url)
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt)
        raise SpotifyError(0, "too many retries", url)


class SpotifyError(Exception):
    def __init__(self, code, body, url):
        super().__init__(f"HTTP {code} for {url}: {body}")
        self.code = code


class RateLimited(Exception):
    """Spotify asked us to back off for longer than MAX_RETRY_WAIT."""
    def __init__(self, wait):
        super().__init__(f"rate limited for {wait}s")
        self.resume_at = datetime.now() + timedelta(seconds=wait)


# ---------------------------------------------------------------- matching

FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(ft|feat|featuring)\b\.?.*$", re.I)
PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
SUFFIX_RE = re.compile(r"\s+-\s+.*$")


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(t):
    """Strip 'ft. X', '(Remix)', ' - Remastered' etc. for a broader search."""
    t = FEAT_RE.sub("", t)
    t = PAREN_RE.sub("", t)
    t = SUFFIX_RE.sub("", t)
    return t.strip() or t


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(item, artist, track):
    artist_score = max((sim(a["name"], artist) for a in item["artists"]), default=0)
    # also accept "Artist A & Artist B" style CSV artist strings
    if artist_score < 0.8 and any(norm(a["name"]) in norm(artist) for a in item["artists"]):
        artist_score = 0.85
    title_score = max(sim(item["name"], track), sim(clean_title(item["name"]), clean_title(track)))
    return 0.45 * artist_score + 0.55 * title_score


def find_track(sp, artist, track):
    ct = clean_title(track)
    queries = [
        f'track:"{track}" artist:"{artist}"',
        f'track:"{ct}" artist:"{artist}"',
        f"{artist} {ct}",
    ]
    best, best_score = None, 0.0
    for q in OrderedDict.fromkeys(queries):
        try:
            res = sp.request("GET", "/search", {"q": q, "type": "track", "limit": 10})
        except SpotifyError as e:
            print(f"  search error: {e}")
            continue
        for item in res.get("tracks", {}).get("items", []):
            if not item:
                continue
            s = score(item, artist, track)
            # light tie-break toward popular (usually the canonical release)
            s += item.get("popularity", 0) / 10000
            if s > best_score:
                best, best_score = item, s
        if best_score >= 0.95:
            break
    if best and best_score >= MATCH_THRESHOLD:
        return {"uri": best["uri"], "name": best["name"],
                "artist": ", ".join(a["name"] for a in best["artists"]),
                "score": round(best_score, 3)}
    return None


# ---------------------------------------------------------------- csv

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def playlist_name(raw):
    raw = raw.strip()
    m = re.match(r"^(\d{4})\s*-\s*([A-Za-z]{3})", raw)        # 2016 - Sep
    if m:
        return f"{m.group(2).title()}-{m.group(1)}"
    m = re.match(r"^([A-Za-z]{3})[\s\-/]*(\d{4})$", raw)       # Sep-2016
    if m:
        return f"{m.group(1).title()}-{m.group(2)}"
    return raw


def sort_key(name):
    try:
        return datetime.strptime(name, "%b-%Y")
    except ValueError:
        return datetime.max


def read_playlists(path):
    playlists = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3 or not row[0].strip():
                continue
            if row[0].strip().lower() in ("monthyear", "month", "playlist"):
                continue
            name = playlist_name(row[0])
            artist, track = row[1].strip(), row[2].strip()
            if artist and track:
                playlists.setdefault(name, OrderedDict())[(artist, track)] = None
    return OrderedDict((k, list(playlists[k])) for k in sorted(playlists, key=sort_key))


# ---------------------------------------------------------------- playlists

def create_playlist(sp, user_id, name, public, description):
    body = {"name": name, "public": public, "description": description}
    try:
        return sp.request("POST", "/me/playlists", body=body)["id"]
    except SpotifyError as e:
        if e.code not in (403, 404, 405):
            raise
        return sp.request("POST", f"/users/{user_id}/playlists", body=body)["id"]


def set_playlist_items(sp, playlist_id, uris):
    """Replace playlist contents (idempotent, safe to re-run)."""
    for sub in ("items", "tracks"):
        try:
            sp.request("PUT", f"/playlists/{playlist_id}/{sub}", body={"uris": uris[:100]})
            for i in range(100, len(uris), 100):
                sp.request("POST", f"/playlists/{playlist_id}/{sub}", body={"uris": uris[i:i + 100]})
            return
        except SpotifyError as e:
            if sub == "tracks" or e.code not in (403, 404, 405):
                raise


# ---------------------------------------------------------------- main

def main():
    try:
        run()
    except RateLimited as e:
        sys.exit(f"\nSpotify rate limit: blocked until about {e.resume_at:%a %d %b %H:%M}. "
                 "Progress is saved; re-run the same command after that to continue.")


def run():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=os.path.join(DATA_DIR, "playlists.csv"))
    ap.add_argument("--dry-run", action="store_true", help="search only; don't create playlists")
    ap.add_argument("--public", action="store_true", help="make playlists public (default private)")
    ap.add_argument("--only", action="append", help="only process this playlist (e.g. Sep-2016); repeatable")
    ap.add_argument("--prefix", default="", help="prefix for playlist names, e.g. 'Last.fm '")
    ap.add_argument("--recreate", action="store_true", help="ignore saved state and create new playlists")
    args = ap.parse_args()

    load_env()
    for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"):
        if not os.environ.get(k):
            sys.exit(f"Missing {k} (set it in .env)")

    playlists = read_playlists(args.csv)
    if args.only:
        playlists = OrderedDict((k, v) for k, v in playlists.items() if k in args.only)
    total = sum(len(v) for v in playlists.values())
    print(f"{len(playlists)} playlists, {total} tracks")

    sp = Spotify()
    me = sp.request("GET", "/me")
    print(f"Logged in as {me.get('display_name') or me['id']}")

    cache = load_json(CACHE_FILE, {})
    state = {} if args.recreate else load_json(STATE_FILE, {})
    match_rows, unmatched_rows = [], []

    for name, tracks in playlists.items():
        full_name = args.prefix + name
        print(f"\n== {full_name} ({len(tracks)} tracks)")
        uris = []
        for i, (artist, track) in enumerate(tracks, 1):
            key = f"{artist}\t{track}"
            if key not in cache:
                cache[key] = find_track(sp, artist, track)
                save_json(CACHE_FILE, cache)
            m = cache[key]
            if m:
                if m["uri"] not in uris:
                    uris.append(m["uri"])
                match_rows.append([full_name, artist, track, m["artist"], m["name"], m["score"], m["uri"]])
            else:
                unmatched_rows.append([full_name, artist, track])
            if i % 50 == 0:
                print(f"  searched {i}/{len(tracks)}", flush=True)
        save_json(CACHE_FILE, cache)
        print(f"  matched {len(uris)}/{len(tracks)}")

        if args.dry_run or not uris:
            continue
        pages = [uris[i:i + PAGE_SIZE] for i in range(0, len(uris), PAGE_SIZE)]
        for n, page in enumerate(pages, 1):
            page_name = full_name if len(pages) == 1 else f"{full_name} #{n}"
            if len(pages) > 1:
                print(f"  -- {page_name} ({len(page)} tracks)")
            entry = state.get(page_name)
            if entry and entry.get("done"):
                print("  already imported, skipping (use --recreate to redo)")
                continue
            if not entry:
                desc = f"Imported from Last.fm scrobbles for {name}"
                if len(pages) > 1:
                    desc += f" (part {n} of {len(pages)})"
                pid = create_playlist(sp, me["id"], page_name, args.public, desc)
                entry = state[page_name] = {"id": pid, "done": False}
                save_json(STATE_FILE, state)
            set_playlist_items(sp, entry["id"], page)
            entry["done"] = True
            entry["tracks"] = len(page)
            save_json(STATE_FILE, state)
            print(f"  playlist ready: https://open.spotify.com/playlist/{entry['id']}")

    with open(os.path.join(DATA_DIR, "matches.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["playlist", "artist", "track", "matched_artist", "matched_track", "score", "uri"])
        w.writerows(match_rows)
    with open(os.path.join(DATA_DIR, "unmatched.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["playlist", "artist", "track"])
        w.writerows(unmatched_rows)
    print(f"\nDone. {len(match_rows)} matched, {len(unmatched_rows)} unmatched "
          f"(see data/matches.csv / data/unmatched.csv)")


if __name__ == "__main__":
    main()
