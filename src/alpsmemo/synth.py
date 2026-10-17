"""lyrics.json から音声ファイルを作る（読み上げ／歌唱 共通の出力処理）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import render_read, render_sing
from .audio import SAMPLE_RATE, Track, add_clicks, sec_to_samples, write_wav
from .config import Config, Melody
from .lyrics import load_lyrics, validate_shape, write_lyrics_txt
from .voicevox import VoicevoxClient


@dataclass
class SynthResult:
    out_dir: Path
    files: list[Path] = field(default_factory=list)
    skipped: list[tuple[int, list[str]]] = field(default_factory=list)
    needs_review: list[int] = field(default_factory=list)


def synthesize(out_dir: Path, cfg: Config, melody: Melody, *, mode: str, bpm: float, repeat: int, click: bool,
               log: Callable[[str], None] = print) -> SynthResult:
    data = load_lyrics(out_dir)
    blocks = data.get("blocks", [])
    write_lyrics_txt(data, out_dir / "lyrics.txt")  # 手直しした内容を反映
    result = SynthResult(out_dir=out_dir / mode)

    client = VoicevoxClient(cfg.voicevox_url)
    client.version()  # 起動確認

    beat_sec = 60.0 / bpm
    block_len = sec_to_samples(melody.total_beats() * beat_sec)
    bar_len = sec_to_samples(melody.beats_per_bar * beat_sec)

    mode_dir = out_dir / mode
    mode_dir.mkdir(parents=True, exist_ok=True)
    for old in list(mode_dir.glob("block_*.wav")) + list(mode_dir.glob("all*.wav")):
        old.unlink()  # 前回の生成物（ブロック数が変わった場合の残りを防ぐ）

    mora_beats = melody.mora_start_beats()
    timing: dict[str, list[dict]] = {}

    def block_events(idx: int, origin: int) -> list[dict]:
        """ブロックの1拍目がファイルの origin サンプル目にあるときの、各モーラの開始時刻（歌詞表示用）。"""
        ev = [{"t": round((origin + sec_to_samples(b * beat_sec)) / SAMPLE_RATE, 4), "block": idx, "line": li, "mora": mi}
              for li, beats in enumerate(mora_beats) for mi, b in enumerate(beats)]
        ev.append({"t": round((origin + block_len) / SAMPLE_RATE, 4), "block": idx, "line": None, "mora": None})
        return ev

    rendered: list[tuple[int, Track]] = []
    for idx, block in enumerate(blocks, 1):
        # 人が手直しした後の内容を、音数だけはもう一度検証する（原文との照合は lyrics 時に済み）
        errors = validate_shape(block, melody.phrase_lengths)
        if errors:
            result.skipped.append((idx, errors))
            log(f"  ブロック{idx}: 音数が合わないためスキップしました")
            for e in errors:
                log(f"    - {e}")
            continue
        if block.get("needs_review"):
            result.needs_review.append(idx)
        lines = [line["morae"] for line in block["lines"]]
        log(f"  ブロック{idx}: 合成中…")
        if mode == "read":
            track = render_read.render_block(client, lines, melody, bpm, cfg.speaker_read)
        else:
            track = render_sing.render_block(client, lines, melody, bpm, cfg.speaker_sing_query, cfg.speaker_sing)
        if click:
            add_clicks(track, 0, melody.total_beats(), beat_sec, melody.beats_per_bar)
        path = mode_dir / f"block_{idx:02d}.wav"
        write_wav(path, track.render(min(track.start, 0), max(track.end, block_len)))
        timing[path.name] = block_events(idx, -min(track.start, 0))
        result.files.append(path)
        rendered.append((idx, track))

    if not rendered:
        return result

    def combine(times: int, name: str) -> Track:
        """全ブロックを結合（各ブロックを times 回ずつ）。ブロック間は1小節の無音。"""
        all_track = Track()
        pos = 0
        events = []
        seq = [r for r in rendered for _ in range(times)]
        for i, (idx, t) in enumerate(seq):
            all_track.add(pos + t.start, t.render())
            events.append((idx, pos))
            pos += block_len
            if i < len(seq) - 1:
                if click:
                    add_clicks(all_track, pos, melody.beats_per_bar, beat_sec, melody.beats_per_bar)
                pos += bar_len
        shift = -min(all_track.start, 0)
        timing[name] = [e for idx, p in events for e in block_events(idx, p + shift)]
        write_wav(mode_dir / name, all_track.render(min(all_track.start, 0)))
        result.files.append(mode_dir / name)
        return all_track

    combine(1, "all.wav")
    if repeat > 1:
        combine(repeat, f"all_repeat{repeat}.wav")
    (mode_dir / "timing.json").write_text(json.dumps({"bpm": bpm, "files": timing}, ensure_ascii=False), encoding="utf-8")
    return result

