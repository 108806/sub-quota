from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

try:
    from .providers import (
        OpenSubtitlesLegacyProvider,
        OpenSubtitlesProvider,
        ProviderSearchResult,
        SubDLProvider,
    )
except ImportError:
    from providers import (
        OpenSubtitlesLegacyProvider,
        OpenSubtitlesProvider,
        ProviderSearchResult,
        SubDLProvider,
    )


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


@dataclass
class SubtitleConfig:
    catalog_path: Path
    status_path: Path
    subs_root: Path
    start_year: int
    end_year: int
    providers: list[str]
    timeout_seconds: int
    max_titles: int | None
    max_subs_per_title: int
    recheck_having_subs: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download English subtitles for movies in local catalog.")
    parser.add_argument("--catalog", default="data/catalog/movies_2000_now.jsonl")
    parser.add_argument("--status", default="data/state/subtitle_status.jsonl")
    parser.add_argument("--subs-root", default="subs/movie")
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year)
    parser.add_argument("--providers", default="opensubtitles_legacy,opensubtitles,subdl")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")))
    parser.add_argument("--max-titles", type=int, default=0)
    parser.add_argument("--max-subs-per-title", type=int, default=3)
    parser.add_argument("--recheck-having-subs", action="store_true")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> SubtitleConfig:
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    def _resolve(path_str: str) -> Path:
        p = Path(path_str)
        return p if p.is_absolute() else (project_root / p)

    max_titles = args.max_titles if args.max_titles > 0 else None

    providers = [item.strip().lower() for item in args.providers.split(",") if item.strip()]
    if not providers:
        raise ValueError("At least one provider must be specified.")

    return SubtitleConfig(
        catalog_path=_resolve(args.catalog),
        status_path=_resolve(args.status),
        subs_root=_resolve(args.subs_root),
        start_year=args.start_year,
        end_year=args.end_year,
        providers=providers,
        timeout_seconds=max(1, args.timeout_seconds),
        max_titles=max_titles,
        max_subs_per_title=max(1, args.max_subs_per_title),
        recheck_having_subs=bool(args.recheck_having_subs),
    )


def load_catalog_movies(catalog_path: Path, start_year: int, end_year: int) -> list[dict[str, Any]]:
    if not catalog_path.exists() or not catalog_path.is_file():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    movies: list[dict[str, Any]] = []
    with catalog_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if item.get("media_type") != "movie":
                continue
            year = item.get("year")
            if not isinstance(year, int):
                continue
            if year < start_year or year > end_year:
                continue

            movies.append(item)

    movies.sort(key=lambda m: (m.get("year", 0), str(m.get("title", "")), m.get("tmdb_id", 0)))
    return movies


def load_status_map(status_path: Path) -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {}
    if not status_path.exists() or not status_path.is_file():
        return status

    with status_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            key = str(row.get("tmdb_id", ""))
            if key:
                status[key] = row
    return status


def append_status(status_path: Path, row: dict[str, Any]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with status_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).strip("_")
    return cleaned or "subtitle"


