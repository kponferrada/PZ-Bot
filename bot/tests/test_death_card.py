"""Tests for death_card: the formatting helpers and a full render (Pillow only).

Run from bot/:  python -m pytest tests
"""

import datetime
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import death_card as dc  # noqa: E402

PHT = datetime.timezone(datetime.timedelta(hours=8))


def test_normalize_part_enum_and_friendly_names():
    assert dc.normalize_part("Hand_L") == "hand_l"
    assert dc.normalize_part("ForeArm_R") == "forearm_r"
    assert dc.normalize_part("UpperArm_L") == "upperarm_l"
    assert dc.normalize_part("UpperLeg_R") == "upperleg_r"
    assert dc.normalize_part("LowerLeg_L") == "lowerleg_l"
    assert dc.normalize_part("Foot_R") == "foot_r"
    assert dc.normalize_part("Torso_Upper") == "torso_upper"
    assert dc.normalize_part("Torso_Lower") == "torso_lower"
    assert dc.normalize_part("Left Hand") == "hand_l"
    assert dc.normalize_part("right shin") == "lowerleg_r"
    assert dc.normalize_part("Head") == "head"
    assert dc.normalize_part("Hand") is None  # no side
    assert dc.normalize_part("") is None


def test_every_part_key_has_both_doll_anchors_and_a_label():
    keys = set(dc._PART_LABELS)
    assert keys == set(dc._FRONT) == set(dc._BACK)


def test_parse_injuries_both_mod_formats():
    old = dc.parse_injuries("Hand_R: Bleeding, Deep Wound; Head: Scratched")
    new = dc.parse_injuries("Right Hand (Bleeding, Deep Wound); Head (Scratched)")
    assert old == new == [("hand_r", ["bleeding", "deep wound"]), ("head", ["scratched"])]
    assert dc.parse_injuries("None") == []


def test_injury_lines_and_summary():
    inj = "Hand_L: Bitten, Bleeding; ForeArm_R: Scratched; Head: Scratched; Neck: Weird"
    assert dc.injury_lines(inj)[0] == "Left hand: Bitten, Bleeding"
    assert dc.injury_summary(inj) == ["Bite Wound", "Scratches (2)", "Bleeding Wound"]


def test_split_and_format_cause():
    assert dc.split_cause("Zombie - Shambler") == ("Zombie Attack", "Shambler")
    assert dc.split_cause("Player - bob") == ("Homicide", "bob")
    assert dc.split_cause("Animal - Wolf") == ("Animal Attack", "Wolf")
    assert dc.split_cause("Starvation") == ("Starvation", "")
    assert dc.split_cause("Player - x — by bob (Robert)") == ("Homicide", "bob (Robert)")
    assert dc.format_cause("Zombie - Shambler") == "Zombie Attack (Shambler)"
    assert dc.format_cause("Player - bob") == "Homicide — killed by bob"


def test_format_game_date():
    assert dc.format_game_date("1993-7-22 14:32") == "July 22, 1993   14:32"
    assert dc.format_game_date("1993-07-09") == "July 9, 1993"
    assert dc.format_game_date("day 3") == "day 3"


def test_format_location():
    assert dc.format_location("X: 10612.4, Y: 9845.9, Z: 0") == (
        "near Muldraugh (X 10612, Y 9846, Floor 0)", "near Muldraugh")
    assert dc.format_location("X: 300, Y: 300, Z: 1") == ("X 300, Y 300, Floor 1", "300, 300 (F1)")
    assert dc.format_location("") == ("Unknown", "Unknown")


def test_registry_number():
    issued = datetime.datetime(2026, 7, 14, 8, 32, tzinfo=PHT)
    assert dc.registry_number(issued, 327) == "BT-DC-20260714-00327"


def test_autopsy_notes():
    notes = dc.autopsy_notes("Zombie - Shambler", True, "12 days", "Head: Bitten")
    assert notes.startswith("Victim succumbed to a zombie attack (Shambler) after surviving 12 days.")
    assert "1 wound recorded." in notes
    assert notes.endswith("Signs of infection were present at time of death.")


def _data(**over):
    data = dict(
        survivor="silvast", character_name="Jamie Bear", infected="true",
        survival_time="23 days, 5 hours", zombie_kills="327", cause="Zombie - Shambler",
        injuries="Hand_L: Bitten, Bleeding; LowerLeg_R: Fractured",
        location="X: 10612, Y: 9845, Z: 0", game_date_time="1993-7-22 14:32",
        death_count=4, deaths_today=1, deaths_week=3, registry_serial=12,
        issued_at=datetime.datetime(2026, 10, 5, 19, 32, tzinfo=PHT),
    )
    data.update(over)
    return data


def test_render_with_and_without_avatar():
    template = Image.open(dc._CARD_PATH)
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 50, 50)).save(buf, format="PNG")
    with_avatar = dc.render_death_card(_data(), buf.getvalue())
    no_avatar = dc.render_death_card(_data(injuries="None", infected="false"), None)
    garbage = dc.render_death_card(_data(), b"not an image")
    for img in (with_avatar, no_avatar, garbage):
        assert img.size == template.size
    # The polaroid differs between a real avatar and the placeholder.
    box = dc._PHOTO_BOX
    assert with_avatar.crop(box).tobytes() != no_avatar.crop(box).tobytes()
    assert garbage.crop(box).tobytes() == no_avatar.crop(box).tobytes()


def test_left_username_is_discord_name_when_linked():
    # Left "Username" row (DEATH INFORMATION side) shows the linked Discord
    # name; unlinked survivors get "Not linked". The right side keeps the PZ name.
    row = (330, 345, 565, 376)
    right_row = (1000, 405, 1212, 432)
    linked = dc.render_death_card(_data(discord_name="keym"), None)
    unlinked = dc.render_death_card(_data(), None)
    assert linked.crop(row).tobytes() != unlinked.crop(row).tobytes()
    assert linked.crop(right_row).tobytes() == unlinked.crop(right_row).tobytes()
