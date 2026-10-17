"""alpsmemo コマンド（lyrics / synth / run / check）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config, load_melody
from .lyrics import LyricsError, generate, save_lyrics
from .render_read import RenderError as ReadRenderError
from .render_sing import RenderError as SingRenderError
from .synth import synthesize
from .voicevox import VoicevoxClient, VoicevoxError


def _read_input(path: str) -> str:
    if path == "-":
        print("覚えたい文章を貼り付けて、最後に Ctrl+Z → Enter を押してください：", file=sys.stderr)
        text = sys.stdin.read()
    else:
        p = Path(path)
        if not p.is_file():
            raise LyricsError(f"入力ファイルが見つかりません: {p}\n→ パスを確認してください。")
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp932")  # メモ帳の古い既定（Shift_JIS）にも対応
    text = text.strip()
    if not text:
        raise LyricsError("入力の文章が空です。")
    return text


def _melody(cfg: Config, args):
    path = cfg.resolve(args.melody) if getattr(args, "melody", None) else cfg.resolve(cfg.melody)
    return load_melody(path).transposed(args.transpose if args.transpose is not None else cfg.transpose)


def cmd_lyrics(args, cfg: Config) -> int:
    melody = _melody(cfg, args)
    text = _read_input(args.input)
    out_dir = Path(args.output)
    data = generate(text, melody, cfg)
    save_lyrics(data, out_dir)
    print(f"\n歌詞を保存しました: {out_dir / 'lyrics.json'}（一覧は {out_dir / 'lyrics.txt'}）")
    review = [i for i, b in enumerate(data["blocks"], 1) if b.get("needs_review")]
    if review:
        print("\n★要確認のブロック（自動修正できませんでした）:")
        for i in review:
            print(f"  ブロック{i}")
            for e in data["blocks"][i - 1]["errors"]:
                print(f"    - {e}")
    print("\n次の手順: lyrics.txt で数字・用語が原文どおりか確認し、必要なら lyrics.json を直してください。")
    return 0


def cmd_synth(args, cfg: Config) -> int:
    melody = _melody(cfg, args)
    mode = args.mode or cfg.mode
    bpm = args.bpm or cfg.bpm
    repeat = args.repeat or cfg.repeat
    click = cfg.click if args.click is None else args.click
    out_dir = Path(args.dir)
    print(f"音声を生成しています（{'読み上げ' if mode == 'read' else '歌唱'}モード、BPM {bpm:g}、クリック音{'あり' if click else 'なし'}）…")
    result = synthesize(out_dir, cfg, melody, mode=mode, bpm=bpm, repeat=repeat, click=click)
    print()
    if result.files:
        print(f"出力しました（{result.out_dir}）:")
        for f in result.files:
            print(f"  {f.name}")
    if result.needs_review:
        print(f"\n注意: ブロック {', '.join(map(str, result.needs_review))} は「要確認」のままです。数字・用語を確認してください"
              "（確認済みなら lyrics.json の needs_review を false に）。")
    if result.skipped:
        print(f"\nスキップしたブロック: {', '.join(str(i) for i, _ in result.skipped)}")
        print("→ lyrics.json の該当行の morae を直してから、もう一度 synth を実行してください。")
        return 1 if not result.files else 0
    return 0


def cmd_run(args, cfg: Config) -> int:
    args.dir = args.output
    rc = cmd_lyrics(args, cfg)
    if rc:
        return rc
    print()
    return cmd_synth(args, cfg)


def cmd_check(args, cfg: Config) -> int:
    client = VoicevoxClient(cfg.voicevox_url)
    try:
        version = client.version()
    except VoicevoxError as e:
        print(e)
        return 1
    print(f"VOICEVOX ENGINE 版数: {version}（{cfg.voicevox_url}）\n")

    print("■ 読み上げ用の話者（config.toml の speaker_read に ID を設定）")
    read_ids = set()
    for sp in client.speakers():
        styles = "、".join(f"{st['name']}={st['id']}" for st in sp["styles"])
        read_ids.update(st["id"] for st in sp["styles"])
        print(f"  {sp['name']}: {styles}")

    query_ids, style_ids = set(), set()
    query_lines, style_lines = [], []
    for sp in client.singers():
        q = [st for st in sp["styles"] if st.get("type") == "sing"]
        f = [st for st in sp["styles"] if st.get("type") != "sing"]
        if q:
            query_ids.update(st["id"] for st in q)
            query_lines.append(f"  {sp['name']}: " + "、".join(f"{st['name']}={st['id']}" for st in q))
        if f:
            style_ids.update(st["id"] for st in f)
            style_lines.append(f"  {sp['name']}: " + "、".join(f"{st['name']}={st['id']}" for st in f))
    print("\n■ 歌唱クエリ用（speaker_sing_query に ID を設定）")
    print("\n".join(query_lines) or "  （なし）")
    print("\n■ 歌唱スタイル（speaker_sing に ID を設定）")
    print("\n".join(style_lines) or "  （なし）")

    print("\n■ 現在の設定")
    for label, value, ok in (
        ("speaker_read", cfg.speaker_read, cfg.speaker_read in read_ids),
        ("speaker_sing_query", cfg.speaker_sing_query, cfg.speaker_sing_query in query_ids),
        ("speaker_sing", cfg.speaker_sing, cfg.speaker_sing in style_ids),
    ):
        print(f"  {label} = {value}  {'OK' if ok else '← 一覧にありません。上の一覧から選んで config.toml を書き換えてください'}")
    return 0


def cmd_gui(args, cfg: Config) -> int:
    from .gui import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alpsmemo", description="覚えたい文章をアルプス一万尺の替え歌にして音声化するツール")
    p.add_argument("--config", help="設定ファイル（省略時はカレントまたはツールのフォルダの config.toml）")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--melody", help="メロディファイル（例: melodies/alps_verse.json）")
    common.add_argument("--transpose", type=int, help="移調（半音単位、例: -2）")
    audio = argparse.ArgumentParser(add_help=False)
    audio.add_argument("--mode", choices=["read", "sing"], help="read=読み上げ、sing=歌唱（既定: config.toml の mode）")
    audio.add_argument("--bpm", type=float, help="テンポ（既定: config.toml の値）")
    audio.add_argument("--repeat", type=int, help="各ブロックをN回繰り返した版も出力する")
    audio.add_argument("--click", dest="click", action="store_true", default=None, help="クリック音を重ねる")
    audio.add_argument("--no-click", dest="click", action="store_false", help="クリック音なし")

    s = sub.add_parser("lyrics", parents=[common], help="歌詞を生成して lyrics.json と lyrics.txt を出力")
    s.add_argument("input", help="覚えたい文章のテキストファイル（- で貼り付け入力）")
    s.add_argument("-o", "--output", required=True, help="出力フォルダ（例: out/テーマ名）")
    s.set_defaults(func=cmd_lyrics)

    s = sub.add_parser("synth", parents=[common, audio], help="lyrics.json から音声を生成")
    s.add_argument("dir", help="lyrics.json のあるフォルダ（例: out/テーマ名）")
    s.set_defaults(func=cmd_synth)

    s = sub.add_parser("run", parents=[common, audio], help="lyrics と synth を続けて実行")
    s.add_argument("input", help="覚えたい文章のテキストファイル（- で貼り付け入力）")
    s.add_argument("-o", "--output", required=True, help="出力フォルダ（例: out/テーマ名）")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("check", help="VOICEVOXの起動確認と話者・歌唱スタイル一覧の表示")
    s.set_defaults(func=cmd_check, transpose=None)

    s = sub.add_parser("gui", help="簡易GUIを開く")
    s.set_defaults(func=cmd_gui, transpose=None)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (ConfigError, LyricsError, VoicevoxError, ReadRenderError, SingRenderError) as e:
        print(f"\nエラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except Exception as e:  # anthropic のエラーは種類ごとに案内を出す
        msg = _anthropic_message(e)
        if msg is None:
            raise
        print(f"\nエラー: {msg}", file=sys.stderr)
        return 1


def _anthropic_message(e: Exception) -> str | None:
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(e, anthropic.AuthenticationError):
        return "APIキーが正しくありません。\n→ 環境変数 ANTHROPIC_API_KEY の値を確認してください。"
    if isinstance(e, anthropic.NotFoundError):
        return f"モデルが見つかりません（{e.message}）。\n→ config.toml の [ai] model を確認してください。"
    if isinstance(e, anthropic.RateLimitError):
        return "APIの利用上限に達しました。\n→ しばらく待ってからもう一度実行してください。"
    if isinstance(e, anthropic.BadRequestError):
        return (f"APIがリクエストを受け付けませんでした: {e.message}\n"
                "→ config.toml の [ai] model を変えた場合は、fallbacks = \"\" にすると通ることがあります。")
    if isinstance(e, anthropic.APIStatusError):
        return f"APIでエラーが発生しました（HTTP {e.status_code}）: {e.message}\n→ しばらく待ってからもう一度実行してください。"
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude API に接続できませんでした。\n→ インターネット接続を確認してください。"
    return None


if __name__ == "__main__":
    sys.exit(main())
