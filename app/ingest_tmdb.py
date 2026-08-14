from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_DISCOVER_MAX_PAGE = 500


def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]

        # Keep explicit shell-exported env values as higher priority.
        os.environ.setdefault(key, value)


def load_dotenv() -> None:
    # Search in current directory first, then parents, then script location parents.
    seen: set[Path] = set()
    candidate_roots: list[Path] = []

    cwd = Path.cwd().resolve()
    candidate_roots.append(cwd)
    candidate_roots.extend(cwd.parents)

    script_dir = Path(__file__).resolve().parent
    candidate_roots.append(script_dir)
    candidate_roots.extend(script_dir.parents)

    for root in candidate_roots:
        env_path = root / ".env"
        if env_path in seen:
            continue
        seen.add(env_path)
        _load_dotenv_file(env_path)


@dataclass
class IngestConfig:
    api_key: str
    start_year: int
    end_year: int
    output_path: Path
    timeout_seconds: int
    retry_count: int
    retry_backoff_seconds: float
    max_pages_per_year: int | None
    full_refresh: bool
    recheck_completed_years: bool
    allow_truncate: bool


@dataclass
class DateWindow:
    start_date: date
    end_date: date
    total_pages: int
    effective_total_pages: int
    first_page_results: list[dict[str, Any]]


