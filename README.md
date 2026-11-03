# spotify-import

Turn your Last.fm listening history into monthly Spotify playlists, such as `Sep-2016` and `Oct-2016`.

- **No dependencies.** Plain Python 3 standard library.
- **Fuzzy matching.** Handles `feat.` credits, `(Remastered)` and `- Live` suffixes, accents and `&` vs `and`.
- **Resumable.** Search results and created playlists are saved, so an interrupted or rate-limited run picks up where it stopped.
- **Paged playlists.** A month with more than 250 tracks is split into `Sep-2016 #1`, `Sep-2016 #2`, and so on.
- **Re-runnable.** A playlist is filled by replacing its contents, so running the import again never adds duplicates.

## How it works

```
Last.fm scrobbles ──lastfm_export.py──▶ data/*.csv ──import_playlists.py──▶ Spotify playlists
```

1. `lastfm_export.py` downloads your scrobbles and writes one CSV row per play: month, artist, track.
2. `import_playlists.py` groups the rows by month and searches Spotify for each unique track. It keeps the best match above a similarity threshold and creates one private playlist per month.

You can skip step 1 and supply your own CSV.

## Setup

1. **Create a Spotify app** at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
   Add `http://127.0.0.1:8888/callback` as a Redirect URI.
2. **Optional: get a Last.fm API key** at [last.fm/api/account/create](https://www.last.fm/api/account/create).
   You need this only to export your history from Last.fm.
3. **Configure:**

   ```sh
   cp .env.example .env
   # then fill in the values in .env
   ```

The first time the import runs, it opens a browser window so you can log in to Spotify. The token is saved to `data/.spotify_token.json` and refreshed automatically after that.

## Usage

```sh
# 1. Export scrobbles from Last.fm (whole history, or a range of months)
python3 lastfm_export.py
python3 lastfm_export.py --from 2009-01 --to 2009-12 -o data/lastfm_2009.csv

# 2. Preview the matches without creating anything
python3 import_playlists.py data/lastfm_2009.csv --dry-run

# 3. Create the playlists
python3 import_playlists.py data/lastfm_2009.csv
```

### `import_playlists.py` options

| Option | Description |
| --- | --- |
| `csv` | Input file (default `data/playlists.csv`) |
| `--dry-run` | Search only. Writes the match report but creates no playlists |
| `--only Sep-2016` | Process only this month. Can be repeated |
| `--prefix "Last.fm "` | Prefix for playlist names, giving e.g. `Last.fm Sep-2016` |
| `--public` | Create public playlists (default is private) |
| `--recreate` | Ignore saved state and create fresh playlists |

### `lastfm_export.py` options

| Option | Description |
| --- | --- |
| `--from YYYY-MM` / `--to YYYY-MM` | Range of months, both ends included |
| `--user NAME` | Last.fm user (default `LASTFM_USER`) |
| `-o FILE` | Output CSV (default `data/lastfm_playlists.csv`) |

## CSV format

```csv
MonthYear,Artist,Track
2016 - Sep,The Strokes,Last Nite
Oct-2016,Daft Punk,Get Lucky
```

- The month can be written as `2016 - Sep` or `Sep-2016`. Any other value is used as the playlist name as-is.
- The header row is optional, and duplicate rows within a month are ignored.

See [`examples/sample.csv`](examples/sample.csv).

## Files and privacy

Everything personal stays on your machine and is git-ignored:

| Path | Contents |
| --- | --- |
| `.env` | API credentials |
| `data/.spotify_token.json` | Spotify login token |
| `data/*.csv` | Listening history, plus the `matches.csv` and `unmatched.csv` reports |
| `data/search_cache.json` | Saved Spotify search results, so searches are never repeated |
| `data/import_state.json` | Playlists already created, so a re-run resumes instead of duplicating them |

After each run, `data/matches.csv` lists every match with its score. Check it for wrong picks. `data/unmatched.csv` lists the tracks Spotify couldn't find.

## Rate limits

Spotify apps in development mode have strict limits. The script waits at least 0.5 s between requests. If Spotify returns a `Retry-After` longer than 5 minutes, which can be hours or even a day, the script saves its progress, prints when you can continue, and exits. Run the same command again after that time and it carries on.

Expect a large history (thousands of unique tracks) to need several runs.

## License

[MIT](LICENSE)
