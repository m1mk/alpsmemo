"""設定ファイル（config.toml）とメロディファイルの読み込み。"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    base_dir: Path
    backend: str = "claude_code"  # claude_code（サブスク）または api（APIキー）
    model: str = "claude-sonnet-5-5"
    max_retries: int = 3
    fallbacks: str = "default"
    voicevox_url: str = "http://127.0.0.1:50021"
    speaker_read: int = 3
    speaker_sing_query: int = 6000
    speaker_sing: int = 3003
    melody: str = "melodies/alps_verse.json"
    bpm: float = 110
    repeat: int = 1
    click: bool = True
    transpose: int = 0
    mode: str = "sing"
    prompts_dir: str = "prompts"
    extra: dict = field(default_factory=dict)

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.base_dir / p


def find_config(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"設定ファイルが見つかりません: {p}\n→ パスを確認してください。")
        return p
    for cand in (Path.cwd() / "config.toml", PROJECT_ROOT / "config.toml"):
        if cand.is_file():
            return cand
    return None


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    if path is None:
        return Config(base_dir=PROJECT_ROOT)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml の書式が正しくありません: {e}\n→ {path} を見直してください。") from e
    ai = data.get("ai", {})
    vv = data.get("voicevox", {})
    music = data.get("music", {})
    cfg = Config(base_dir=path.resolve().parent)
    cfg.backend = ai.get("backend", cfg.backend)
    if cfg.backend not in ("claude_code", "api"):
        raise ConfigError(f"config.toml の [ai] backend は \"claude_code\" か \"api\" にしてください（現在: {cfg.backend}）。")
    cfg.model = ai.get("model", cfg.model)
    cfg.max_retries = int(ai.get("max_retries", cfg.max_retries))
    cfg.fallbacks = ai.get("fallbacks", cfg.fallbacks)
    cfg.voicevox_url = vv.get("url", cfg.voicevox_url).rstrip("/")
    cfg.speaker_read = int(vv.get("speaker_read", cfg.speaker_read))
    cfg.speaker_sing_query = int(vv.get("speaker_sing_query", cfg.speaker_sing_query))
    cfg.speaker_sing = int(vv.get("speaker_sing", cfg.speaker_sing))
    cfg.melody = music.get("melody", cfg.melody)
    cfg.bpm = float(music.get("bpm", cfg.bpm))
    cfg.repeat = int(music.get("repeat", cfg.repeat))
    cfg.click = bool(music.get("click", cfg.click))
    cfg.transpose = int(music.get("transpose", cfg.transpose))
    cfg.mode = music.get("mode", cfg.mode)
    if cfg.mode not in ("read", "sing"):
        raise ConfigError(f"config.toml の [music] mode は \"read\" か \"sing\" にしてください（現在: {cfg.mode}）。")
    return cfg


@dataclass
class Melody:
    name: str
    bpm: float | None
    phrases: list[list[tuple[int | None, float]]]  # 各フレーズ = [(key, beats), ...]、key=None は休符
    beats_per_bar: int = 2  # 4分の2拍子
    title: str = ""  # 画面に出す曲名
    description: str = ""  # どんな内容に向くか
    verified: bool = True  # False = 楽譜なしの書き起こしで、音程が要確認
    path: Path | None = None  # 読み込んだファイル

    @property
    def label(self) -> str:
        """画面用の曲名（音数つき）。"""
        return f"{self.title or self.name}（{'・'.join(map(str, self.phrase_lengths))}音）" + ("" if self.verified else "　※音程は要確認")

    @property
    def phrase_lengths(self) -> list[int]:
        """各フレーズの音数（休符を除いた音符数 = その行のモーラ数）。"""
        return [sum(1 for key, _ in p if key is not None) for p in self.phrases]

    def phrase_beats(self) -> list[float]:
        return [sum(b for _, b in p) for p in self.phrases]

    def total_beats(self) -> float:
        return sum(self.phrase_beats())

    def phrase_start_beats(self) -> list[float]:
        starts, t = [], 0.0
        for b in self.phrase_beats():
            starts.append(t)
            t += b
        return starts

    def mora_start_beats(self) -> list[list[float]]:
        """各行の各モーラ（休符以外の音符）が始まる拍（ブロック先頭からの拍数）。"""
        out = []
        for start, phrase in zip(self.phrase_start_beats(), self.phrases):
            t, beats = start, []
            for key, b in phrase:
                if key is not None:
                    beats.append(t)
                t += b
            out.append(beats)
        return out

    def transposed(self, semitones: int) -> "Melody":
        if not semitones:
            return self
        return Melody(self.name, self.bpm, [[(None if k is None else k + semitones, b) for k, b in p] for p in self.phrases],
                      self.beats_per_bar, self.title, self.description, self.verified, self.path)



def load_melody(path: Path) -> Melody:
    if not path.is_file():
        raise ConfigError(f"メロディファイルが見つかりません: {path}\n→ --melody か config.toml の [music] melody を確認してください。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        phrases = []
        for i, ph in enumerate(data["phrases"], 1):
            notes = []
            for n in ph["notes"]:
                key, beats = n
                if key is not None:
                    key = int(key)
                beats = float(beats)
                if beats <= 0:
                    raise ValueError(f"フレーズ{i}に長さ0以下の音符があります")
                notes.append((key, beats))
            if not notes:
                raise ValueError(f"フレーズ{i}に音符がありません")
            phrases.append(notes)
        if not phrases:
            raise ValueError("phrases が空です")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"メロディファイルの形式が正しくありません（{path}）: {e}\n→ notes は [key, beats] の配列にしてください。") from e
    return Melody(name=data.get("name", path.stem), bpm=data.get("bpm"), phrases=phrases,
                  beats_per_bar=int(data.get("beats_per_bar", 2)), title=str(data.get("title", "")),
                  description=str(data.get("description", "")), verified=bool(data.get("verified", True)), path=path)


def list_melodies(cfg: Config) -> list[Melody]:
    """melodies フォルダ（config.toml の melody と同じフォルダ）の曲を、既定の曲を先頭にして返す。"""
    default = cfg.resolve(cfg.melody)
    folder = default.parent
    melodies = []
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            melodies.append(load_melody(p))
        except ConfigError:
            continue
    melodies.sort(key=lambda m: m.path != default)
    return melodies


def melody_for_lyrics(cfg: Config, data: dict, explicit: str | None = None) -> Melody:
    """歌詞データ（lyrics.json）を作ったときの曲を返す。explicit があればそれを優先。

    lyrics.json の melody_file（melodies フォルダからの相対パス）を見る。古い歌詞（melody_file なし）は
    config.toml の既定の曲を使い、音数が合わない場合はフォルダ内から音数の合う曲を探す。
    """
    if explicit:
        return load_melody(cfg.resolve(explicit))
    folder = cfg.resolve(cfg.melody).parent
    if data.get("melody_file"):
        return load_melody(folder / data["melody_file"])
    melody = load_melody(cfg.resolve(cfg.melody))
    lengths = data.get("phrase_lengths")
    if lengths and melody.phrase_lengths != lengths:
        for m in list_melodies(cfg):
            if m.phrase_lengths == lengths:
                return m
    return melody
