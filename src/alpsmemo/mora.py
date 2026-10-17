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


# ---------------------------------------------------------------- 音数合わせ（人が歌詞を短くしたとき用）

def can_stretch(mora: str) -> bool:
    """この音の後ろに「ー」を足して伸ばせるか（母音のある音か、すでに伸ばしている「ー」）。"""
    return mora == LONG_MARK or vowel_of(mora) is not None


def fill_rest(morae: list[str], need: int) -> list[str]:
    """足りない分を休符（＿）で埋める。"""
    if len(morae) > need:
        raise ValueError(f"{len(morae)}音あります（{need}音まで）")
    return list(morae) + [REST] * (need - len(morae))


def fill_stretch(morae: list[str], need: int) -> list[str]:
    """足りない分を、母音のある音の後ろに「ー」を入れて均等に伸ばす。

    歌唱では「ー」は直前の母音で歌うので、休符にせずメロディが続く。
    伸ばせる音がない（ッ・ン・休符だけ）ときは休符で埋める。
    余りは後ろの音ほど多く配る（行末の音が一番長く伸びる）。
    """
    if len(morae) > need:
        raise ValueError(f"{len(morae)}音あります（{need}音まで）")
    extra = need - len(morae)
    idx = [i for i, m in enumerate(morae) if can_stretch(m)]
    if not extra:
        return list(morae)
    if not idx:
        return fill_rest(morae, need)
    k = len(idx)
    add = {i: (j + 1) * extra // k - j * extra // k for j, i in enumerate(idx)}
    out: list[str] = []
    for i, m in enumerate(morae):
        out.append(m)
        out.extend([LONG_MARK] * add.get(i, 0))
    return out


def fill_repeat(morae: list[str], need: int) -> list[str]:
    """読みをまるごと繰り返して埋め、端数は伸ばして埋める（例: イツカ → イツカイツカ）。"""
    if len(morae) > need:
        raise ValueError(f"{len(morae)}音あります（{need}音まで）")
    body = [m for m in morae if m != REST]
    if not body:
        return fill_rest(morae, need)
    times = max(1, need // len(body))
    return fill_stretch(body * times, need)


def reading_from_kana(kana: str) -> str:
    """VOICEVOX の audio_query が返す kana（AquesTalk風記法）を、読みのカタカナだけにする。

    例: "ジュ'ーゴド/ナラ" → "ジューゴドナラ"、"_ス'キ" → "スキ"（_ は無声化の印）。
    """
    return "".join(ch for ch in kana if ch not in "'/_、？?" and not ch.isspace())
