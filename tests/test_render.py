import pytest

from alpsmemo import render_read, render_sing
from alpsmemo.audio import Track, add_clicks
from alpsmemo.config import PROJECT_ROOT, Melody, load_melody
from alpsmemo.voicevox import VoicevoxClient, VoicevoxError

MELODY = load_melody(PROJECT_ROOT / "melodies" / "alps_verse.json")
LINES = [
    ["ジュ", "ウ", "ゴ", "ド", "ナ", "ラ", "ジュ", "ウ"],
    ["イ", "ツ", "カ", "イ", "ジョ", "ウ"],
    ["コ", "ン", "ク", "リ", "ー", "ト", "ノ", "ッ"],
    ["イ", "ツ", "カ", "ヨ", "ウ", "ジョ"],
]


def test_melody_phrase_lengths():
    assert MELODY.phrase_lengths == [8, 6, 8, 6]
    assert MELODY.phrase_beats() == [4, 4, 4, 4]


def test_segments_split_on_rest():
    notes = [(60, 0.5), (None, 0.5), (62, 1)]
    assert render_read.segments(notes, ["ア", "イ"]) == [(0.0, [("ア", 0.5)]), (1.0, [("イ", 1)])]


def test_apply_timing_vowel_on_beat():
    moras = [{"text": "カ", "consonant": "k", "consonant_length": 0.06, "vowel": "a", "vowel_length": 0.1, "pitch": 5},
             {"text": "ス", "consonant": "s", "consonant_length": 0.08, "vowel": "U", "vowel_length": 0.1, "pitch": 0},
             {"text": "ン", "vowel": "N", "vowel_length": 0.1, "pitch": 5}]
    q = {"accent_phrases": [{"moras": moras, "accent": 1, "pause_mora": {"text": "、"}}]}
    lead = render_read.apply_timing(q, [0.25, 0.25, 0.5])
    fps = render_read.FPS
    frames = [round(m.get("consonant_length", 0) * fps) + round(m["vowel_length"] * fps) for m in moras]
    # 合計 = 行頭の子音のはみ出し(lead) + 音符の長さの合計
    assert sum(frames) == lead + round(1.0 * fps)
    assert lead == round(0.06 * fps)
    # 2つ目のモーラの母音開始 = 0.25秒
    v1_start = lead + round(moras[0]["vowel_length"] * fps) + round(moras[1]["consonant_length"] * fps)
    assert v1_start - lead == round(0.25 * fps)
    assert moras[1]["vowel"] == "u"  # 無声化をやめる
    assert q["accent_phrases"][0]["pause_mora"] is None


def test_sing_notes_frame_lengths_and_long_mark():
    notes, lead = render_sing.build_notes(LINES, MELODY, 110)
    assert notes[0]["key"] is None and notes[0]["lyric"] == ""  # 先頭は休符
    sung = notes[1:-1]
    assert len(sung) == sum(MELODY.phrase_lengths)
    total = sum(n["frame_length"] for n in sung)
    assert total == round(16 * 60 / 110 * render_sing.FPS)
    assert sung[18]["lyric"] == "イ"  # 「リー」の「ー」は前の母音で歌う
    assert sung[0]["key"] == 60 and sung[7]["key"] == 55


def test_transpose():
    m = MELODY.transposed(-2)
    assert m.phrases[0][0][0] == 58 and m.phrase_lengths == MELODY.phrase_lengths


def test_track_negative_and_clicks():
    t = Track()
    t.add(-10, [100] * 20)
    t.add(5, [100] * 5)
    out = t.render()
    assert t.start == -10 and len(out) == 20 and out[15] == 200
    add_clicks(t, 0, 4, 0.5, 2, rate=100)
    assert t.end >= 151


def _engine():
    c = VoicevoxClient()
    try:
        c.version()
    except VoicevoxError:
        pytest.skip("VOICEVOX が起動していません")
    return c


def test_read_line_length_matches_notes():
    """受け入れ基準1c：1行の長さが計算値と±10ms以内（実エンジン）。"""
    c = _engine()
    beat_sec = 60 / 110
    for notes, morae in zip(MELODY.phrases, LINES):
        mb = [(m, b) for m, (_, b) in zip(morae, notes)]
        samples, lead = render_read.render_segment(c, mb, beat_sec, 3)
        expected = sum(b for _, b in notes) * beat_sec
        assert abs((len(samples) - lead) / 24000 - expected) <= 0.010


def test_sing_block_renders():
    c = _engine()
    track = render_sing.render_block(c, LINES, MELODY, 110, 6000, 3003)
    assert track.start < 0  # 先頭の休符ぶん、1拍目より前から始まる
    assert track.end >= round(16 * 60 / 110 * 24000)


def test_fill_pitch_for_devoiced_mora():
    """無声化（pitch=0）のモーラは前後の高さで埋める（かすれ声防止）。ッは無音のまま。"""
    moras = [{"vowel": "a", "pitch": 5.8}, {"vowel": "U", "pitch": 0.0}, {"vowel": "cl", "pitch": 0.0},
             {"vowel": "o", "pitch": 6.0}]
    q = {"accent_phrases": [{"moras": moras, "accent": 1}]}
    render_read.apply_timing(q, [0.25] * 4)
    assert moras[1]["pitch"] == pytest.approx(5.9) and moras[1]["vowel"] == "u"
    assert moras[2]["pitch"] == 0.0


def test_rest_marker_becomes_rest():
    lines = [l[:] for l in LINES]
    lines[3] = ["イ", "ツ", "カ", "＿", "＿", "ー"]  # 消した部分は休符、休符の後の「ー」も休符
    notes, _ = render_sing.build_notes(lines, MELODY, 110)
    sung = notes[1:-1]
    assert [n["lyric"] for n in sung[22:]] == ["イ", "ツ", "カ", "", "", ""]
    assert all(n["key"] is None for n in sung[25:])
    segs = render_read.segments(MELODY.phrases[3], lines[3])
    assert segs == [(0.0, [("イ", 0.5), ("ツ", 0.5), ("カ", 0.5)])]
