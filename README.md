# Movie + TV Subtitle Search (2000 -> now)

Local Python application that:

- pulls movie and TV title metadata from TMDB
- downloads English subtitles when available
- stores subtitles in a local `subs/` directory
- searches quotes using exact text match via ripgrep
- uses the same command for first ingest and incremental updates

## Status

Planning and scaffold phase.

This README defines the v1 design and operating model before code is added.

## Goals

- Maximize catalog coverage from year 2000 to current year
- Include both movies and TV shows
- Download any English subtitle that exists
- Keep local-only storage and local deployment
- Use exact quote matching only in v1
- Reuse one update flow for initial run and subsequent syncs

## Non-goals (v1)

- No PostgreSQL or external database
- No fuzzy search/ranking
- No cloud deployment
- No distributed worker cluster

## Tech Stack

- Python backend
- TMDB API for title catalog
- Subtitle provider adapter(s), starting with OpenSubtitles API
- ripgrep (`rg`) for exact text search
- Local JSONL state files for tracking progress

## High-Level Architecture

1. Catalog ingest
   - Pull movie titles from TMDB (2000 -> current year)
   - Pull TV titles from TMDB (2000 -> current year)
   - Normalize and persist title metadata

2. Subtitle discovery + download
   - Check subtitle provider(s) for English subtitles by title
   - Download available subtitles
   - Parse to normalized text records
   - Keep local raw and normalized assets under `subs/`

3. State tracking
   - Record which titles already have downloaded English subtitles
   - Record which titles were checked and had no subtitles
   - Schedule rechecks for no-subtitle titles

4. Search
   - Run ripgrep exact search across normalized subtitle text files
   - Return all matches with title and timestamp context

## Directory Layout (planned)

```
Movie_sub_search/
  app/
    update_all.py
    ingest_tmdb.py
    ingest_subtitles.py
    search_quotes.py
    providers/
      base.py
      opensubtitles.py
  data/
    catalog/
      titles.jsonl
    state/
      subtitle_status.jsonl
      sync_state.json
  subs/
    movie/
      <tmdb_id>/
    tv/
      <tmdb_id>/<season>/<episode>/
  logs/
  README.md
```

## Storage Model

### data/catalog/titles.jsonl

One record per TMDB title (movie or TV).

Suggested fields:

- media_type (`movie` or `tv`)
- tmdb_id
- title
- original_title
- release_date_or_first_air_date
- year
- original_language
- popularity
- vote_count
- fetched_at

### data/state/subtitle_status.jsonl

One record per title tracking subtitle state.

Suggested fields:

- media_type
- tmdb_id
- has_en_subs
- downloaded_count
- last_checked_at
- next_check_at
- last_provider
- last_result (`downloaded`, `not_found`, `error`, `skipped_already_have_subs`)

### subs/

Contains downloaded subtitle files and normalized text variants used for search.

Suggested per-subtitle metadata file:

- provider
- provider_sub_id
- language
- source_hash
- release_name
- media context (movie or show/season/episode)

## First Run vs Incremental Updates

The same command handles both initial ingest and later updates.

### First run behavior

- discover titles from 2000 -> now
- check subtitle availability for each title
- download English subtitles when found
- mark title state as has subtitles or not found

### Incremental behavior

- fetch new TMDB titles since last sync
- for titles already marked `has_en_subs = true`, skip re-download checks
- for titles with `has_en_subs = false`, recheck only when `next_check_at` is due
- if subtitles become available later, download and flip state to true

### Suggested recheck cadence for no-subtitle titles

- 0 to 30 days from release: recheck every 7 days
- 31 to 180 days from release: recheck every 30 days
- older than 180 days: recheck every 90 days

## Search Behavior (v1)

- exact text only
- case-insensitive by default
- no fuzzy matching
- search runs over normalized subtitle text files in `subs/`

Expected match output fields:

- media_type
- tmdb_id
- title
- year
- season/episode (for TV)
- subtitle file id/path
- subtitle timestamp
- matched line text

## API/CLI Plan (v1)

CLI first, optional local API wrapper.

Planned commands:

- `python -m app.ingest_tmdb` to fetch movies from 2000 -> now
- `python -m app.ingest_subtitles` to fetch English subtitles with provider fallback
- `python -m app.update_all` for first run and updates
- `python -m app.search_quotes --query "..."` for exact search

### Subtitle ingest command

Provider order defaults to `opensubtitles,subdl` and can be overridden.

- `python -m app.ingest_subtitles`
- `python -m app.ingest_subtitles --providers opensubtitles,subdl`
- `python -m app.ingest_subtitles --start-year 2024 --end-year 2026 --max-titles 500`
- `python -m app.ingest_subtitles --recheck-having-subs`

Behavior:

- append-only subtitle ingest (no deletion)
- skip titles already marked with subtitles by default
- writes status to `data/state/subtitle_status.jsonl`
- saves files under `subs/movie/<tmdb_id>/`

### Current implemented command

Movie list ingest:

- `python -m app.ingest_tmdb`
- `python -m app.ingest_tmdb --start-year 2000 --end-year 2026`
- `python -m app.ingest_tmdb --output data/catalog/movies_2000_now.jsonl`

Output fields include movie name/title and year, plus TMDB metadata useful for later subtitle matching.

Optional local API endpoints later:

- `POST /update`
- `GET /search?q=...`
- `GET /title/{tmdb_id}`

## Configuration

Use environment variables in a local `.env` file (not committed).

Required:

- `TMDB_API_KEY`

Likely required depending on provider adapter:

- `OPENSUBTITLES_API_KEY`
- `OPENSUBTITLES_USERNAME`
- `OPENSUBTITLES_PASSWORD`
- `SUBDL_API_KEY`

Optional:

- `START_YEAR` (default `2000`)
- `MAX_PAGES_PER_YEAR` (safety cap)
- `REQUEST_TIMEOUT_SECONDS`
- `RETRY_COUNT`
- `LOG_LEVEL`

## Idempotency Rules

- Never redownload subtitles already present locally
- Detect duplicates by provider ID and content hash
- Keep updates safe to rerun if interrupted
- Persist sync checkpoints frequently

## Operational Notes

- ripgrep must be installed and available in PATH
- disk usage will grow significantly with catalog size
- API rate limits must be respected with backoff/retry
- provider terms and licenses must be followed

## Roadmap

1. Scaffold Python project structure
2. Implement TMDB catalog ingest (movie + TV)
3. Implement provider adapter and subtitle downloader
4. Implement JSONL state tracker and idempotent update loop
5. Implement exact ripgrep search command
6. Add logs, retry strategy, and basic tests

## License and Data Compliance

Before broad crawling/downloading:

- verify subtitle provider API terms permit your usage
- keep attribution where required
- implement removal/update workflow if content policy requires it