def download_bytes(url: str, timeout_seconds: int) -> bytes:
    request = Request(url=url, method="GET", headers={"User-Agent": "movie-sub-search/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:
        content = response.read()
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content


def get_env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def require_env(provider_name: str, env_names: list[str]) -> str:
    value = get_env_first(*env_names)
    if value:
        return value
    joined = ", ".join(env_names)
    raise RuntimeError(f"Provider '{provider_name}' requires one of: {joined}")


def provider_instances(config: SubtitleConfig) -> list[Any]:
    known: list[Any] = []

    if "opensubtitles" in config.providers:
        api_key = require_env("opensubtitles", ["OPENSUBTITLES_API_KEY"])
        username = require_env("opensubtitles", ["OPENSUBTITLES_USERNAME"])
        password = require_env("opensubtitles", ["OPENSUBTITLES_PASSWORD"])
        known.append(
            OpenSubtitlesProvider(
                api_key=api_key,
                username=username,
                password=password,
                user_agent="movie-sub-search/0.1",
                timeout_seconds=config.timeout_seconds,
            )
        )

    if "subdl" in config.providers:
        api_key = require_env("subdl", ["SUBDL_API_KEY"])
        known.append(
            SubDLProvider(
                api_key=api_key,
                timeout_seconds=config.timeout_seconds,
                user_agent="movie-sub-search/0.1",
            )
        )

    if "opensubtitles_legacy" in config.providers:
        # Free XML-RPC API used by VLC/QNapi: anonymous login, no account or
        # personal API key required. OpenSubtitles' shared "OSTestUserAgent"
        # has been disabled, so default to VLC's own registered agent (VLSub);
        # set OPENSUBTITLES_LEGACY_USER_AGENT to override with your own.
        user_agent = get_env_first("OPENSUBTITLES_LEGACY_USER_AGENT") or "VLSub 0.10.2"
        known.append(
            OpenSubtitlesLegacyProvider(
                user_agent=user_agent,
                timeout_seconds=config.timeout_seconds,
            )
        )
    return [p for p in known if p.is_enabled()]


def save_subtitle_file(
    *,
    subs_root: Path,
    tmdb_id: int,
    title: str,
    year: int,
    result: ProviderSearchResult,
    content: bytes,
) -> tuple[Path, str]:
    title_dir = subs_root / str(tmdb_id)
    title_dir.mkdir(parents=True, exist_ok=True)

    name_base = f"{safe_name(title)}_{year}_{result.provider}_{result.provider_sub_id}_{safe_name(result.file_name)}"
    file_name = f"{name_base}.bin"
    out_path = title_dir / file_name

    out_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
    meta = {
        "tmdb_id": tmdb_id,
        "title": title,
        "year": year,
        "provider": result.provider,
        "provider_sub_id": result.provider_sub_id,
        "file_name": result.file_name,
        "release_name": result.release_name,
        "language": result.language,
        "sha256": digest,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path, digest


def run(config: SubtitleConfig) -> int:
    print(f"Catalog: {config.catalog_path}")
    print(f"Status : {config.status_path}")
    print(f"Subs   : {config.subs_root}")
    print(f"Years  : {config.start_year}..{config.end_year}")

    providers = provider_instances(config)
    if not providers:
        raise RuntimeError(
            "No subtitle providers enabled. Configure at least one: "
            "OpenSubtitles (OPENSUBTITLES_API_KEY, OPENSUBTITLES_USERNAME, OPENSUBTITLES_PASSWORD) "
            "or SubDL (SUBDL_API_KEY)."
        )

    print("Enabled providers: " + ", ".join(p.name for p in providers))

    movies = load_catalog_movies(config.catalog_path, config.start_year, config.end_year)
    if config.max_titles is not None:
        movies = movies[: config.max_titles]
    print(f"Movies to process: {len(movies)}")

    status_map = load_status_map(config.status_path)
    downloaded_count = 0
    checked_count = 0

    for idx, movie in enumerate(movies, start=1):
        tmdb_id = movie.get("tmdb_id")
        title = str(movie.get("title") or "")
        year = int(movie.get("year") or 0)
        if not isinstance(tmdb_id, int):
            continue

        state_key = str(tmdb_id)
        previous = status_map.get(state_key)
        if previous and previous.get("has_en_subs") is True and not config.recheck_having_subs:
            print(f"[{idx}/{len(movies)}] skip tmdb_id={tmdb_id} title={title} (already has subtitles)")
            continue

        print(f"[{idx}/{len(movies)}] search tmdb_id={tmdb_id} title={title} ({year})")
        checked_count += 1

        already_downloaded_ids: set[str] = set(previous.get("downloaded_ids", []) if previous else [])
        found_any = bool(already_downloaded_ids)
        total_saved_for_title = len(already_downloaded_ids)
        last_provider = None
        last_error = None

        for provider in providers:
            last_provider = provider.name
            try:
                results = provider.search_movie(title=title, year=year, tmdb_id=tmdb_id)
            except Exception as exc:
                last_error = str(exc)
                print(f"  provider={provider.name} error={exc}")
                continue

            if not results:
                print(f"  provider={provider.name} no results")
                continue

            print(f"  provider={provider.name} candidates={len(results)}")
            for result in results:
                if total_saved_for_title >= config.max_subs_per_title:
                    break

                sub_key = f"{result.provider}:{result.provider_sub_id}"
                if sub_key in already_downloaded_ids:
                    continue

                try:
                    content = download_bytes(result.download_url, config.timeout_seconds)
                    out_path, digest = save_subtitle_file(
                        subs_root=config.subs_root,
                        tmdb_id=tmdb_id,
                        title=title,
                        year=year,
                        result=result,
                        content=content,
                    )
                except Exception as exc:
                    print(f"    download failed provider={provider.name} sub_id={result.provider_sub_id} err={exc}")
                    last_error = str(exc)
                    continue

                total_saved_for_title += 1
                downloaded_count += 1
                found_any = True
                already_downloaded_ids.add(sub_key)
                print(
                    f"    saved {out_path} bytes={len(content)} sha256={digest[:12]}..."
                )

            if total_saved_for_title >= config.max_subs_per_title:
                # Quota met from an earlier (e.g. unlimited) provider: skip the
                # remaining rate-limited providers for this title.
                break

        now = datetime.now(timezone.utc).isoformat()
        status_row = {
            "media_type": "movie",
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "has_en_subs": found_any,
            "downloaded_count": total_saved_for_title,
            "downloaded_ids": sorted(already_downloaded_ids),
            "last_checked_at": now,
            "last_provider": last_provider,
            "last_result": "downloaded" if found_any else ("error" if last_error else "not_found"),
            "last_error": last_error,
        }
        append_status(config.status_path, status_row)
        status_map[state_key] = status_row

    print(
        "Done: "
        f"checked={checked_count}, subtitles_saved={downloaded_count}, "
        f"status_file={config.status_path}"
    )
    return 0


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
