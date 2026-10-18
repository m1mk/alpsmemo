"""歌詞生成（Claude Code または Claude API の呼び出し）・検証・リトライ、lyrics.json / lyrics.txt の入出力。

AIの出力はそのまま信じず、音数・1モーラ性・キーワード・数字を必ずプログラムで検証する。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Callable

from .config import Config, Melody
from .mora import MoraError, is_single_mora, split_morae

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "display": {"type": "string"},
        "morae": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["display", "morae"],
    "additionalProperties": False,
}
BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "lines": {"type": "array", "items": LINE_SCHEMA},
    },
    "required": ["source", "keywords", "lines"],
    "additionalProperties": False,
}
LYRICS_SCHEMA = {
    "type": "object",
    "properties": {"blocks": {"type": "array", "items": BLOCK_SCHEMA}},
    "required": ["blocks"],
    "additionalProperties": False,
}


class LyricsError(Exception):
    pass


# ---------------------------------------------------------------- 検証

def _norm(s: str) -> str:
    """比較用の正規化（全角半角の統一、空白除去）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def _numbers(s: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", _norm(s))


def _contains_number(text: str, num: str) -> bool:
    # 「15」の中の「5」や「1.5」の中の「5」には一致させない
    return re.search(rf"(?<!\d)(?<!\d\.){re.escape(num)}(?!\d|\.\d)", text) is not None


def validate_shape(block: dict, phrase_lengths: list[int]) -> list[str]:
    """行数・音数・1モーラ性だけを検証する（音声化できるかどうか）。"""
    errors: list[str] = []
    lines = block.get("lines") or []
    if len(lines) != len(phrase_lengths):
        errors.append(f"行数が{len(lines)}行です（{len(phrase_lengths)}行必要）")
    for i, (line, need) in enumerate(zip(lines, phrase_lengths), 1):
        morae = line.get("morae")
        if not isinstance(morae, list):
            errors.append(f"{i}行目: morae が配列になっていません")
            continue
        bad = [m for m in morae if not is_single_mora(m)]
        for m in bad:
            errors.append(f"{i}行目: morae の要素「{m}」は1モーラのカタカナではありません（小書き文字は前の文字とまとめ、ッ・ン・ーは単独の要素にする）")
        n = len(morae)
        if n != need:
            diff = n - need
            errors.append(f"{i}行目: {n}音です。{need}音必要です（{abs(diff)}音{'多い' if diff > 0 else '少ない'}）")
    return errors


def validate_block(block: dict, phrase_lengths: list[int], original: str | None = None) -> list[str]:
    """ブロックを検証し、問題点を具体的な日本語のリストで返す（空なら合格）。"""
    errors = validate_shape(block, phrase_lengths)
    lines = block.get("lines") or []
    for i, line in enumerate(lines, 1):
        if not str(line.get("display", "")).strip():
            errors.append(f"{i}行目: display が空です")

    display_all = _norm("".join(str(l.get("display", "")) for l in lines))
    keywords = block.get("keywords") or []
    if not keywords:
        errors.append("keywords が空です（覚えるべき数字・用語を1〜2個入れる）")
    for k in keywords:
        nk = _norm(k)
        if not nk:
            continue
        if nk not in display_all:
            errors.append(f"キーワード「{k}」が歌詞の表示（display）にそのまま含まれていません")
        if original is not None and nk not in _norm(original):
            errors.append(f"キーワード「{k}」が原文にありません（原文の表記どおりに。数字・用語を変えていないか確認）")

    source = str(block.get("source", ""))
    norm_source = _norm(source)
    # source は原文の抜き書き。離れた箇所を組み合わせるときは「…」でつなぐ
    if original is not None:
        for part in re.split(r"…+|\.{3,}", source):
            if _norm(part) and _norm(part) not in _norm(original):
                errors.append(f"source の「{part.strip()}」が原文と一致しません（原文から該当箇所をそのまま書き写す）")
    for k in keywords:
        if _norm(k) and _norm(k) not in norm_source:
            errors.append(f"キーワード「{k}」がこのブロックの source にありません（別の事項と混ざらないように）")
    for num in dict.fromkeys(_numbers(source)):
        if not _contains_number(display_all, num):
            errors.append(f"原文の数字「{num}」が歌詞の表示（display）に含まれていません")
    for num in dict.fromkeys(_numbers(display_all)):
        if not _contains_number(norm_source, num):
            errors.append(f"歌詞の表示に、このブロックの source にない数字「{num}」が入っています（別の事項と混ざらないように）")
    return errors


def normalize_block(block: dict) -> None:
    """人が手で直した lyrics.json を受け付けやすくする（morae を文字列で書いても分割する）。"""
    for line in block.get("lines", []):
        if isinstance(line.get("morae"), str):
            try:
                line["morae"] = split_morae(line["morae"])
            except MoraError:
                pass  # 検証でエラーとして報告される


# ---------------------------------------------------------------- AI 呼び出し

def _load_prompt(cfg: Config, name: str) -> str:
    path = cfg.resolve(cfg.prompts_dir) / name
    if not path.is_file():
        raise LyricsError(f"プロンプトファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def _fill(template: str, **values: str) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def _melody_desc(melody: Melody) -> dict[str, str]:
    lengths = melody.phrase_lengths
    return {
        "LINES": str(len(lengths)),
        "PHRASE_LENGTHS": "・".join(map(str, lengths)),
        "PHRASE_TABLE": "\n".join(f"- {i}行目: {n}音" for i, n in enumerate(lengths, 1)),
    }


def make_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LyricsError(
            "環境変数 ANTHROPIC_API_KEY が設定されていません。\n"
            "→ PowerShell で  setx ANTHROPIC_API_KEY \"sk-ant-...\"  を実行し、ターミナルを開き直してください。"
        )
    import anthropic

    return anthropic.Anthropic()


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LyricsError(f"AIの応答がJSONとして読めませんでした: {e}\n→ もう一度実行してください。") from e


def call_json_claude_code(cfg: Config, system: str, user: str, schema: dict) -> dict:
    """Claude Code（claude -p）経由で JSON を得る。claude.ai のサブスクの範囲で動く。"""
    exe = shutil.which("claude")
    if exe is None:
        raise LyricsError(
            "Claude Code（claude コマンド）が見つかりません。\n"
            "→ Claude Code をインストールするか、config.toml の [ai] backend を \"api\" にしてください。"
        )
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--system-prompt", system,
        "--model", cfg.model,
        "--tools", "",  # ファイル操作などのツールは使わせない
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory() as work:  # プロジェクトの設定を読ませないよう空のフォルダで実行
        try:
            proc = subprocess.run(cmd, input=user.encode("utf-8"), capture_output=True, cwd=work, timeout=900,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # GUIから呼んでも黒い窓を出さない
        except subprocess.TimeoutExpired as e:
            raise LyricsError("Claude Code の応答がタイムアウトしました。\n→ 入力の文章を短く分けて、もう一度実行してください。") from e
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise LyricsError(f"Claude Code の実行に失敗しました（終了コード {proc.returncode}）: {err or out}")
    if res.get("is_error"):
        msg = str(res.get("result", ""))
        low = msg.lower()
        if "authenticate" in low or "login" in low:
            raise LyricsError(
                f"Claude Code のログインが切れています（{msg}）。\n"
                "→ ターミナルで  claude auth login  を実行してログインし直してください。"
            )
        if "limit" in low:
            raise LyricsError(f"サブスクの利用上限に達しました（{msg}）。\n→ 上限がリセットされてから、もう一度実行してください。")
        raise LyricsError(f"Claude Code がエラーを返しました: {msg}")
    if isinstance(res.get("structured_output"), dict):
        return res["structured_output"]
    return _parse_json(str(res.get("result", "")))


def call_json(client, cfg: Config, system: str, user: str, schema: dict) -> dict:
    """Claude に JSON（構造化出力）で答えさせる。client が None なら Claude Code 経由。"""
    if client is None:
        return call_json_claude_code(cfg, system, user, schema)
    kwargs = dict(
        model=cfg.model,
        max_tokens=32000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if cfg.fallbacks:
        # 安全性分類で断られた場合にサーバー側で別モデルに切り替えて再実行する
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks=cfg.fallbacks)
    with client.beta.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise LyricsError("AIが歌詞の生成を断りました。入力の文章を見直してください。")
    if msg.stop_reason == "max_tokens":
        raise LyricsError("AIの出力が長すぎて途中で切れました。\n→ 入力の文章を短く分けて実行してください。")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if text is None:
        raise LyricsError("AIの応答に本文がありませんでした。もう一度実行してください。")
    return _parse_json(text)


def _client_for(cfg: Config, client):
    return make_client() if client is None and cfg.backend == "api" else client


def _fix_until_valid(client, cfg: Config, system: str, block: dict, text: str, melody: Melody,
                     log: Callable[[str], None], label: str) -> dict:
    """ブロックを検証し、不合格なら作り直しを依頼する（最大 max_retries 回）。結果に needs_review / errors を付ける。"""
    desc = _melody_desc(melody)
    fix_template = _load_prompt(cfg, "lyrics_fix.md")
    errors = validate_block(block, melody.phrase_lengths, text)
    attempt = 0
    while errors and attempt < cfg.max_retries:
        attempt += 1
        log(f"  {label}: 不合格 → 作り直し（{attempt}/{cfg.max_retries}回目）")
        for e in errors:
            log(f"    - {e}")
        fix_user = _fill(
            fix_template, TEXT=text,
            BLOCK_JSON=json.dumps(block, ensure_ascii=False, indent=2),
            ERRORS="\n".join(f"- {e}" for e in errors), **desc,
        )
        block = call_json(client, cfg, system, fix_user, BLOCK_SCHEMA)
        errors = validate_block(block, melody.phrase_lengths, text)
    block["needs_review"] = bool(errors)
    block["errors"] = errors
    log(f"  {label}: {'要確認（自動修正できませんでした）' if errors else 'OK'}")
    return block


def generate(text: str, melody: Melody, cfg: Config, client=None, log: Callable[[str], None] = print) -> dict:
    """文章から歌詞を生成し、検証・リトライ済みの lyrics データを返す。"""
    client = _client_for(cfg, client)
    desc = _melody_desc(melody)
    system = _fill(_load_prompt(cfg, "lyrics_system.md"), **desc)
    user = _fill(_load_prompt(cfg, "lyrics_user.md"), TEXT=text, **desc)

    log("歌詞を生成しています…")
    result = call_json(client, cfg, system, user, LYRICS_SCHEMA)
    blocks = result.get("blocks") or []
    if not blocks:
        raise LyricsError("AIがブロックを1つも返しませんでした。入力の文章を確認して、もう一度実行してください。")

    for idx, block in enumerate(blocks, 1):
        blocks[idx - 1] = _fix_until_valid(client, cfg, system, block, text, melody, log, f"ブロック{idx}")

    return {
        "melody": melody.name,
        "phrase_lengths": melody.phrase_lengths,
        "input": text,
        "blocks": blocks,
    }


def rewrite_block(block: dict, text: str, melody: Melody, cfg: Config, instruction: str = "",
                  client=None, log: Callable[[str], None] = print) -> dict:
    """人が手直しした1ブロックを、その文面と指示を尊重して AI に整えてもらう（音数合わせ・読み付け）。

    block には手直し後の display / morae（音数が合っていなくてよい）を入れる。
    戻り値は検証・リトライ済みのブロック（needs_review / errors 付き）。
    """
    client = _client_for(cfg, client)
    desc = _melody_desc(melody)
    system = _fill(_load_prompt(cfg, "lyrics_system.md"), **desc)
    current = {k: block[k] for k in ("source", "keywords", "lines") if k in block}
    shape = validate_shape(current, melody.phrase_lengths)
    user = _fill(
        _load_prompt(cfg, "lyrics_edit.md"), TEXT=text,
        INSTRUCTION=instruction.strip() or "（指示なし。手直しした文面を活かして音数を合わせてください）",
        BLOCK_JSON=json.dumps(current, ensure_ascii=False, indent=2),
        ERRORS="\n".join(f"- {e}" for e in shape) or "- （音数は合っています）", **desc,
    )
    log("AIにブロックを直してもらっています…")
    result = call_json(client, cfg, system, user, BLOCK_SCHEMA)
    return _fix_until_valid(client, cfg, system, result, text, melody, log, "直したブロック")


# ---------------------------------------------------------------- 入出力

def save_lyrics(data: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "lyrics.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_lyrics_txt(data, out_dir / "lyrics.txt")


def load_lyrics(out_dir: Path) -> dict:
    path = out_dir / "lyrics.json"
    if not path.is_file():
        raise LyricsError(f"{path} がありません。\n→ 先に  alpsmemo lyrics 入力.txt -o {out_dir}  を実行してください。")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise LyricsError(f"lyrics.json の書式が壊れています（{e.lineno}行目 {e.colno}文字目付近）。\n→ カンマや括弧の閉じ忘れを確認してください。") from e
    for block in data.get("blocks", []):
        normalize_block(block)
    return data


def list_themes(out_root: Path) -> list[dict]:
    """out フォルダにあるテーマ（lyrics.json を持つフォルダ）の一覧を、新しい順に返す。

    各要素: name / dir / updated（lyrics.json の更新時刻）/ blocks / review / audio（音声のあるモードの一覧）/ preview（冒頭の歌詞）
    """
    themes = []
    if not out_root.is_dir():
        return themes
    for d in out_root.iterdir():
        path = d / "lyrics.json"
        if not d.is_dir() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            blocks = data.get("blocks") or []
        except (OSError, ValueError):
            data, blocks = {}, []
        first = ""
        for line in (blocks[0].get("lines") or []) if blocks else []:
            if isinstance(line, dict) and line.get("display"):
                first = str(line["display"])
                break
        themes.append({
            "name": d.name,
            "dir": d,
            "updated": path.stat().st_mtime,
            "blocks": len(blocks),
            "review": sum(1 for b in blocks if isinstance(b, dict) and b.get("needs_review")),
            "audio": [mode for mode in ("sing", "read") if (d / mode / "all.wav").is_file()],
            "preview": first,
            "broken": not data,
        })
    themes.sort(key=lambda t: t["updated"], reverse=True)
    return themes


def write_lyrics_txt(data: dict, path: Path) -> None:
    lengths = data.get("phrase_lengths", [])
    out = [f"メロディ: {data.get('melody', '')}（{'・'.join(map(str, lengths))}音）", ""]
    for i, block in enumerate(data.get("blocks", []), 1):
        mark = "  ★要確認" if block.get("needs_review") else ""
        out.append(f"【ブロック{i}】{mark}")
        out.append(f"原文: {block.get('source', '')}")
        out.append(f"キーワード: {'、'.join(block.get('keywords', []))}")
        for j, line in enumerate(block.get("lines", []), 1):
            morae = line.get("morae", [])
            morae_s = "・".join(morae) if isinstance(morae, list) else str(morae)
            out.append(f"  {j}. {line.get('display', '')}")
            out.append(f"     {morae_s}（{len(morae) if isinstance(morae, list) else '?'}音）")
        for e in block.get("errors", []) if block.get("needs_review") else []:
            out.append(f"  ※ {e}")
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
