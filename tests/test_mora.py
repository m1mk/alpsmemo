import pytest

from alpsmemo.mora import MoraError, count_morae, is_single_mora, split_morae, vowel_of


@pytest.mark.parametrize("text, expected", [
    ("ジュ", ["ジュ"]),
    ("ニジュウハチニチノ", ["ニ", "ジュ", "ウ", "ハ", "チ", "ニ", "チ", "ノ"]),
    ("ガッコウ", ["ガ", "ッ", "コ", "ウ"]),
    ("コンクリート", ["コ", "ン", "ク", "リ", "ー", "ト"]),
    ("ヴァイオリン", ["ヴァ", "イ", "オ", "リ", "ン"]),
    ("キョウ ジョウ", ["キョ", "ウ", "ジョ", "ウ"]),
    ("クヮシ", ["クヮ", "シ"]),
])
def test_split(text, expected):
    assert split_morae(text) == expected
    assert count_morae(text) == len(expected)


@pytest.mark.parametrize("item", ["ジュ", "ッ", "ン", "ー", "ヴァ", "ア", "フィ"])
def test_single_mora_ok(item):
    assert is_single_mora(item)


@pytest.mark.parametrize("item", ["", "ジュウ", "ャ", "ッャ", "じゅ", "28", "ア ", "ンー", 3])
def test_single_mora_ng(item):
    assert not is_single_mora(item)


@pytest.mark.parametrize("text", ["ャア", "あいう", "ニ28"])
def test_split_errors(text):
    with pytest.raises(MoraError):
        split_morae(text)


def test_vowel_of():
    assert vowel_of("カ") == "a"
    assert vowel_of("ジュ") == "u"
    assert vowel_of("ヴァ") == "a"
    assert vowel_of("ン") is None
    assert vowel_of("ッ") is None


def test_rest_marker():
    assert split_morae("イツカ＿＿_") == ["イ", "ツ", "カ", "＿", "＿", "＿"]
    assert is_single_mora("＿")
    with pytest.raises(MoraError):
        split_morae("＿ャ")
