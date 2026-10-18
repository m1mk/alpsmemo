"""melodies フォルダの曲ファイルと、歌詞に記録した曲の復元のテスト。"""

from alpsmemo.config import PROJECT_ROOT, Config, list_melodies, load_melody, melody_for_lyrics

MELODIES = PROJECT_ROOT / "melodies"


def _cfg():
    return Config(base_dir=PROJECT_ROOT)


def test_all_melody_files_load_and_have_titles():
    melodies = list_melodies(_cfg())
    names = [m.path.name for m in melodies]
    assert names[0] == "alps_verse.json"  # 既定の曲が先頭
    assert {"twinkle.json", "mary.json", "jingle_bells.json", "railroad.json", "bear.json"} <= set(names)
    for m in melodies:
        assert m.title and m.description, m.path
        assert all(n > 0 for n in m.phrase_lengths)
        assert m.beats_per_bar in (2, 3, 4)
        for phrase in m.phrases:  # 各行は小節の整数倍（拍の頭が揃う）
            beats = sum(b for _, b in phrase)
            assert abs(beats / m.beats_per_bar - round(beats / m.beats_per_bar)) < 1e-9, (m.path, phrase)


def test_known_phrase_lengths():
    assert load_melody(MELODIES / "twinkle.json").phrase_lengths == [7, 7, 7, 7, 7, 7]
    assert load_melody(MELODIES / "mary.json").phrase_lengths == [7, 6, 8, 5]
    assert load_melody(MELODIES / "jingle_bells.json").phrase_lengths == [6, 5, 8, 6, 6, 5, 8, 6]
    assert load_melody(MELODIES / "railroad.json").verified is False
    assert "要確認" in load_melody(MELODIES / "bear.json").label


def test_melody_for_lyrics_uses_recorded_file_or_matches_lengths():
    cfg = _cfg()
    assert melody_for_lyrics(cfg, {"melody_file": "twinkle.json"}).name == "twinkle"
    # 古い歌詞（melody_file なし）は既定の曲
    assert melody_for_lyrics(cfg, {"phrase_lengths": [8, 6, 8, 6]}).name == "alps_ichimanjaku_verse"
    # 音数から曲を探す
    assert melody_for_lyrics(cfg, {"phrase_lengths": [7, 6, 8, 5]}).name == "mary"
    # 明示指定が最優先
    assert melody_for_lyrics(cfg, {"melody_file": "twinkle.json"}, "melodies/mary.json").name == "mary"


def test_transposed_keeps_metadata():
    m = load_melody(MELODIES / "twinkle.json").transposed(-3)
    assert m.title == "きらきら星" and m.path == MELODIES / "twinkle.json" and m.phrases[0][0][0] == 57
