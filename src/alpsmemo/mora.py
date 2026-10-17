"""カタカナのモーラ分割と数え上げ。

ルール（設計書より）:
- 小書き文字（ャュョァィゥェォヮ）は直前の文字と合わせて1モーラ
- ッ、ン、ー はそれぞれ1モーラ
- ＿（または _）は休符。その音符では歌わない（人が歌詞の一部を消すときに使う）
"""

from __future__ import annotations

SMALL_KANA = set("ャュョァィゥェォヮ")
LONG_MARK = "ー"
REST = "＿"

# 母音ごとのカタカナ（歌唱時に「ー」を前の母音に置き換えるのに使う）
_VOWEL_ROWS = {
    "a": "アカサタナハマヤラワガザダバパァャヮヵ",
    "i": "イキシチニヒミリギジヂビピィヰ",
    "u": "ウクスツヌフムユルグズヅブプゥュヴ",
    "e": "エケセテネヘメレゲゼデベペェヱヶ",
    "o": "オコソトノホモヨロヲゴゾドボポォョ",
}
_VOWEL_OF_CHAR = {ch: v for v, chars in _VOWEL_ROWS.items() for ch in chars}
VOWEL_KANA = {"a": "ア", "i": "イ", "u": "ウ", "e": "エ", "o": "オ"}


def _is_katakana(ch: str) -> bool:
    # ァ(U+30A1)〜ヴ(U+30F4) と 長音記号
    return "ァ" <= ch <= "ヴ" or ch == LONG_MARK


class MoraError(ValueError):
    pass


def split_morae(text: str) -> list[str]:
    """カタカナ文字列をモーラのリストに分割する。空白は無視する。"""
    morae: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in ("_", REST):
            morae.append(REST)
            continue
        if not _is_katakana(ch):
            raise MoraError(f"カタカナ以外の文字「{ch}」が含まれています（{text}）")
        if ch in SMALL_KANA:
            if not morae or morae[-1] in ("ッ", "ン", LONG_MARK, REST) or morae[-1][-1] in SMALL_KANA:
                raise MoraError(f"小書き文字「{ch}」の前に結合できる文字がありません（{text}）")
            morae[-1] += ch
        else:
            morae.append(ch)
    return morae


def count_morae(text: str) -> int:
    return len(split_morae(text))


def is_single_mora(item: str) -> bool:
    """morae配列の1要素が、正確に1モーラのカタカナかどうか。"""
    if not isinstance(item, str) or item != item.strip() or not item:
        return False
    try:
        return len(split_morae(item)) == 1
    except MoraError:
        return False


def vowel_of(mora: str) -> str | None:
    """モーラの母音（a/i/u/e/o）。ッ・ン・ー など母音を持たないものは None。"""
    if not mora:
        return None
    return _VOWEL_OF_CHAR.get(mora[-1])
