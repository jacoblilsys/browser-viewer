"""
Tests for the viewer's version and its GitHub release check (update_check).

No network is used: update_check.urlopen is replaced by a stub.

Run standalone:
    python backend/tests/test_update_check.py
(also discoverable by pytest as test_* functions).
"""

import asyncio
import io
import json
import os
import sys
import urllib.error

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import update_check
import version
from version import is_newer, parse


class _Stub:
    """Stands in for urlopen: returns `release` as JSON, or raises `exc`."""
    def __init__(self, release=None, exc=None):
        self.release, self.exc, self.calls = release, exc, []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        if self.exc:
            raise self.exc
        return io.BytesIO(json.dumps(self.release).encode())


def _release(tag):
    return {'tag_name': tag, 'name': f'Viewer {tag}', 'published_at': '2026-10-02T00:00:00Z',
            'html_url': f'https://github.com/x/y/releases/tag/{tag}', 'body': 'notes ' * 200}


def _run(stub, refresh=False, env=None):
    saved = update_check.urlopen, dict(os.environ)
    update_check.urlopen = stub
    os.environ.update(env or {})
    try:
        return asyncio.run(update_check.check(refresh=refresh))
    finally:
        update_check.urlopen = saved[0]
        os.environ.clear()
        os.environ.update(saved[1])


def test_parse_and_compare():
    assert parse('v1.2.3') == (1, 2, 3) and parse('1.2') == (1, 2)
    assert parse('v1.2-beta') is None and parse('') is None and parse(None) is None
    assert is_newer('v1.2.10', '1.2.9') and not is_newer('v1.2.9', '1.2.10')
    assert not is_newer('v1.0.0', '1.0.0') and not is_newer('1.0', '1.0.0')
    assert is_newer('1.0.1', '1.0') and not is_newer('nightly', '1.0.0')
    print('  ok  version parsing and comparison')


def test_newer_same_older():
    cur = version.__version__
    major = parse(cur)[0]
    for tag, want in ((f'v{major + 1}.0.0', True), (f'v{cur}', False), ('v0.0.1', False)):
        update_check.reset_cache()
        r = _run(_Stub(_release(tag)))
        assert r['newer'] is want and r['latest'] == tag.lstrip('v') and not r['error'], (tag, r)
        assert r['current'] == cur and r['url'].endswith(tag) and len(r['notes']) <= 500
    print('  ok  newer / same / older release reported correctly')


def test_no_release_yet_is_not_an_error():
    update_check.reset_cache()
    err = urllib.error.HTTPError('u', 404, 'Not Found', {}, None)
    r = _run(_Stub(exc=err))
    assert r['latest'] is None and r['newer'] is False and r['error'] == '', r
    assert r['url'].endswith('/releases')
    print('  ok  a repository without releases is "no release yet", not an error')


def test_offline_is_quiet():
    update_check.reset_cache()
    r = _run(_Stub(exc=urllib.error.URLError('Name or service not known')))
    assert r['newer'] is False and 'URLError' in r['error'], r
    update_check.reset_cache()
    r = _run(_Stub(exc=TimeoutError('timed out')))
    assert r['newer'] is False and 'TimeoutError' in r['error'], r
    print('  ok  offline / timeout: error recorded, no update claimed')


def test_cache_and_refresh():
    update_check.reset_cache()
    stub = _Stub(_release('v99.0.0'))
    _run(stub)
    _run(stub)
    assert len(stub.calls) == 1, 'second check must come from the cache'
    _run(stub, refresh=True)
    assert len(stub.calls) == 2, 'refresh must bypass the cache'
    print('  ok  results cached; refresh re-checks')


def test_disabled_makes_no_request():
    update_check.reset_cache()
    stub = _Stub(_release('v99.0.0'))
    r = _run(stub, env={'UPDATE_CHECK': '0'})
    assert stub.calls == [] and r['enabled'] is False and r['newer'] is False, r
    print('  ok  UPDATE_CHECK=0 sends nothing')


def test_repo_override():
    update_check.reset_cache()
    stub = _Stub(_release('v1.0.0'))
    _run(stub, env={'UPDATE_REPO': 'someone/fork'})
    assert stub.calls == ['https://api.github.com/repos/someone/fork/releases/latest'], stub.calls
    print('  ok  UPDATE_REPO selects the repository')


# ── standalone runner ───────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    print(f'Running {len(tests)} tests...\n')
    for t in tests:
        t()
    print(f'\nAll {len(tests)} tests passed.')


if __name__ == '__main__':
    _run_all()
