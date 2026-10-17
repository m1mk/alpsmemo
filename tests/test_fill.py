"""音数合わせ（伸ばす・繰り返す・休符）と VOICEVOX の読み変換のテスト。"""

import pytest

from alpsmemo import render_read, render_sing
from alpsmemo.config import PROJECT_ROOT, load_melody
from alpsmemo.mora import LONG_MARK, REST, can_stretch, fill_repeat, fill_rest, fill_stretch, reading_from_kana, split_morae

MELODY = load_melody(PROJECT_ROOT / "melodies" / "alps_verse.json")


def test_can_stretch():
    assert can_stretch("カ") and can_stretch("ジュ") and can_stretch("ー")
    assert not can_stretch("ッ") and not can_stretch("ン") and not can_stretch(REST)


def test_fill_rest():
    assert fill_rest(["イ", "ツ", "カ"], 6) == ["イ", "ツ", "カ", REST, REST, REST]
    assert fill_rest(["イ", "ツ", "カ"], 3) == ["イ", "ツ", "カ"]
    with pytest.raises(ValueError):
        fill_rest(["イ", "ツ", "カ"], 2)


def test_fill_stretch_puts_long_marks_evenly_and_last_longest():
    assert fill_stretch(["イ", "ツ", "カ"], 6) == ["イ", "ー", "ツ", "ー", "カ", "ー"]
    assert fill_stretch(["イ", "ツ", "カ"], 4) == ["イ", "ツ", "カ", "ー"]
    assert fill_stretch(["イ", "ツ", "カ"], 5) == ["イ", "ツ", "ー", "カ", "ー"]
    assert fill_stretch(["イ", "ツ", "カ"], 3) == ["イ", "ツ", "カ"]


def test_fill_stretch_skips_unstretchable_morae():
    # ッ・ン の後ろには伸ばしを置かない
    assert fill_stretch(["ガ", "ッ", "コ", "ン"], 6) == ["ガ", "ー", "ッ", "コ", "ー", "ン"]
    # 伸ばせる音がなければ休符
    assert fill_stretch(["ッ", "ン"], 4) == ["ッ", "ン", REST, REST]
    assert fill_stretch([], 3) == [REST, REST, REST]


def test_fill_repeat():
    assert fill_repeat(["イ", "ツ", "カ"], 6) == ["イ", "ツ", "カ", "イ", "ツ", "カ"]
    assert fill_repeat(["イ", "ツ", "カ"], 8) == ["イ", "ツ", "カ", "ー", "イ", "ツ", "カ", "ー"]
    assert fill_repeat(["イ", "ツ", "カ", REST], 8) == ["イ", "ツ", "カ", "ー", "イ", "ツ", "カ", "ー"]  # 休符は捨てて繰り返す
    assert fill_repeat([REST, REST], 4) == [REST] * 4


def test_stretched_line_is_sung_as_held_vowel_not_rest():
    """伸ばした行は休符にならず、前の母音で歌い続ける（歌唱）。読み上げでも途切れない。"""
    lines = [
        ["ジュ", "ウ", "ゴ", "ド", "ナ", "ラ", "ジュ", "ウ"],
        fill_stretch(["イ", "ツ", "カ"], 6),
        ["コ", "ン", "ク", "リ", "ー", "ト", "ノ", "ッ"],
        fill_stretch(["ヨ", "ウ", "ジョ", "ウ"], 6),
    ]
    notes, _ = render_sing.build_notes(lines, MELODY, 110)
    sung = notes[1:-1]
    assert [n["lyric"] for n in sung[8:14]] == ["イ", "イ", "ツ", "ウ", "カ", "ア"]
    assert all(n["key"] is not None for n in sung[8:14])
    segs = render_read.segments(MELODY.phrases[1], lines[1])
    assert len(segs) == 1 and len(segs[0][1]) == 6  # 休符で分かれない


@pytest.mark.parametrize("kana, expected", [
    ("ジュ'ーゴド/ナラ", "ジューゴドナラ"),
    ("_ス'キ", "スキ"),
    ("イツカ、イジョ'ー？", "イツカイジョー"),
    ("ヨ'ージョー キ'カン", "ヨージョーキカン"),
])
def test_reading_from_kana(kana, expected):
    assert reading_from_kana(kana) == expected
    split_morae(reading_from_kana(kana))  # そのまま読みとして使える


def test_long_mark_is_single_mora_in_reading():
    assert split_morae("イーツーカー") == ["イ", LONG_MARK, "ツ", LONG_MARK, "カ", LONG_MARK]
