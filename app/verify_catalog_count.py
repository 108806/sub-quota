from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_DISCOVER_MAX_PAGE = 500


@dataclass
class VerifyConfig:
    api_key: str
    start_year: int
    end_year: int
    catalog_path: Path
    timeout_seconds: int
    retry_count: int
    retry_backoff_seconds: float


def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_dotenv() -> None:
    seen: set[Path] = set()
    roots: list[Path] = []

    cwd = Path.cwd().resolve()
    roots.append(cwd)
    roots.extend(cwd.parents)

    script_dir = Path(__file__).resolve().parent
    roots.append(script_dir)
    roots.extend(script_dir.parents)

    for root in roots:
        env_path = root / ".env"
        if env_path in seen:
            continue
        seen.add(env_path)
        _load_dotenv_file(env_path)


def get_json(url: str, *, timeout_seconds: int, retry_count: int, retry_backoff_seconds: float) -> dict:
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
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except URLError as exc:
            last_error = exc

        if attempt < retry_count:
            time.sleep(retry_backoff_seconds * (2**attempt))

    if last_error is None:
        raise RuntimeError("TMDB request failed for unknown reason.")
    raise RuntimeError(f"TMDB request failed: {last_error}") from last_error


def fetch_discover_page(
    *,
    year: int,
    page: int,
    api_key: str,
    timeout_seconds: int,
    retry_count: int,
    retry_backoff_seconds: float,
    date_gte: str | None = None,
    date_lte: str | None = None,
) -> dict:
    query = {
        "api_key": api_key,
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
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
        retry_backoff_seconds=retry_backoff_seconds,
    )


def tmdb_count_for_year(year: int, config: VerifyConfig) -> tuple[int, int]:
    """Return (total_results_for_year, window_count)."""
    queue: list[tuple[date, date]] = [(date(year, 1, 1), date(year, 12, 31))]
    total_results = 0
    window_count = 0

    while queue:
        start_date, end_date = queue.pop(0)
        data = fetch_discover_page(
            year=year,
            page=1,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            retry_count=config.retry_count,
            retry_backoff_seconds=config.retry_backoff_seconds,
            date_gte=start_date.isoformat(),
            date_lte=end_date.isoformat(),
        )

        total_pages = int(data.get("total_pages", 1))
        window_results = int(data.get("total_results", 0))

        if total_pages > TMDB_DISCOVER_MAX_PAGE and start_date < end_date:
            mid_date = start_date + timedelta(days=(end_date - start_date).days // 2)
            queue.insert(0, (mid_date + timedelta(days=1), end_date))
            queue.insert(0, (start_date, mid_date))
            continue

        if total_pages > TMDB_DISCOVER_MAX_PAGE and start_date == end_date:
            raise RuntimeError(
                "Cannot guarantee full yearly count because one day exceeds 500 pages: "
                f"{start_date.isoformat()}"
            )

        total_results += window_results
        window_count += 1

    return total_results, window_count


def local_count_for_range(catalog_path: Path, start_year: int, end_year: int) -> tuple[int, dict[int, int]]:
    if not catalog_path.exists() or not catalog_path.is_file():
        raise FileNotFoundError(f"Catalog file not found: {catalog_path}")

    total = 0
    by_year: dict[int, int] = {}

    with catalog_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("media_type") != "movie":
                continue

            year = record.get("year")
            if not isinstance(year, int):
                continue
            if year < start_year or year > end_year:
                continue

            total += 1
            by_year[year] = by_year.get(year, 0) + 1

    return total, by_year


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare TMDB movie count vs local catalog count for a year range."
    )
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    parser.add_argument(
        "--catalog",
        default="data/catalog/movies_2000_now.jsonl",
        help="Catalog JSONL path (default: data/catalog/movies_2000_now.jsonl)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    parser.add_argument("--retry-count", type=int, default=int(os.getenv("RETRY_COUNT", "3")))
    parser.add_argument("--retry-backoff-seconds", type=float, default=2.0)
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> VerifyConfig:
    api_key = os.getenv("TMDB_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TMDB_API_KEY is required.")
    if args.start_year > args.end_year:
        raise ValueError("start-year must be <= end-year.")

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    requested_catalog = Path(args.catalog)
    catalog_path = requested_catalog if requested_catalog.is_absolute() else (project_root / requested_catalog)

    return VerifyConfig(
        api_key=api_key,
        start_year=args.start_year,
        end_year=args.end_year,
        catalog_path=catalog_path,
        timeout_seconds=max(1, args.timeout_seconds),
        retry_count=max(0, args.retry_count),
        retry_backoff_seconds=max(0.1, args.retry_backoff_seconds),
    )


def run(config: VerifyConfig) -> int:
    print(f"Catalog path: {config.catalog_path}")
    print(f"Range      : {config.start_year}..{config.end_year}")

    local_total, local_by_year = local_count_for_range(config.catalog_path, config.start_year, config.end_year)

    tmdb_total = 0
    print("Per-year comparison:")
    for year in range(config.start_year, config.end_year + 1):
        expected, window_count = tmdb_count_for_year(year, config)
        actual = local_by_year.get(year, 0)
        delta = actual - expected
        tmdb_total += expected
        status = "OK" if delta == 0 else "MISMATCH"
        print(
            f"{year}: tmdb={expected} local={actual} delta={delta} "
            f"windows={window_count} status={status}"
        )

    total_delta = local_total - tmdb_total
    print("-" * 80)
    print(f"TOTAL tmdb={tmdb_total} local={local_total} delta={total_delta}")

    if total_delta == 0:
        print("Result: MATCH")
        return 0

    print("Result: MISMATCH")
    return 2


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        config = build_config(args)
        return run(config)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
