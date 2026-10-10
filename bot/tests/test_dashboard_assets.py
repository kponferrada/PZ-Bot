"""server_status.asset_fingerprint: dashboard images are re-uploaded only when
their content changes, not when a deploy touches their mtime.
Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server_status import asset_fingerprint  # noqa: E402


def test_fingerprint_ignores_mtime_but_follows_content(tmp_path):
    f = tmp_path / "banner.png"
    f.write_bytes(b"image-1")
    first = asset_fingerprint(f)
    os.utime(f, (1, 1))                       # a checkout rewrites the mtime
    assert asset_fingerprint(f) == first
    f.write_bytes(b"image-2")
    assert asset_fingerprint(f) != first
    assert first.startswith("banner.png:")
