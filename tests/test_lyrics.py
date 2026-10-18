import json
from types import SimpleNamespace

from alpsmemo.config import PROJECT_ROOT, Config, load_melody
from alpsmemo.lyrics import generate, list_themes, load_lyrics, rewrite_block, save_lyrics, validate_block

MELODY = load_melody(PROJECT_ROOT / "melodies" / "alps_verse.json")
ORIGINAL = "普通ポルトランドセメントの湿潤養生期間は、日平均気温15℃以上で5日以上とする。"


def line(display, morae):
    return {"display": display, "morae": list(morae) if isinstance(morae, list) else morae}


def good_block():
    return {
        "source": "日平均気温15℃以上で5日以上",
        "keywords": ["15℃", "5日"],
        "lines": [
            line("15℃なら", ["ジュ", "ウ", "ゴ", "ド", "ナ", "ラ", "ジュ", "ウ"]),  # 8
            line("5日以上", ["イ", "ツ", "カ", "イ", "ジョ", "ウ"]),  # 6
            line("15℃なら", ["ジュ", "ウ", "ゴ", "ド", "ナ", "ラ", "ゴ", "ド"]),  # 8
            line("5日養生", ["イ", "ツ", "カ", "ヨ", "ウ", "ジョ"]),  # 6
        ],
    }


def test_good_block_passes():
    assert validate_block(good_block(), MELODY.phrase_lengths, ORIGINAL) == []


def test_mora_count_mismatch_reports_line_and_diff():
    b = good_block()
    b["lines"][1]["morae"].append("ダ")
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("2行目: 7音です。6音必要です（1音多い）" in e for e in errs)


def test_multi_mora_element_rejected():
    b = good_block()
    b["lines"][0]["morae"][0:2] = ["ジュウ", "ゴ"]  # 要素数は8のまま
    b["lines"][0]["morae"].insert(2, "ゴ")
    b["lines"][0]["morae"].pop()
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("「ジュウ」は1モーラ" in e for e in errs)


def test_keyword_missing_from_display():
    b = good_block()
    b["keywords"] = ["湿潤養生"]
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("「湿潤養生」が歌詞の表示" in e for e in errs)


def test_keyword_altered_from_original():
    b = good_block()
    b["keywords"] = ["15℃", "6日"]
    b["lines"][3]["display"] = "6日養生"
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("「6日」が原文にありません" in e for e in errs)


def test_number_in_source_must_appear():
    b = good_block()
    b["lines"][1]["display"] = "いつか以上"
    b["lines"][3]["display"] = "いつか養生"
    b["keywords"] = ["15℃"]
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("数字「5」" in e for e in errs)  # 「15」に含まれる5では合格にしない


def test_source_must_be_verbatim():
    b = good_block()
    b["source"] = "気温15度以上なら5日"
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("source" in e for e in errs)


