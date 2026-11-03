#!/usr/bin/env python3
"""Export Last.fm scrobbles to a monthly CSV that import_playlists.py can read.

Output rows: <Mon-YYYY>,<artist>,<track>  in chronological order.
Reads LASTFM_API_KEY and LASTFM_USER from .env (or environment).

Usage:
    python3 lastfm_export.py                              # full history -> data/lastfm_playlists.csv
    python3 lastfm_export.py --from 2017-01 --to 2026-10  # date range (inclusive months)
    python3 import_playlists.py data/lastfm_playlists.csv --dry-run
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from import_playlists import DATA_DIR, SSL_CTX, load_env

API = "https://ws.audioscrobbler.com/2.0/"


def month_start(s):
    return datetime.strptime(s, "%Y-%m").replace(tzinfo=timezone.utc)


def next_month(dt):
    return dt.replace(year=dt.year + dt.month // 12, month=dt.month % 12 + 1)


def get_page(params, retries=6):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30, context=SSL_CTX) as resp:
                data = json.load(resp)
            if "error" in data:
                # 29 = rate limit, 8/16 = temporary backend errors
                if data["error"] in (8, 16, 29):
                    time.sleep(2 ** attempt)
                    continue
                sys.exit(f"Last.fm error {data['error']}: {data.get('message')}")
            return data["recenttracks"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(2 ** attempt)
    sys.exit(f"Giving up after {retries} attempts: {url}")


def fetch_scrobbles(api_key, user, start=None, end=None):
    params = {"method": "user.getrecenttracks", "user": user, "api_key": api_key,
              "format": "json", "limit": 200}
    if start:
        params["from"] = int(start.timestamp())
    if end:
        params["to"] = int(end.timestamp()) - 1
    page, pages, scrobbles = 1, 1, []
    while page <= pages:
        rt = get_page({**params, "page": page})
        pages = int(rt["@attr"]["totalPages"])
        tracks = rt.get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        for t in tracks:
            if t.get("@attr", {}).get("nowplaying") or "date" not in t:
                continue
            scrobbles.append((int(t["date"]["uts"]), t["artist"]["#text"], t["name"]))
        print(f"  page {page}/{pages} ({len(scrobbles)} scrobbles)", flush=True)
        page += 1
        time.sleep(0.25)  # stay well under Last.fm's ~5 req/s limit
    scrobbles.sort()
    return scrobbles


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="first month, YYYY-MM")
    ap.add_argument("--to", dest="end", help="last month (inclusive), YYYY-MM")
    ap.add_argument("--user", help="Last.fm username (default LASTFM_USER)")
    ap.add_argument("-o", "--output", default=os.path.join(DATA_DIR, "lastfm_playlists.csv"))
    args = ap.parse_args()

    load_env()
    api_key = os.environ.get("LASTFM_API_KEY")
    user = args.user or os.environ.get("LASTFM_USER")
    if not api_key or not user:
        sys.exit("Missing LASTFM_API_KEY / LASTFM_USER (set them in .env)")

    start = month_start(args.start) if args.start else None
    end = next_month(month_start(args.end)) if args.end else None
    print(f"Fetching scrobbles for {user}...")
    scrobbles = fetch_scrobbles(api_key, user, start, end)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["MonthYear", "Artist", "Track"])
        for uts, artist, track in scrobbles:
            month = datetime.fromtimestamp(uts, timezone.utc).strftime("%b-%Y")
            w.writerow([month, artist, track])
    months = len({datetime.fromtimestamp(u, timezone.utc).strftime("%Y-%m") for u, _, _ in scrobbles})
    print(f"Wrote {len(scrobbles)} scrobbles across {months} months to {args.output}")


if __name__ == "__main__":
    main()