def parse_resume_cursor(state: dict[str, Any], year: int) -> dict[str, Any] | None:
    in_progress = state.get("in_progress_pages", {})
    if not isinstance(in_progress, dict):
        return None

    raw = in_progress.get(str(year))
    if not isinstance(raw, dict):
        return None

    required = ("window_start", "window_end", "last_page")
    if not all(key in raw for key in required):
        return None
    if not isinstance(raw.get("last_page"), int):
        return None

    return {
        "window_start": str(raw["window_start"]),
        "window_end": str(raw["window_end"]),
        "last_page": int(raw["last_page"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download TMDB movie list (title + year) from a year range."
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=int(os.getenv("START_YEAR", "2000")),
        help="First release year to fetch (default: 2000 or START_YEAR env).",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=datetime.now(timezone.utc).year,
        help="Last release year to fetch (default: current year).",
    )
    parser.add_argument(
        "--output",
        default="data/catalog/movies_2000_now.jsonl",
        help="Output JSONL path (default: data/catalog/movies_2000_now.jsonl).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        help="HTTP request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--retry-count",
        type=int,
        default=int(os.getenv("RETRY_COUNT", "3")),
        help="Retries for transient request failures (default: 3).",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Base backoff seconds between retries (default: 2.0).",
    )
    parser.add_argument(
        "--max-pages-per-year",
        type=int,
        default=int(os.getenv("MAX_PAGES_PER_YEAR", "0")),
        help="Optional cap for pages fetched per year; 0 means no cap.",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Rebuild output from scratch and ignore saved ingest state.",
    )
    parser.add_argument(
        "--recheck-completed-years",
        action="store_true",
        help="Force fetching years already marked complete in sync state.",
    )
    parser.add_argument(
        "--allow-truncate",
        action="store_true",
        help="Required with --full-refresh; allows rebuilding catalog from scratch.",
    )
    return parser.parse_args()


def prompt_years_interactive(default_start: int, default_end: int) -> tuple[int, int]:
    if not sys.stdin.isatty():
        return default_start, default_end

    prompt = (
        f"Enter year range to fetch (start-end) [default: {default_start}-{default_end}]: "
    )
    try:
        resp = input(prompt).strip()
    except EOFError:
        return default_start, default_end

    if not resp:
        return default_start, default_end

    # Accept formats: '2000-2020' or '2000 2020' or single year '2005'
    sep = "-" if "-" in resp else None
    parts = resp.split(sep) if sep else resp.split()
    try:
        if len(parts) == 1:
            start = int(parts[0])
            end = default_end
        else:
            start = int(parts[0])
            end = int(parts[1])
        if start > end:
            print("Invalid range: start > end; using defaults.")
            return default_start, default_end
        return start, end
    except Exception:
        print("Could not parse input; using default year range.")
        return default_start, default_end


def build_config(args: argparse.Namespace) -> IngestConfig:
    api_key = os.getenv("TMDB_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TMDB_API_KEY is required.")
    if args.start_year > args.end_year:
        raise ValueError("start-year must be <= end-year.")
    if args.full_refresh and not args.allow_truncate:
        raise ValueError(
            "Refusing to truncate catalog. Use --full-refresh --allow-truncate only if you really want to rebuild."
        )

    max_pages = args.max_pages_per_year if args.max_pages_per_year > 0 else None

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    requested_output = Path(args.output)
    resolved_output = requested_output if requested_output.is_absolute() else (project_root / requested_output)

    return IngestConfig(
        api_key=api_key,
        start_year=args.start_year,
        end_year=args.end_year,
        output_path=resolved_output,
        timeout_seconds=max(1, args.timeout_seconds),
        retry_count=max(0, args.retry_count),
        retry_backoff_seconds=max(0.1, args.retry_backoff_seconds),
        max_pages_per_year=max_pages,
        full_refresh=bool(args.full_refresh),
        recheck_completed_years=bool(args.recheck_completed_years),
        allow_truncate=bool(args.allow_truncate),
    )


def get_json(url: str, *, timeout_seconds: int, retry_count: int, retry_backoff_seconds: float) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "movie-sub-search/0.1",
    }

    last_error: Exception | None = None
    for attempt in range(retry_count + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except HTTPError as exc:
            last_error = exc
            # Retry only on rate limit/server-side errors.
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except URLError as exc:
            last_error = exc

        if attempt < retry_count:
            sleep_seconds = retry_backoff_seconds * (2**attempt)
            time.sleep(sleep_seconds)

    if last_error is None:
        raise RuntimeError("Request failed for unknown reason.")
    raise RuntimeError(f"TMDB request failed: {last_error}") from last_error


def progress_bar(current: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    ratio = min(max(current / total, 0.0), 1.0)
    filled = int(ratio * width)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def should_print_page_checkpoint(page: int, total_pages: int) -> bool:
    if page <= 3:
        return True
    if page == total_pages:
        return True
    if total_pages <= 20:
        return True
    # Print roughly every 5% for long ranges.
    step = max(1, total_pages // 20)
    return page % step == 0


def fetch_discover_page(
    *,
    year: int,
    page: int,
    config: IngestConfig,
    date_gte: str | None = None,
    date_lte: str | None = None,
) -> dict[str, Any]:
    query = {
        "api_key": config.api_key,
        "language": "en-US",
        "sort_by": "primary_release_date.asc",
        "include_adult": "false",
        "include_video": "false",
        "page": str(page),
        "primary_release_year": str(year),
    }
    if date_gte is not None:
        query["primary_release_date.gte"] = date_gte
    if date_lte is not None:
        query["primary_release_date.lte"] = date_lte

    url = f"{TMDB_BASE_URL}/discover/movie?{urlencode(query)}"
    return get_json(
        url,
        timeout_seconds=config.timeout_seconds,
        retry_count=config.retry_count,
        retry_backoff_seconds=config.retry_backoff_seconds,
    )


def plan_date_windows_for_year(year: int, config: IngestConfig) -> list[DateWindow]:
    start_of_year = date(year, 1, 1)
    end_of_year = date(year, 12, 31)

    windows: list[DateWindow] = []
    queue: list[tuple[date, date]] = [(start_of_year, end_of_year)]

    while queue:
        start_date, end_date = queue.pop(0)
        data = fetch_discover_page(
            year=year,
            page=1,
            config=config,
            date_gte=start_date.isoformat(),
            date_lte=end_date.isoformat(),
        )

        total_pages = int(data.get("total_pages", 1))
        results = data.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError(
                f"Unexpected TMDB response shape for year={year}, window={start_date}..{end_date}"
            )

        effective_total_pages = min(total_pages, TMDB_DISCOVER_MAX_PAGE)
        if config.max_pages_per_year is not None:
            effective_total_pages = min(effective_total_pages, config.max_pages_per_year)

        # Split windows until each chunk is within TMDB discover page cap.
        if total_pages > TMDB_DISCOVER_MAX_PAGE and start_date < end_date:
            mid_date = start_date + timedelta(days=(end_date - start_date).days // 2)
            left_end = mid_date
            right_start = mid_date + timedelta(days=1)
            queue.insert(0, (right_start, end_date))
            queue.insert(0, (start_date, left_end))
            continue

        if total_pages > TMDB_DISCOVER_MAX_PAGE and start_date == end_date:
            raise RuntimeError(
                "Cannot guarantee full coverage: "
                f"single day {start_date.isoformat()} has {total_pages} pages (>500)."
            )

        windows.append(
            DateWindow(
                start_date=start_date,
                end_date=end_date,
                total_pages=total_pages,
                effective_total_pages=max(1, effective_total_pages),
                first_page_results=results,
            )
        )

    windows.sort(key=lambda w: (w.start_date, w.end_date))
    return windows


def iter_movie_pages_for_year(
    year: int,
    config: IngestConfig,
    resume_cursor: dict[str, Any] | None = None,
) -> Iterator[tuple[int, int, int, int, date, date, list[dict[str, Any]]]]:
    windows = plan_date_windows_for_year(year, config)
    print(f"Year {year}: planned {len(windows)} date windows.")

    resume_start = None
    resume_end = None
    resume_last_page = None
    if resume_cursor is not None:
        resume_start = resume_cursor.get("window_start")
        resume_end = resume_cursor.get("window_end")
        resume_last_page = resume_cursor.get("last_page")
        print(
            f"Year {year}: resume checkpoint detected at "
            f"{resume_start}..{resume_end} page {resume_last_page}."
        )

    for window_index, window in enumerate(windows, start=1):
        for page in range(1, window.effective_total_pages + 1):
            if (
                resume_start is not None
                and resume_end is not None
                and isinstance(resume_last_page, int)
                and window.start_date.isoformat() == resume_start
                and window.end_date.isoformat() == resume_end
                and page <= resume_last_page
            ):
                continue

            if page == 1:
                results = window.first_page_results
            else:
                data = fetch_discover_page(
                    year=year,
                    page=page,
                    config=config,
                    date_gte=window.start_date.isoformat(),
                    date_lte=window.end_date.isoformat(),
                )
                results = data.get("results", [])
                if not isinstance(results, list):
                    raise RuntimeError(
                        f"Unexpected TMDB response shape for year={year}, "
                        f"window={window.start_date}..{window.end_date}, page={page}"
                    )

            pct = (page / window.effective_total_pages) * 100 if window.effective_total_pages > 0 else 100.0
            bar = progress_bar(page, window.effective_total_pages)
            if should_print_page_checkpoint(page, window.effective_total_pages):
                print(
                    f"Year {year} window {window_index}/{len(windows)} "
                    f"{window.start_date}..{window.end_date} "
                    f"pages {page}/{window.effective_total_pages} {bar} {pct:6.2f}% "
                    f"movies_this_page={len(results)}"
                )

            yield (
                window_index,
                len(windows),
                page,
                window.effective_total_pages,
                window.start_date,
                window.end_date,
                results,
            )

        print(
            f"Year {year} window {window_index}/{len(windows)} complete: "
            f"{window.start_date}..{window.end_date}, pages={window.effective_total_pages}"
        )


def normalize_movie(movie: dict[str, Any], fallback_year: int, fetched_at: str) -> dict[str, Any]:
    release_date = str(movie.get("release_date") or "")
    parsed_year: int | None = None
    if len(release_date) >= 4 and release_date[:4].isdigit():
        parsed_year = int(release_date[:4])

    return {
        "media_type": "movie",
        "tmdb_id": movie.get("id"),
        "title": movie.get("title") or "",
        "original_title": movie.get("original_title") or "",
        "release_date": release_date,
        "year": parsed_year if parsed_year is not None else fallback_year,
        "original_language": movie.get("original_language") or "",
        "popularity": movie.get("popularity"),
        "vote_count": movie.get("vote_count"),
        "fetched_at": fetched_at,
    }


def state_path_for_output(output_path: Path) -> Path:
    return output_path.parent.parent / "state" / "tmdb_ingest_state.json"


def load_existing_tmdb_ids(output_path: Path) -> set[int]:
    ids: set[int] = set()
    catalog_dir = output_path.parent
    if not catalog_dir.exists() or not catalog_dir.is_dir():
        return ids

    for path in sorted(catalog_dir.glob("*.jsonl")):
        if not path.exists() or not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as input_file:
                for line in input_file:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    tmdb_id = record.get("tmdb_id")
                    if isinstance(tmdb_id, int):
                        ids.add(tmdb_id)
        except OSError:
            continue

    return ids


def load_sync_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists() or not state_path.is_file():
        return {}

    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_sync_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def persist_sync_state(
    *,
    state_path: Path,
    completed_years: set[int],
    in_progress_pages: dict[str, int],
    output_path: Path,
    catalog_unique_count: int,
    start_year: int,
    end_year: int,
) -> None:
    sync_state = {
        "completed_years": sorted(completed_years),
        "in_progress_pages": in_progress_pages,
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "catalog_path": output_path.as_posix(),
        "catalog_unique_count": catalog_unique_count,
        "start_year": start_year,
        "end_year": end_year,
    }
    save_sync_state(state_path, sync_state)


def run(config: IngestConfig) -> int:
    started = time.time()
    fetched_at = datetime.now(timezone.utc).isoformat()
    total_years = (config.end_year - config.start_year) + 1
    current_year = datetime.now(timezone.utc).year

    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    sync_state_path = state_path_for_output(config.output_path)

    print("=" * 90)
    print(f"CATALOG OUTPUT DIR : {config.output_path.parent}")
    print(f"SYNC STATE FILE    : {sync_state_path}")
    print("MODE               : append-only (existing records are never removed)")
    print("=" * 90)

    if config.full_refresh:
        print("Full refresh enabled: rebuilding output and resetting sync state.")
        seen_tmdb_ids: set[int] = set()
        completed_years: set[int] = set()
        in_progress_pages: dict[str, dict[str, Any]] = {}
    else:
        seen_tmdb_ids = load_existing_tmdb_ids(config.output_path)
        state = load_sync_state(sync_state_path)
        completed_years = {
            year
            for year in state.get("completed_years", [])
            if isinstance(year, int)
        }
        in_progress_pages = {
            key: value
            for key, value in state.get("in_progress_pages", {}).items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    print(f"Existing movies in catalog: {len(seen_tmdb_ids)}")
    print(f"Output file exists now: {config.output_path.exists()}")

    total_raw = 0
    total_new_written = 0
    skipped_years = 0

    for year_index, year in enumerate(range(config.start_year, config.end_year + 1), start=1):
        if (
            not config.recheck_completed_years
            and year in completed_years
            and year < current_year
        ):
            skipped_years += 1
            year_pct = (year_index / total_years) * 100
            print(
                f"Skipping year {year}: already complete "
                f"({year_index}/{total_years}, {year_pct:.2f}%)."
            )
            continue

        print(f"Starting year {year} ({year_index}/{total_years})...")
        year_new_written = 0
        year_fetched = 0
        resume_cursor = in_progress_pages.get(str(year)) if isinstance(in_progress_pages.get(str(year)), dict) else None
        page_batches = iter_movie_pages_for_year(year, config, resume_cursor=resume_cursor)

        # Open per-year catalog file
        year_file = config.output_path.parent / f"movies_{year}.jsonl"
        file_mode = "w" if config.full_refresh else "a"
        with year_file.open(file_mode, encoding="utf-8", newline="\n") as output_file:
            for _window_idx, _window_count, page, _page_total, start_date, end_date, movies in page_batches:
                total_raw += len(movies)
                year_fetched += len(movies)

                for movie in movies:
                    tmdb_id = movie.get("id")
                    if not isinstance(tmdb_id, int):
                        continue
                    if tmdb_id in seen_tmdb_ids:
                        continue

                    seen_tmdb_ids.add(tmdb_id)
                    normalized = normalize_movie(movie, year, fetched_at)
                    output_file.write(json.dumps(normalized, ensure_ascii=False) + "\n")
                    year_new_written += 1

                in_progress_pages[str(year)] = {
                    "window_start": start_date.isoformat(),
                    "window_end": end_date.isoformat(),
                    "last_page": page,
                }
                persist_sync_state(
                    state_path=sync_state_path,
                    completed_years=completed_years,
                    in_progress_pages=in_progress_pages,
                    output_path=config.output_path,
                    catalog_unique_count=len(seen_tmdb_ids),
                    start_year=config.start_year,
                    end_year=config.end_year,
                )

            # Ensure year file durability
            output_file.flush()
            os.fsync(output_file.fileno())

        total_new_written += year_new_written
        completed_years.add(year)
        in_progress_pages = {}
        persist_sync_state(
            state_path=sync_state_path,
            completed_years=completed_years,
            in_progress_pages=in_progress_pages,
            output_path=config.output_path,
            catalog_unique_count=len(seen_tmdb_ids),
            start_year=config.start_year,
            end_year=config.end_year,
        )

        year_pct = (year_index / total_years) * 100
        elapsed = time.time() - started
        try:
            file_size = year_file.stat().st_size
        except OSError as exc:
            raise RuntimeError(
                f"Failed to stat output file after year {year}: {config.output_path}"
            ) from exc
        if file_size <= 0:
            raise RuntimeError(
                f"Output file is empty after year {year}, save verification failed: {year_file}"
            )

        print(
            f"Year {year} complete: fetched={year_fetched} "
            f"new_this_year={year_new_written} total_new_written={total_new_written} "
            f"overall_year_progress={year_index}/{total_years} ({year_pct:.2f}%) "
            f"elapsed={elapsed:.1f}s"
        )
        print(
            f"Saved and verified year {year} to {year_file} "
            f"(bytes={file_size})"
        )

    duration = time.time() - started
    print(
        "Done: "
        f"raw_fetched={total_raw}, new_written={total_new_written}, "
        f"catalog_total={len(seen_tmdb_ids)}, skipped_years={skipped_years}, "
        f"output={config.output_path.as_posix()}, seconds={duration:.1f}"
    )
    return 0


def main() -> int:
    load_dotenv()
    args = parse_args()
    # If running interactively, allow user to confirm or override year range.
    start_year, end_year = prompt_years_interactive(args.start_year, args.end_year)
    args.start_year = start_year
    args.end_year = end_year
    try:
        config = build_config(args)
        return run(config)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Partial progress was saved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
