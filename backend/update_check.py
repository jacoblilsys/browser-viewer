"""Is a newer viewer released on GitHub?

One unauthenticated request to the GitHub REST API for the repository's latest
release (drafts and pre-releases are skipped by that endpoint), cached so a
fleet of open browser tabs stays far below GitHub's 60 requests/hour limit.
Sensor LANs are often offline: every failure is quiet, reported only as
`error` for the Settings dialog, and never delays the server.

Environment:
  UPDATE_CHECK=0      no outbound request at all
  UPDATE_REPO=o/r     repository to check (default jacoblilsys/browser-viewer)
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from version import __version__, is_newer

_log = logging.getLogger('update')

DEFAULT_REPO = 'jacoblilsys/browser-viewer'
TIMEOUT_S = 5.0
CACHE_OK_S = 6 * 3600
CACHE_FAIL_S = 30 * 60
NOTES_MAX = 500

# Replaced in tests.
urlopen = urllib.request.urlopen

_cache: Optional[dict] = None       # last result
_cache_until = 0.0
_lock: Optional[asyncio.Lock] = None
_lock_loop = None


def enabled() -> bool:
    return os.environ.get('UPDATE_CHECK', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def repo() -> str:
    return os.environ.get('UPDATE_REPO', '').strip() or DEFAULT_REPO


def _fetch_blocking() -> Optional[dict]:
    """Latest release, or None when the repository has none (HTTP 404).
    Raises on network / HTTP errors."""
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo()}/releases/latest',
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': f'browser-viewer/{__version__}'})
    try:
        with urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {
        'tag':          data.get('tag_name') or '',
        'name':         data.get('name') or '',
        'html_url':     data.get('html_url') or '',
        'published_at': data.get('published_at') or '',
        'notes':        (data.get('body') or '')[:NOTES_MAX],
    }


def _result(release: Optional[dict], error: str = '') -> dict:
    tag = release['tag'] if release else None
    return {
        'current':      __version__,
        'latest':       tag.lstrip('v') if tag else None,
        'newer':        bool(tag) and is_newer(tag),
        'url':          (release or {}).get('html_url') or f'https://github.com/{repo()}/releases',
        'published_at': (release or {}).get('published_at', ''),
        'notes':        (release or {}).get('notes', ''),
        'checked_at':   int(time.time()),
        'error':        error,
        'enabled':      True,
    }


def _get_lock() -> asyncio.Lock:
    """One lock per event loop (an asyncio.Lock binds to the loop it first ran on)."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


async def check(refresh: bool = False) -> dict:
    """The update status, from cache unless it expired or `refresh`."""
    global _cache, _cache_until
    if not enabled():
        return {'current': __version__, 'latest': None, 'newer': False, 'url': '',
                'published_at': '', 'notes': '', 'checked_at': None, 'error': '',
                'enabled': False}
    async with _get_lock():
        if _cache is not None and not refresh and time.monotonic() < _cache_until:
            return _cache
        try:
            release = await asyncio.get_running_loop().run_in_executor(None, _fetch_blocking)
            res = _result(release)
            _cache_until = time.monotonic() + CACHE_OK_S
            if res['newer']:
                _log.info('Viewer %s is available (running %s): %s',
                          res['latest'], __version__, res['url'])
        except Exception as e:                       # noqa: BLE001 — offline is normal
            res = _result(None, error=f'{type(e).__name__}: {e}')
            _cache_until = time.monotonic() + CACHE_FAIL_S
            _log.debug('update check failed: %s', res['error'])
        _cache = res
        return res


def reset_cache():
    global _cache, _cache_until
    _cache, _cache_until = None, 0.0
