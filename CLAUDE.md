# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FanFicFare (FFF) is a tool for downloading fanfiction from 100+ websites into eBook formats (EPUB, MOBI, TXT, HTML). It operates as both a CLI tool (`pip install FanFicFare`) and a Calibre plugin. Python 3.7+.

## Build & Development Commands

```bash
# Install for development
pip install -e .

# Install with image processing support
pip install -e '.[image_processing]'

# Run CLI
fanficfare [options] [STORYURL]

# Run all tests
pytest

# Run a single test file
pytest tests/adapters/test_adapter_chireadscom.py

# Run a specific test class or method
pytest tests/adapters/test_adapter_chireadscom.py::TestExtractChapterUrlsAndMetadata::test_get_metadata

# Build Calibre plugin zip
python makeplugin.py

# Bump version (updates pyproject.toml, calibre-plugin/__init__.py, fanficfare/cli.py)
python version_update.py test        # increment micro version
python version_update.py release     # increment minor, reset micro to 0
```

## Architecture

### Core Package (`fanficfare/`)

**Adapter Pattern** — The central design pattern. Each supported website has an adapter in `fanficfare/adapters/adapter_<sitename>.py` (~110 adapters). All inherit from `BaseSiteAdapter` (or `BaseEfictionAdapter` for eFiction-based sites).

Each adapter must:
- Define a class with a `getClass()` module-level function that returns it
- Implement `getSiteDomain()`, `getSiteExampleURLs()`, `getSiteURLPattern()`
- Implement `extractChapterUrlsAndMetadata()` — parse story listing page for metadata and chapter URLs
- Implement `getChapterText(url)` — fetch and return chapter HTML content

Adapter registration is automatic: `adapters/__init__.py` imports all `adapter_*` modules, calls `getClass()` on each, and builds a domain-to-class map. To add a new site, create the adapter module and add its import to `adapters/__init__.py`.

**Inheritance chain**: `BaseSiteAdapter` → `Requestable` → `Configurable` (provides config access and HTTP fetching)

**Configuration** (`configurable.py`) — INI-based config using cascading sections with this priority order (lowest to highest):
1. `[defaults]` — base settings in `defaults.ini`
2. `[sitedomain.com]` — site-specific
3. `[epub]` / `[html]` etc. — format-specific
4. `[sitedomain.com:epub]` — site+format
5. `[overrides]` — CLI options override everything

**Story** (`story.py`) — Holds all story metadata and chapter data. Handles image processing (tries calibre, then PIL/Pillow, then falls back to no processing).

**Fetchers** (`fetchers/`) — HTTP fetching with decorators for caching and rate-limiting:
- `fetcher_requests.py` — standard requests-based
- `fetcher_cloudscraper.py` — CloudFlare bypass
- `fetcher_flaresolverr_proxy.py`, `fetcher_nsapa_proxy.py` — proxy-based fetchers
- `cache_basic.py`, `cache_browser.py` — page caching (basic in-memory and browser cache reading)
- `decorators.py` — `SleepDecorator` (rate limiting), `ProgressBarDecorator`

**Writers** (`writers/`) — Output format writers: `writer_epub.py`, `writer_html.py`, `writer_mobi.py`, `writer_txt.py`. All inherit from `base_writer.py`.

**CLI** (`cli.py`) — Entry point. Contains the version string (must stay in sync with `pyproject.toml` and `calibre-plugin/__init__.py`). Uses `version_update.py` to coordinate version bumps across all three files.

### Calibre Plugin (`calibre-plugin/`)

Separate UI layer that wraps the core `fanficfare/` package for use inside Calibre. The plugin zip is built by `makeplugin.py`, which bundles `calibre-plugin/`, `included_dependencies/`, and `fanficfare/` into `FanFicFare.zip`.

### Tests (`tests/`)

Uses pytest with `unittest.mock`. Tests mock `get_request` to avoid real HTTP calls. Fixture data lives in `tests/fixtures_*.py` files. The `GenericAdapterTestExtractChapterUrlsAndMetadata` and `GenericAdapterTestGetChapterText` base classes in `tests/adapters/generic_adapter_test.py` provide reusable test patterns for adapter testing.

### Key Utilities

- `htmlcleanup.py` — HTML sanitization (strip tags, decode emails, entity handling)
- `htmlheuristics.py` — heuristic HTML cleanup (e.g., converting `<br>` chains to `<p>` tags)
- `epubutils.py` — EPUB reading/manipulation for update workflows
- `geturls.py` — Extract story URLs from web pages and IMAP
- `dateutils.py` — Date parsing across various site formats
- `six.py` — Bundled Python 2/3 compatibility layer (historical, still used throughout)

## Version Synchronization

Version appears in three places that must stay in sync:
- `pyproject.toml` (`version = "X.Y.Z"`)
- `fanficfare/cli.py` (`version="X.Y.Z"`)
- `calibre-plugin/__init__.py` (`__version__ = (X, Y, Z)`)

Always use `python version_update.py` to update versions.
