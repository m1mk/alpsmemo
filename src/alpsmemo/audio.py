"""WAVの読み書き・無音・クリック音・タイムライン合成（標準ライブラリのみ）。

音声はすべて 16bit モノラルの array('h') で扱う。
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 24000  # VOICEVOX の既定出力


def read_wav(data: bytes) -> tuple[array, int]:
    with wave.open(io.BytesIO(data)) as wf:
        if wf.getsampwidth() != 2:
            raise ValueError("16bit以外のWAVには対応していません")
        rate = wf.getframerate()
        ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if ch == 2:
        samples = array("h", ((samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples), 2)))
    elif ch != 1:
        raise ValueError(f"{ch}チャンネルのWAVには対応していません")
    return samples, rate


def write_wav(path: Path, samples: array, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def sec_to_samples(sec: float, rate: int = SAMPLE_RATE) -> int:
    return round(sec * rate)


def click(rate: int = SAMPLE_RATE, accent: bool = False, dur: float = 0.03) -> array:
    """拍ごとのクリック音（減衰する短いサイン波）。小節頭は高めの音。"""
    freq = 1760.0 if accent else 1320.0
    amp = 9000 if accent else 6500
    n = sec_to_samples(dur, rate)
    return array("h", (int(amp * math.exp(-6 * i / n) * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))


class Track:
    """サンプル位置を指定して音を重ねていくタイムライン。負の位置にも置ける。"""

    def __init__(self) -> None:
        self.start = 0  # buf[0] のタイムライン上の位置
        self.buf: list[int] = []

    def _ensure(self, lo: int, hi: int) -> None:
        if not self.buf:
            self.start = lo
        if lo < self.start:
            self.buf[0:0] = [0] * (self.start - lo)
            self.start = lo
        end = self.start + len(self.buf)
        if hi > end:
            self.buf.extend([0] * (hi - end))

    @property
    def end(self) -> int:
        return self.start + len(self.buf)

    def add(self, pos: int, samples) -> None:
        if len(samples) == 0:
            return
        self._ensure(pos, pos + len(samples))
        off = pos - self.start
        buf = self.buf
        for i, s in enumerate(samples):
            buf[off + i] += s

    def render(self, frm: int | None = None, to: int | None = None) -> array:
        """[frm, to) を16bitに丸めて返す。範囲外は無音。"""
        frm = self.start if frm is None else frm
        to = self.start + len(self.buf) if to is None else to
        out = array("h", bytes(2 * max(0, to - frm)))
        for i in range(max(frm, self.start), min(to, self.start + len(self.buf))):
            v = self.buf[i - self.start]
            out[i - frm] = 32767 if v > 32767 else -32768 if v < -32768 else v
        return out


def add_clicks(track: Track, origin: int, beats: float, beat_sec: float, beats_per_bar: int, rate: int = SAMPLE_RATE) -> None:
    """origin から beats 拍分、拍の頭にクリック音を重ねる。"""
    hi, lo = click(rate, accent=True), click(rate)
    k = 0
    while k < beats - 1e-9:
        track.add(origin + sec_to_samples(k * beat_sec, rate), hi if k % beats_per_bar == 0 else lo)
        k += 1