class FakeClient:
    """Claude API の代わりに決まった応答を順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.responses.pop(0)
        msg = SimpleNamespace(stop_reason="end_turn",
                              content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: msg)

            def __exit__(self_inner, *a):
                return False

        return Ctx()


def _cfg():
    return Config(base_dir=PROJECT_ROOT, max_retries=3, backend="api")


def test_generate_retries_until_valid():
    bad = good_block()
    bad["lines"][0]["morae"] = bad["lines"][0]["morae"][:7]
    client = FakeClient([{"blocks": [bad]}, good_block()])
    data = generate(ORIGINAL, MELODY, _cfg(), client=client, log=lambda s: None)
    assert data["blocks"][0]["needs_review"] is False
    assert len(client.calls) == 2
    fix_prompt = client.calls[1]["messages"][0]["content"]
    assert "1行目: 7音です。8音必要です（1音少ない）" in fix_prompt
    assert client.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_generate_marks_needs_review_after_max_retries():
    bad = good_block()
    bad["lines"][0]["morae"] = bad["lines"][0]["morae"][:7]
    client = FakeClient([{"blocks": [bad, good_block()]}, bad, bad, bad])
    data = generate(ORIGINAL, MELODY, _cfg(), client=client, log=lambda s: None)
    assert data["blocks"][0]["needs_review"] is True
    assert data["blocks"][0]["errors"]
    assert data["blocks"][1]["needs_review"] is False
    assert len(client.calls) == 4  # 初回 + 作り直し3回


def test_save_and_load_accepts_morae_string(tmp_path):
    data = {"melody": MELODY.name, "phrase_lengths": MELODY.phrase_lengths, "blocks": [good_block()]}
    data["blocks"][0]["lines"][1]["morae"] = "イツカイジョウ"  # 人が文字列で書き直した場合
    save_lyrics(data, tmp_path)
    loaded = load_lyrics(tmp_path)
    assert loaded["blocks"][0]["lines"][1]["morae"] == ["イ", "ツ", "カ", "イ", "ジョ", "ウ"]
    assert "ブロック1" in (tmp_path / "lyrics.txt").read_text(encoding="utf-8")


def test_list_themes(tmp_path):
    import os
    import time

    assert list_themes(tmp_path / "nothing") == []
    old = {"melody": "m", "phrase_lengths": [8, 6, 8, 6], "blocks": [good_block(), dict(good_block(), needs_review=True)]}
    save_lyrics(old, tmp_path / "古い")
    (tmp_path / "古い" / "sing").mkdir()
    (tmp_path / "古い" / "sing" / "all.wav").write_bytes(b"")
    t = time.time() - 100
    os.utime(tmp_path / "古い" / "lyrics.json", (t, t))
    save_lyrics({"melody": "m", "phrase_lengths": [8, 6, 8, 6], "blocks": [good_block()]}, tmp_path / "新しい")
    (tmp_path / "壊れた").mkdir()
    (tmp_path / "壊れた" / "lyrics.json").write_text("{", encoding="utf-8")
    (tmp_path / "関係ないフォルダ").mkdir()

    themes = list_themes(tmp_path)
    assert [t["name"] for t in themes] == ["新しい", "壊れた", "古い"] or [t["name"] for t in themes][2] == "古い"
    by = {t["name"]: t for t in themes}
    assert by["古い"]["blocks"] == 2 and by["古い"]["review"] == 1 and by["古い"]["audio"] == ["sing"]
    assert by["古い"]["preview"] == "15℃なら" and by["古い"]["dir"] == tmp_path / "古い"
    assert by["新しい"]["audio"] == [] and by["新しい"]["review"] == 0
    assert by["壊れた"]["broken"] is True and by["壊れた"]["blocks"] == 0
    assert "関係ないフォルダ" not in by


def _fake_run(payloads, calls):
    def run(cmd, input, capture_output, cwd, timeout, creationflags=0):
        calls.append((cmd, input.decode("utf-8")))
        out = json.dumps(payloads.pop(0), ensure_ascii=False).encode("utf-8")
        return SimpleNamespace(stdout=out, stderr=b"", returncode=0)
    return run


def test_generate_via_claude_code(monkeypatch):
    import alpsmemo.lyrics as lyr
    calls = []
    monkeypatch.setattr(lyr.shutil, "which", lambda name: "claude.exe")
    monkeypatch.setattr(lyr.subprocess, "run", _fake_run(
        [{"type": "result", "is_error": False, "result": "", "structured_output": {"blocks": [good_block()]}}], calls))
    cfg = _cfg()
    cfg.backend = "claude_code"
    data = generate(ORIGINAL, MELODY, cfg, log=lambda s: None)
    assert data["blocks"][0]["needs_review"] is False
    cmd, stdin = calls[0]
    assert cmd[1] == "-p" and "--json-schema" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert ORIGINAL in stdin


def test_claude_code_login_error(monkeypatch):
    import pytest
    import alpsmemo.lyrics as lyr
    monkeypatch.setattr(lyr.shutil, "which", lambda name: "claude.exe")
    monkeypatch.setattr(lyr.subprocess, "run", _fake_run(
        [{"type": "result", "is_error": True, "result": "Failed to authenticate: OAuth session expired"}], []))
    cfg = _cfg()
    cfg.backend = "claude_code"
    with pytest.raises(lyr.LyricsError, match="claude auth login"):
        generate(ORIGINAL, MELODY, cfg, log=lambda s: None)


def test_rewrite_block_sends_edits_and_instruction():
    """手直し中のブロック（音数が合っていない）と指示を AI に渡し、検証済みのブロックを返す。"""
    edited = good_block()
    edited["lines"][1] = line("5日", ["イ", "ツ", "カ"])  # 人が短くした
    edited["needs_review"] = True
    client = FakeClient([good_block()])
    out = rewrite_block(edited, ORIGINAL, MELODY, _cfg(), "2行目を短くしたので合わせて", client=client, log=lambda s: None)
    assert out["needs_review"] is False and out["errors"] == []
    assert len(client.calls) == 1
    prompt = client.calls[0]["messages"][0]["content"]
    assert "2行目を短くしたので合わせて" in prompt
    assert "2行目: 3音です。6音必要です（3音少ない）" in prompt
    assert '"イ",' in prompt and "needs_review" not in prompt
    assert client.calls[0]["output_config"]["format"]["schema"]["required"] == ["source", "keywords", "lines"]


def test_rewrite_block_retries_when_result_invalid():
    edited = good_block()
    bad = good_block()
    bad["lines"][0]["morae"] = bad["lines"][0]["morae"][:7]
    client = FakeClient([bad, good_block()])
    out = rewrite_block(edited, ORIGINAL, MELODY, _cfg(), "", client=client, log=lambda s: None)
    assert out["needs_review"] is False
    assert len(client.calls) == 2
    assert "指示なし" in client.calls[0]["messages"][0]["content"]


def test_number_from_other_block_rejected():
    """別ブロックの数字が混ざった歌詞（原文にはある数字でも）は不合格にする。"""
    original = ORIGINAL + "早強セメントは3日とする。"
    b = good_block()
    b["lines"][3]["display"] = "3日養生"
    errs = validate_block(b, MELODY.phrase_lengths, original)
    assert any("source にない数字「3」" in e for e in errs)


def test_source_with_ellipsis_excerpts():
    b = good_block()
    b["source"] = "日平均気温15℃以上…5日以上"
    assert validate_block(b, MELODY.phrase_lengths, ORIGINAL) == []
    b["source"] = "日平均気温15℃以上…6日以上"
    errs = validate_block(b, MELODY.phrase_lengths, ORIGINAL)
    assert any("「6日以上」が原文と一致しません" in e for e in errs)
