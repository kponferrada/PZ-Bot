"""/modlist: "Mod Name - <Workshop link>" lines and paging.

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jeeves_modmanager as mm  # noqa: E402


def test_link_lines():
    titles = {"111": "Siege Night", "222": "Mod *with* stars\nand a newline"}
    lines = mm.link_lines(["111", "222", "333"], titles)
    assert lines[0] == "Siege Night - <https://steamcommunity.com/sharedfiles/filedetails/?id=111>"
    assert lines[1].startswith("Mod \\*with\\* stars and a newline - <")      # one line, markdown escaped
    assert lines[2].startswith("Workshop item 333 - <")                         # no name found
    assert mm.file_lines(["111"], titles) == [
        "Siege Night - https://steamcommunity.com/sharedfiles/filedetails/?id=111"]


def test_pages_never_split_a_line():
    lines = [f"Mod {i:03d} - <{mm.workshop_url(str(3_000_000_000 + i))}>" for i in range(270)]
    body = mm.pages(lines, limit=4000)
    assert all(len(p) <= 4000 for p in body) and len(body) > 1
    assert "\n".join(body).split("\n") == lines
    assert mm.pages([]) == []
