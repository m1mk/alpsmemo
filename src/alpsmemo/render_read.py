"""読み上げモード：通常の読み上げAPIで「1モーラ＝1音符」の唱え方にする。

各モーラの長さを音符の長さで上書きする。子音は前の拍に食い込ませ、
母音の立ち上がりが拍の頭に来るようにする（クリック音と拍が揃って聞こえる）。
"""

from __future__ import annotations

from .audio import SAMPLE_RATE, Track, read_wav, sec_to_samples
from .config import Melody
from .mora import LONG_MARK, REST
from .voicevox import VoicevoxClient, VoicevoxError

FPS = 24000 / 256  # VOICEVOX の音素長のフレームレート（93.75）
MAX_LEAD_SEC = 0.15  # 行頭の子音を拍の前に出す上限


class RenderError(Exception):
    pass


def segments(notes: list[tuple[int | None, float]], morae: list[str]) -> list[tuple[float, list[tuple[str, float]]]]:
    """フレーズを休符で区切り、[(開始拍, [(モーラ, 拍数), ...]), ...] にする。"""
    segs: list[tuple[float, list[tuple[str, float]]]] = []
    cur = None
    t = 0.0
    mi = 0
    for key, beats in notes:
        mora = None
        if key is not None:
            mora = morae[mi]
            mi += 1
        if mora is None or mora == REST or (mora == LONG_MARK and cur is None):  # 休符（休符直後の「ー」も）
            cur = None
        else:
            if cur is None:
                cur = (t, [])
                segs.append(cur)
            cur[1].append((mora, beats))
        t += beats
    return segs


def _flatten(query: dict) -> list[dict]:
    return [m for ap in query["accent_phrases"] for m in ap["moras"]]


def build_query(client: VoicevoxClient, morae: list[str], speaker: int) -> dict:
    """audio_query を取得し、モーラ数が歌詞と一致することを確認する。

    一致しない場合は AquesTalk風記法（is_kana）で取り直す。それでもダメならエラー。
    """
    text = "".join(morae)
    query = client.audio_query(text, speaker)
    got = len(_flatten(query))
    if got == len(morae):
        return query
    kana = morae[0] + "'" + "".join(morae[1:])
    try:
        aps = client.accent_phrases_kana(kana, speaker)
    except VoicevoxError:
        aps = None
    if aps is not None and sum(len(ap["moras"]) for ap in aps) == len(morae):
        query["accent_phrases"] = aps
        return query
    raise RenderError(
        f"VOICEVOXの読みのモーラ数（{got}）が歌詞のモーラ数（{len(morae)}）と一致しません: {'・'.join(morae)}\n"
        f"→ lyrics.json のこの行の morae を、読み間違えにくい表記に直してから再実行してください。"
    )


def _fill_pitch(moras: list[dict]) -> None:
    """無声化されていたモーラ（pitch=0）に、前後の有声モーラの高さを入れる。

    pitch=0 のまま母音を引き延ばすと、かすれたささやき声になるため。ッ（cl）は無音なのでそのまま。
    """
    voiced = [i for i, m in enumerate(moras) if m.get("pitch", 0) > 0]
    if not voiced:
        return
    for i, m in enumerate(moras):
        if m.get("pitch", 0) > 0 or m["vowel"] == "cl":
            continue
        prev = max((j for j in voiced if j < i), default=None)
        nxt = min((j for j in voiced if j > i), default=None)
        near = [moras[j]["pitch"] for j in (prev, nxt) if j is not None]
        m["pitch"] = sum(near) / len(near)


def apply_timing(query: dict, durations_sec: list[float]) -> int:
    """各モーラの長さを音符の長さに書き換える。戻り値は行頭の子音のはみ出しフレーム数。"""
    moras = _flatten(query)
    assert len(moras) == len(durations_sec)
    # 各音符の開始位置（フレーム）。累積で丸めて誤差をためない
    bounds = [0]
    acc = 0.0
    for d in durations_sec:
        acc += d
        bounds.append(round(acc * FPS))
    cons = []
    for i, m in enumerate(moras):
        c = 0
        if m.get("consonant") and m.get("consonant_length"):
            limit = (bounds[i] - bounds[i - 1]) // 2 if i > 0 else round(MAX_LEAD_SEC * FPS)
            c = max(1, min(round(m["consonant_length"] * FPS), limit))
        cons.append(c)
    cons.append(0)
    for i, m in enumerate(moras):
        if m.get("consonant"):
            m["consonant_length"] = cons[i] / FPS
        m["vowel_length"] = (bounds[i + 1] - cons[i + 1] - bounds[i]) / FPS
        if m["vowel"] in ("A", "I", "U", "E", "O"):  # 無声化をやめて拍をはっきり出す
            m["vowel"] = m["vowel"].lower()
    _fill_pitch(moras)
    for ap in query["accent_phrases"]:
        ap["pause_mora"] = None
    query["speedScale"] = 1.0
    query["prePhonemeLength"] = 0.0
    query["postPhonemeLength"] = 0.0
    query["outputSamplingRate"] = SAMPLE_RATE
    query["outputStereo"] = False
    return cons[0]


def render_segment(client: VoicevoxClient, morae_beats: list[tuple[str, float]], beat_sec: float, speaker: int):
    """休符を含まない連続した音符列を1回で合成する。戻り値は (サンプル, 子音のはみ出しサンプル数)。"""
    morae = [m for m, _ in morae_beats]
    durations = [b * beat_sec for _, b in morae_beats]
    query = build_query(client, morae, speaker)
    lead_frames = apply_timing(query, durations)
    samples, rate = read_wav(client.synthesis(query, speaker))
    if rate != SAMPLE_RATE:
        raise RenderError(f"想定外のサンプリングレート {rate}Hz が返されました")
    lead = lead_frames * 256
    want = lead + sec_to_samples(sum(durations))
    # 長さを「音符の長さの合計」ぴったりにする（余りは無音、はみ出しは切る）
    if len(samples) < want:
        samples.extend([0] * (want - len(samples)))
    else:
        del samples[want:]
    return samples, lead


def render_block(client: VoicevoxClient, lines: list[list[str]], melody: Melody, bpm: float, speaker: int) -> Track:
    """1ブロック（メロディ1周）を合成する。Track の位置0が1拍目。"""
    beat_sec = 60.0 / bpm
    track = Track()
    for phrase_start, notes, morae in zip(melody.phrase_start_beats(), melody.phrases, lines):
        for seg_start, morae_beats in segments(notes, morae):
            samples, lead = render_segment(client, morae_beats, beat_sec, speaker)
            track.add(sec_to_samples((phrase_start + seg_start) * beat_sec) - lead, samples)
    return track
