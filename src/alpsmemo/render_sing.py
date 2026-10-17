"""歌唱モード：VOICEVOX ENGINE の歌唱API（sing_frame_audio_query → frame_synthesis）で歌わせる。

ENGINE 0.25.2 で確認した仕様:
- フレームレートは 93.75（24000Hz ÷ 256）
- lyric に「ー」は使えない（400）。「ッ」「ン」は使える
"""

from __future__ import annotations

from .audio import SAMPLE_RATE, Track, read_wav
from .config import Melody
from .mora import LONG_MARK, REST, VOWEL_KANA, vowel_of
from .voicevox import VoicevoxClient, VoicevoxError

FPS = 24000 / 256
TAIL_SEC = 0.4  # 最後の音を自然に終わらせるための休符


class RenderError(Exception):
    pass


def sing_lyric(mora: str, prev: str | None) -> str | None:
    """歌唱用の歌詞に変換する。「ー」は直前の母音で歌う。歌えない場合は None（休符扱い）。"""
    if mora != LONG_MARK:
        return mora
    if prev == "ン":
        return "ン"
    v = vowel_of(prev) if prev else None
    return VOWEL_KANA[v] if v else None


def build_notes(lines: list[list[str]], melody: Melody, bpm: float) -> tuple[list[dict], int]:
    """楽譜（notes）を組み立てる。戻り値は (notes, 1拍目の位置[フレーム])。先頭には必ず休符を置く。"""
    beat_sec = 60.0 / bpm
    lead = max(round(beat_sec * FPS), round(0.3 * FPS))
    notes = [{"key": None, "frame_length": lead, "lyric": ""}]
    t = 0.0
    prev_frame = 0
    prev_lyric: str | None = None
    for phrase, morae in zip(melody.phrases, lines):
        mi = 0
        for key, beats in phrase:
            t += beats
            frame = round(t * beat_sec * FPS)  # 累積で丸めて誤差をためない
            length = frame - prev_frame
            prev_frame = frame
            lyric = None
            if key is not None:
                mora = morae[mi]
                mi += 1
                if mora == REST:  # 人が消した部分は休符
                    prev_lyric = None
                else:
                    lyric = sing_lyric(mora, prev_lyric)
                    prev_lyric = lyric if lyric else prev_lyric
            if lyric is None:
                notes.append({"key": None, "frame_length": length, "lyric": ""})
            else:
                notes.append({"key": key, "frame_length": length, "lyric": lyric})
    notes.append({"key": None, "frame_length": round(TAIL_SEC * FPS), "lyric": ""})
    return notes, lead


def render_block(client: VoicevoxClient, lines: list[list[str]], melody: Melody, bpm: float,
                 query_speaker: int, style_speaker: int) -> Track:
    """1ブロック（メロディ1周）を歌わせる。Track の位置0が1拍目。"""
    notes, lead = build_notes(lines, melody, bpm)
    if all(n["key"] is None for n in notes):  # 全部休符なら無音
        return Track()
    try:
        query = client.sing_frame_audio_query(notes, query_speaker)
    except VoicevoxError as e:
        raise RenderError(
            f"{e}\n→ 歌えない文字が含まれている可能性があります。lyrics.json の morae を確認するか、"
            f"config.toml の speaker_sing_query（alpsmemo check の「歌唱クエリ用」）を確認してください。"
        ) from e
    query["outputSamplingRate"] = SAMPLE_RATE
    query["outputStereo"] = False
    samples, rate = read_wav(client.frame_synthesis(query, style_speaker))
    if rate != SAMPLE_RATE:
        raise RenderError(f"想定外のサンプリングレート {rate}Hz が返されました")
    track = Track()
    track.add(-lead * 256, samples)
    return track
