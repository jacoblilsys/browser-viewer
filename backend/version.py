"""The viewer's own version — the single source, bumped by hand at each release.

A release is a git tag `v<__version__>` with a GitHub release on it (see the
README, "Releases & update check"). The running viewer compares this value with
the latest GitHub release to tell the operator a newer one exists.
"""
import re
from typing import Optional

__version__ = '1.0.0'

_VERSION_RE = re.compile(r'^v?(\d+(?:\.\d+)*)$')


def parse(tag: Optional[str]) -> Optional[tuple[int, ...]]:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    m = _VERSION_RE.match((tag or '').strip())
    return tuple(int(p) for p in m.group(1).split('.')) if m else None


def is_newer(latest: Optional[str], current: str = __version__) -> bool:
    """True only if both parse and `latest` is strictly higher (1.2 == 1.2.0)."""
    a, b = parse(latest), parse(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))
