"""簡易GUI（tkinter）。

文章を貼る → 歌詞を作る → 確認・手直し → 音声を作る → 歌詞を表示しながら再生。
"""

from __future__ import annotations

import bisect
import json
import queue
import os
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import wave
import winsound
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .cli import _anthropic_message
from .config import PROJECT_ROOT, ConfigError, load_config, load_melody
from .lyrics import LyricsError, generate, list_themes, load_lyrics, rewrite_block, save_lyrics, validate_block, validate_shape
from .mora import LONG_MARK, REST, MoraError, fill_repeat, fill_rest, fill_stretch, reading_from_kana, split_morae
from .render_read import RenderError as ReadRenderError
from .render_sing import RenderError as SingRenderError
from .synth import synthesize
from .voicevox import VoicevoxClient, VoicevoxError

FONT = "Yu Gothic UI"
OUT_ROOT = PROJECT_ROOT / "out"
KNOWN_ERRORS = (ConfigError, LyricsError, VoicevoxError, ReadRenderError, SingRenderError)
MODES = {"歌唱": "sing", "読み上げ": "read"}

PX_PER_BEAT = 64  # 音符の図の横幅（1拍あたり）
STRIP_H = 44  # 音符の図の高さ


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = load_config()
        self.melody = load_melody(self.cfg.resolve(self.cfg.melody)).transposed(self.cfg.transpose)
        self.out_dir: Path | None = None
        self.data: dict | None = None
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.play_events: list[dict] = []
        self.play_times: list[float] = []
        self.play_start = 0.0
        self.play_len = 0.0
        self.play_job = None
        # 編集パネルの状態
        self.editor_block: int | None = None  # 編集中のブロック番号（1始まり）
        self.editor_saved: tuple | None = None  # 保存済みの内容（変更の有無を調べる用）
        self._selecting = False

        root.title("替え歌暗記ツール")
        root.geometry("1280x820")
        try:
            root.state("zoomed")  # 画面いっぱいに開く
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        style = ttk.Style()
        style.configure(".", font=(FONT, 10))
        style.configure("Treeview", rowheight=26)
        style.configure("Big.TButton", font=(FONT, 11, "bold"))
        self._build()
        self.root.after(100, self._poll)
        # 起動時に前面へ出す（他のウィンドウの後ろに隠れないように）
        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ------------------------------------------------------------ 画面
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="テーマ名").pack(side="left")
        self.theme = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.theme, width=30).pack(side="left", padx=6)
        ttk.Button(top, text="作ったもの一覧…", command=self.open_list).pack(side="left", padx=6)
        self.status = tk.StringVar(value="テーマ名を入れ、文章を貼り付けて「歌詞を作る」を押してください。")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="left", padx=12)

        where = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        where.pack(fill="x")
        self.where = tk.StringVar(value=f"保存先：{OUT_ROOT}\\（テーマ名）　歌詞と音声はテーマごとにここへ自動保存されます")
        ttk.Label(where, textvariable=self.where, foreground="#555").pack(side="left")
        ttk.Button(where, text="このフォルダをエクスプローラーで見る", command=self.open_folder).pack(side="left", padx=8)

        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=1)

        # 1. 文章
        f1 = ttk.LabelFrame(left, text="1. 覚えたい文章", padding=6)
        f1.pack(fill="x")
        self.input = tk.Text(f1, height=5, wrap="char", font=(FONT, 11))
        self.input.pack(fill="x")
        self.btn_lyrics = ttk.Button(f1, text="歌詞を作る", style="Big.TButton", command=self.make_lyrics)
        self.btn_lyrics.pack(anchor="e", pady=(6, 0))

        # 2. 歌詞の一覧
        f2 = ttk.LabelFrame(left, text="2. 歌詞の一覧（行をクリックすると右で直せます）", padding=6)
        f2.pack(fill="both", expand=True, pady=6)
        cols = ("blk", "line", "display", "morae", "count")
        self.tree = ttk.Treeview(f2, columns=cols, show="headings", selectmode="browse")
        for c, label, w in zip(cols, ("ブロック", "行", "歌詞", "読み", "音数"), (56, 32, 170, 230, 56)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=c in ("display", "morae"), anchor="w" if c in ("display", "morae") else "center")
        self.tree.tag_configure("review", background="#fff3c4")
        self.tree.tag_configure("bad", foreground="#c00000")
        sb = ttk.Scrollbar(f2, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<Double-1>", lambda e: self.focus_line())

        # 4. 音声
        f4w = ttk.LabelFrame(left, text="4. 音声を作る", padding=6)
        f4w.pack(fill="x")
        f4 = ttk.Frame(f4w)
        f4.pack(fill="x")
        self.mode = tk.StringVar(value="歌唱" if self.cfg.mode == "sing" else "読み上げ")
        self.bpm = tk.IntVar(value=int(self.cfg.bpm))
        self.repeat = tk.IntVar(value=self.cfg.repeat)
        self.click = tk.BooleanVar(value=self.cfg.click)
        ttk.Label(f4, text="モード").pack(side="left")
        ttk.Combobox(f4, textvariable=self.mode, values=list(MODES), width=8, state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(f4, text="テンポ").pack(side="left")
        ttk.Spinbox(f4, from_=60, to=200, increment=5, textvariable=self.bpm, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(f4, text="繰り返し").pack(side="left")
        ttk.Spinbox(f4, from_=1, to=10, textvariable=self.repeat, width=4).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(f4, text="クリック音", variable=self.click).pack(side="left")
        self.btn_synth = ttk.Button(f4w, text="音声を作る", style="Big.TButton", command=self.make_audio)
        self.btn_synth.pack(anchor="e", pady=(6, 0))

        # 3. 編集パネル
        self._build_editor(right)

        # 5. 再生（歌詞表示つき）
        f5 = ttk.LabelFrame(right, text="5. 再生", padding=8)
        f5.pack(fill="both", expand=True, pady=(6, 0))
        pbar = ttk.Frame(f5)
        pbar.pack(fill="x")
        self.play_file = tk.StringVar()
        self.files_box = ttk.Combobox(pbar, textvariable=self.play_file, width=16, state="readonly")
        self.files_box.pack(side="left")
        ttk.Button(pbar, text="▶ 再生", style="Big.TButton", command=self.play).pack(side="left", padx=6)
        ttk.Button(pbar, text="■ 停止", command=self.stop).pack(side="left")
        ttk.Button(pbar, text="選んだブロックを再生", command=self.play_selected_block).pack(side="left", padx=6)
        sbar = ttk.Frame(f5)
        sbar.pack(fill="x", pady=(6, 0))
        ttk.Button(sbar, text="この音声を別の場所にコピー…", command=self.save_audio).pack(side="left")
        self.saved_to: Path | None = None
        self.btn_saved = ttk.Button(sbar, text="コピー先を開く", command=self.open_saved, state="disabled")
        self.btn_saved.pack(side="left", padx=6)
        self.saved_label = tk.StringVar(value="（音声は保存先フォルダにもう入っています。スマホなどに渡すときにコピーしてください）")
        ttk.Label(sbar, textvariable=self.saved_label, foreground="#666", wraplength=560).pack(side="left", padx=6)

        self.k_header = tk.StringVar()
        ttk.Label(f5, textvariable=self.k_header, font=(FONT, 12), foreground="#666").pack(anchor="w", pady=(10, 2))
        self.k_line = tk.StringVar()
        ttk.Label(f5, textvariable=self.k_line, font=(FONT, 26, "bold"), wraplength=560).pack(anchor="w", pady=2)
        self.k_morae = tk.Text(f5, height=2, width=18, font=(FONT, 20), relief="flat", background=self.root.cget("background"),
                               wrap="char", cursor="arrow")
        self.k_morae.tag_configure("sung", foreground="#1565c0")
        self.k_morae.tag_configure("now", foreground="#ffffff", background="#1565c0")
        self.k_morae.tag_configure("rest", foreground="#999999")
        self.k_morae.configure(state="disabled")
        self.k_morae.pack(fill="x", pady=2)
        self.k_next = tk.StringVar()
        ttk.Label(f5, textvariable=self.k_next, font=(FONT, 14), foreground="#888", wraplength=560).pack(anchor="w", pady=(6, 0))

        # ログ
        f6 = ttk.LabelFrame(self.root, text="ログ", padding=4)
        f6.pack(fill="x", padx=10, pady=(0, 8))
        self.log_box = tk.Text(f6, height=5, font=(FONT, 9), state="disabled", wrap="char")
        self.log_box.pack(fill="x")

        self.root.bind("<Control-s>", lambda e: self.save_block())

    def _build_editor(self, parent: ttk.Frame) -> None:
        """3. 選んだブロックを直す（4行まとめて編集。読みを変えると音符の図と音数がその場で変わる）。"""
        f3 = ttk.LabelFrame(parent, text="3. 選んだブロックを直す（Ctrl+S で保存）", padding=6)
        f3.pack(fill="x")
        self.editor_frame = f3

        head = ttk.Frame(f3)
        head.pack(fill="x")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="原文").grid(row=0, column=0, sticky="w")
        self.src_var = tk.StringVar()
        ttk.Entry(head, textvariable=self.src_var, font=(FONT, 10)).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(head, text="キーワード").grid(row=1, column=0, sticky="w")
        self.kw_var = tk.StringVar()
        ttk.Entry(head, textvariable=self.kw_var, font=(FONT, 10)).grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(2, 0))
        ttk.Label(head, text="（「、」区切り。歌詞に必ず入れる数字・用語）", foreground="#666").grid(row=1, column=2, sticky="w", padx=4)
        for v in (self.src_var, self.kw_var):
            v.trace_add("write", lambda *_: self._refresh_dirty())

        self.edit_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
        self.edit_entries: list[tuple[ttk.Entry, ttk.Entry]] = []
        self.count_labels: list[ttk.Label] = []
        self.strips: list[tk.Canvas] = []
        lines = ttk.Frame(f3)
        lines.pack(fill="x", pady=(6, 0))
        lines.columnconfigure(1, weight=2)
        lines.columnconfigure(2, weight=3)
        for li, need in enumerate(self.melody.phrase_lengths):
            r = li * 2
            ttk.Label(lines, text=f"{li + 1}行目\n{need}音", justify="center").grid(row=r, column=0, rowspan=2, padx=(0, 4), sticky="n")
            dv, rv = tk.StringVar(), tk.StringVar()
            e1 = ttk.Entry(lines, textvariable=dv, font=(FONT, 12))
            e1.grid(row=r, column=1, sticky="ew", padx=2, pady=(4, 0))
            e2 = ttk.Entry(lines, textvariable=rv, font=(FONT, 12))
            e2.grid(row=r, column=2, sticky="ew", padx=2, pady=(4, 0))
            cl = ttk.Label(lines, text="", width=9, font=(FONT, 11, "bold"), anchor="center")
            cl.grid(row=r, column=3, padx=2)
            mb = ttk.Menubutton(lines, text="埋める▼", width=9)
            menu = tk.Menu(mb, tearoff=False, font=(FONT, 10))
            menu.add_command(label="伸ばして埋める（ー）", command=lambda i=li: self.fill_line(i, fill_stretch))
            menu.add_command(label="繰り返して埋める", command=lambda i=li: self.fill_line(i, fill_repeat))
            menu.add_command(label="休符で埋める（＿）", command=lambda i=li: self.fill_line(i, fill_rest))
            menu.add_separator()
            menu.add_command(label="読みを自動で付ける（VOICEVOX）", command=lambda i=li: self.auto_reading(i))
            mb.configure(menu=menu)
            mb.grid(row=r, column=4, padx=2)
            strip = tk.Canvas(lines, height=STRIP_H, highlightthickness=0, background="#fafafa")
            strip.grid(row=r + 1, column=1, columnspan=4, sticky="ew", padx=2, pady=(0, 2))
            self.edit_vars.append((dv, rv))
            self.edit_entries.append((e1, e2))
            self.count_labels.append(cl)
            self.strips.append(strip)
            dv.trace_add("write", lambda *_, i=li: self._line_changed(i))
            rv.trace_add("write", lambda *_, i=li: self._line_changed(i))
        ttk.Label(lines, text="歌詞（表示用）", foreground="#666").grid(row=len(self.melody.phrase_lengths) * 2, column=1, sticky="w", padx=2)
        ttk.Label(lines, text="読み（カタカナ。ー＝前の音を伸ばす、＿＝休符）", foreground="#666").grid(
            row=len(self.melody.phrase_lengths) * 2, column=2, columnspan=2, sticky="w", padx=2)

        self.edit_info = tk.StringVar()
        ttk.Label(f3, textvariable=self.edit_info, wraplength=600, justify="left", foreground="#8a6d00").pack(fill="x", pady=(4, 0))

        bar = ttk.Frame(f3)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_save = ttk.Button(bar, text="このブロックを保存", style="Big.TButton", command=self.save_block)
        self.btn_save.pack(side="left")
        ttk.Button(bar, text="元に戻す", command=self.revert_block).pack(side="left", padx=(6, 12))
        self.btn_ai = ttk.Button(bar, text="AIに直してもらう…", command=self.ai_rewrite)
        self.btn_ai.pack(side="left")
        ttk.Button(bar, text="このブロックを削除", command=self.delete_block).pack(side="right")
        ttk.Button(bar, text="確認済みにする", command=self.confirm_block).pack(side="right", padx=6)
        self.set_editor_enabled(False)

    # ------------------------------------------------------------ 共通
    def log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def run_bg(self, label: str, work, done) -> None:
        """時間のかかる処理を裏で実行する（画面を固めない）。"""
        if self.busy:
            return
        self.busy = True
        self.busy_label = label
        self.busy_start = time.monotonic()
        self.status.set(label)
        for b in (self.btn_lyrics, self.btn_synth, self.btn_ai):
            b.configure(state="disabled")

        def target():
            try:
                result = work(lambda m: self.queue.put(("log", m)))
                self.queue.put(("done", done, result))
            except KNOWN_ERRORS as e:
                self.queue.put(("error", str(e)))
            except Exception as e:  # noqa: BLE001 - 画面に出して止まらないようにする
                self.queue.put(("error", _anthropic_message(e) or "".join(traceback.format_exception(e))))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, *rest = self.queue.get_nowait()
                if kind == "log":
                    self.log(rest[0])
                    continue
                self.busy = False
                for b in (self.btn_lyrics, self.btn_synth, self.btn_ai):
                    b.configure(state="normal")
                if kind == "done":
                    self.status.set("")
                    rest[0](rest[1])
                else:
                    self.status.set("エラーが発生しました")
                    self.log("エラー: " + rest[0])
                    messagebox.showerror("エラー", rest[0])
        except queue.Empty:
            pass
        if self.busy:  # 動いていることが分かるよう経過時間を出す
            self.status.set(f"{self.busy_label}　{int(time.monotonic() - self.busy_start)}秒経過")
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ 1. 歌詞を作る
    def make_lyrics(self) -> None:
        text = self.input.get("1.0", "end").strip()
        theme = self.theme.get().strip()
        if not self.ask_discard():
            return
        if not text:
            messagebox.showinfo("文章がありません", "覚えたい文章を「1. 覚えたい文章」に貼り付けてください。")
            return
        if not theme or re.search(r'[\\/:*?"<>|]', theme):
            messagebox.showinfo("テーマ名", "テーマ名を入れてください（\\ / : * ? \" < > | は使えません）。")
            return
        out_dir = OUT_ROOT / theme
        if (out_dir / "lyrics.json").exists() and not messagebox.askyesno(
                "上書きの確認", f"「{theme}」の歌詞はもうあります。作り直して上書きしますか？"):
            return

        def work(log):
            data = generate(text, self.melody, self.cfg, log=log)
            save_lyrics(data, out_dir)
            return out_dir

        self.run_bg("歌詞を作っています…（1〜3分ほどかかります）", work, self.load_dir)

    def open_list(self) -> None:
        """作ったテーマの一覧を出し、選んだものを開く。"""
        themes = list_themes(OUT_ROOT)
        if not themes:
            messagebox.showinfo("まだありません", f"作った歌詞はまだありません。\n（保存先：{OUT_ROOT}）")
            return
        win = tk.Toplevel(self.root)
        win.title("作ったもの一覧")
        win.transient(self.root)
        win.grab_set()
        win.geometry("900x420")
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"保存先：{OUT_ROOT}　（新しい順。ダブルクリックで開く）", foreground="#555").pack(anchor="w")
        cols = ("name", "updated", "blocks", "review", "audio", "preview")
        tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="browse", height=12)
        for c, label, w, anchor in zip(cols, ("テーマ名", "更新日時", "ブロック", "要確認", "音声", "冒頭の歌詞"),
                                       (180, 130, 60, 60, 90, 300), ("w", "center", "center", "center", "center", "w")):
            tree.heading(c, text=label)
            tree.column(c, width=w, anchor=anchor, stretch=c in ("name", "preview"))
        tree.tag_configure("review", background="#fff3c4")
        tree.tag_configure("broken", foreground="#c00000")
        sb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, pady=6)
        by_iid = {}
        for i, t in enumerate(themes):
            audio = "、".join("歌唱" if m == "sing" else "読み上げ" for m in t["audio"]) or "まだ"
            tags = ("broken",) if t["broken"] else ("review",) if t["review"] else ()
            iid = str(i)
            by_iid[iid] = t
            tree.insert("", "end", iid=iid, tags=tags, values=(
                t["name"], time.strftime("%Y/%m/%d %H:%M", time.localtime(t["updated"])), t["blocks"],
                t["review"] or "", audio, t["preview"] if not t["broken"] else "（lyrics.json が壊れています）"))
        tree.selection_set("0")

        def chosen():
            sel = tree.selection()
            return by_iid[sel[0]] if sel else None

        def do_open(*_):
            t = chosen()
            if not t:
                return
            if not self.ask_discard():
                return
            win.destroy()
            try:
                self.load_dir(t["dir"])
            except KNOWN_ERRORS as e:
                messagebox.showerror("開けません", str(e))

        def do_explorer():
            t = chosen()
            if t:
                os.startfile(t["dir"])

        def do_delete():
            t = chosen()
            if not t:
                return
            if not messagebox.askyesno("削除の確認", f"「{t['name']}」の歌詞と音声をすべて削除しますか？\n元に戻せません。", parent=win):
                return
            shutil.rmtree(t["dir"], ignore_errors=True)
            if self.out_dir == t["dir"]:
                self.out_dir = None
                self.data = None
                self.editor_block = None
                self.editor_saved = None
                self.set_editor_enabled(False)
                self.fill_tree_empty()
                self.refresh_files()
                self.where.set(f"保存先：{OUT_ROOT}\\（テーマ名）　歌詞と音声はテーマごとにここへ自動保存されます")
            self.log(f"削除しました: {t['dir']}")
            win.destroy()
            self.open_list()

        tree.bind("<Double-1>", do_open)
        tree.bind("<Return>", do_open)
        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        ttk.Button(bar, text="開く", style="Big.TButton", command=do_open).pack(side="left")
        ttk.Button(bar, text="エクスプローラーで見る", command=do_explorer).pack(side="left", padx=6)
        ttk.Button(bar, text="閉じる", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="削除…", command=do_delete).pack(side="right", padx=6)
        tree.focus_set()

    def fill_tree_empty(self) -> None:
        self._selecting = True
        try:
            self.tree.delete(*self.tree.get_children())
        finally:
            self._selecting = False

    def load_dir(self, out_dir: Path) -> None:
        self.stop()
        self.out_dir = out_dir
        self.where.set(f"保存先：{out_dir}　歌詞と音声はここへ自動保存されます")
        self.data = load_lyrics(out_dir)
        self.theme.set(out_dir.name)
        if self.data.get("input"):
            self.input.delete("1.0", "end")
            self.input.insert("1.0", self.data["input"])
        self.editor_block = None
        self.fill_tree()
        self.refresh_files()
        review = [i for i, b in enumerate(self.data["blocks"], 1) if b.get("needs_review")]
        self.status.set("歌詞を確認してから「音声を作る」を押してください。" + (f"（要確認: ブロック{review}）" if review else ""))
        self.log(f"開きました: {out_dir}")
        if self.data.get("blocks"):
            self.select_row(1, 0)

    # ------------------------------------------------------------ 2. 歌詞の一覧
    def fill_tree(self) -> None:
        self._selecting = True
        try:
            self.tree.delete(*self.tree.get_children())
        finally:
            self._selecting = False
        need = self.melody.phrase_lengths
        for bi, block in enumerate(self.data.get("blocks", []), 1):
            for li, line in enumerate(block.get("lines", [])):
                morae = line.get("morae", [])
                n = len(morae) if isinstance(morae, list) else 0
                want = need[li] if li < len(need) else 0
                tags = []
                if block.get("needs_review"):
                    tags.append("review")
                if n != want:
                    tags.append("bad")
                mark = "★" if block.get("needs_review") and li == 0 else ""
                self.tree.insert("", "end", iid=f"{bi}:{li}", tags=tags, values=(
                    f"{mark}{bi}" if li == 0 else "", li + 1, line.get("display", ""), "・".join(morae), f"{n}/{want}"))

    def _selected(self) -> tuple[int, int] | None:
        sel = self.tree.selection()
        if not sel:
            return None
        b, l = sel[0].split(":")
        return int(b), int(l)

    def select_row(self, bi: int, li: int) -> None:
        iid = f"{bi}:{li}"
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def on_select(self) -> None:
        """一覧で行を選んだら、そのブロックを編集パネルに出す（別ブロックへ移るときは保存を確認）。"""
        if self._selecting:
            return
        s = self._selected()
        if not s or not self.data:
            return
        bi, li = s
        if bi != self.editor_block:
            if not self.ask_discard():
                self._selecting = True
                try:
                    self.select_row(self.editor_block, 0)
                finally:
                    self._selecting = False
                return
            self.load_block(bi)

    def focus_line(self) -> None:
        s = self._selected()
        if s and s[0] == self.editor_block and s[1] < len(self.edit_entries):
            self.edit_entries[s[1]][0].focus_set()

    # ------------------------------------------------------------ 3. ブロックを直す
    def set_editor_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for w in self.editor_frame.winfo_children():
            for c in [w] + w.winfo_children():
                if isinstance(c, (ttk.Entry, ttk.Button, ttk.Menubutton)):
                    c.configure(state=state)
        if not on:
            for (dv, rv) in self.edit_vars:
                dv.set("")
                rv.set("")
            self.src_var.set("")
            self.kw_var.set("")
            self.edit_info.set("")

    def _snapshot(self) -> tuple:
        return (self.src_var.get(), self.kw_var.get(), tuple((d.get(), r.get()) for d, r in self.edit_vars))

    def is_dirty(self) -> bool:
        return self.editor_block is not None and self.editor_saved is not None and self._snapshot() != self.editor_saved

    def _refresh_dirty(self) -> None:
        if self.editor_block is None:
            return
        dirty = self.is_dirty()
        self.editor_frame.configure(text=f"3. ブロック{self.editor_block}を直す（Ctrl+S で保存）" + ("　※未保存の変更あり" if dirty else ""))

    def ask_discard(self) -> bool:
        """未保存の変更があれば保存するか尋ねる。続けてよければ True。"""
        if not self.is_dirty():
            return True
        ans = messagebox.askyesnocancel("未保存の変更", f"ブロック{self.editor_block}の変更を保存しますか？")
        if ans is None:
            return False
        if ans:
            return self.save_block()
        return True

    def load_block(self, bi: int) -> None:
        """ブロックの内容を編集パネルに入れる。"""
        block = self.data["blocks"][bi - 1]
        self.editor_block = None  # 入れている最中は変更扱いにしない
        self.set_editor_enabled(True)
        self.src_var.set(block.get("source", ""))
        self.kw_var.set("、".join(block.get("keywords", [])))
        lines = block.get("lines", [])
        for li, (dv, rv) in enumerate(self.edit_vars):
            line = lines[li] if li < len(lines) else {}
            morae = line.get("morae", [])
            dv.set(line.get("display", ""))
            rv.set("".join(morae) if isinstance(morae, list) else str(morae))
        self.editor_block = bi
        self.editor_saved = self._snapshot()
        self._refresh_dirty()
        self._refresh_info(block)
        for li in range(len(self.edit_vars)):
            self._line_changed(li)

    def _refresh_info(self, block: dict) -> None:
        if block.get("needs_review"):
            self.edit_info.set("★要確認（自動修正できませんでした）" + "".join(f"\n・{e}" for e in block.get("errors", [])))
        else:
            self.edit_info.set("")

    def _line_morae(self, li: int) -> list[str] | None:
        """編集中の読みをモーラに分ける。カタカナ以外が混ざっていれば None。"""
        try:
            return split_morae(self.edit_vars[li][1].get())
        except MoraError:
            return None

    def _line_changed(self, li: int) -> None:
        need = self.melody.phrase_lengths[li]
        m = self._line_morae(li)
        label = self.count_labels[li]
        if m is None:
            label.configure(text="読み？", foreground="#c00000")
        else:
            label.configure(text=f"{len(m)} / {need}", foreground="#2e7d32" if len(m) == need else "#c00000")
        self._draw_strip(li, m)
        self._refresh_dirty()

    def _draw_strip(self, li: int, morae: list[str] | None) -> None:
        """この行のメロディを音符の箱で描き、各箱に読みを載せる（足りない箱は赤の点線、余った音は右に赤）。"""
        cv = self.strips[li]
        cv.delete("all")
        phrase = self.melody.phrases[li]
        keys = [k for p in self.melody.phrases for k, _ in p if k is not None]
        lo, hi = (min(keys), max(keys)) if keys else (0, 1)
        box_h = 18
        morae = morae or []
        x = 4.0
        mi = 0
        for key, beats in phrase:
            w = beats * PX_PER_BEAT
            if key is None:
                cv.create_line(x + 2, STRIP_H - 6, x + w - 2, STRIP_H - 6, fill="#bbb")
                x += w
                continue
            y = 4 + (hi - key) / max(1, hi - lo) * (STRIP_H - box_h - 8)
            mora = morae[mi] if mi < len(morae) else None
            mi += 1
            if mora is None:
                cv.create_rectangle(x + 1, y, x + w - 1, y + box_h, outline="#c00000", dash=(3, 2))
            elif mora == REST:
                cv.create_rectangle(x + 1, y, x + w - 1, y + box_h, outline="#aaa", fill="#eee")
                cv.create_text(x + w / 2, y + box_h / 2, text="休", fill="#999", font=(FONT, 9))
            else:
                hold = mora == LONG_MARK
                cv.create_rectangle(x + 1, y, x + w - 1, y + box_h, outline="#1565c0", fill="#dbe8f8" if hold else "#bcd3f2")
                cv.create_text(x + w / 2, y + box_h / 2, text="ー" if hold else mora, fill="#0d3c78", font=(FONT, 10, "bold"))
            x += w
        extra = morae[mi:]
        if extra:
            x += 8
            cv.create_text(x, STRIP_H / 2, text="余り:", anchor="w", fill="#c00000", font=(FONT, 9))
            x += 34
            for mora in extra:
                cv.create_rectangle(x, (STRIP_H - box_h) / 2, x + 24, (STRIP_H + box_h) / 2, outline="#c00000", fill="#fde0e0")
                cv.create_text(x + 12, STRIP_H / 2, text=mora, fill="#c00000", font=(FONT, 10, "bold"))
                x += 26

    def fill_line(self, li: int, fill) -> None:
        """読みが足りない分を、伸ばす／繰り返す／休符で埋める。"""
        need = self.melody.phrase_lengths[li]
        m = self._line_morae(li)
        if m is None:
            messagebox.showwarning("読みを直してください", f"{li + 1}行目の読みにカタカナ以外の文字があります。", parent=self.root)
            return
        if len(m) > need:
            messagebox.showinfo("音が多すぎます", f"{li + 1}行目は{len(m)}音あります。{need}音以下になるまで消すか、「AIに直してもらう」を使ってください。", parent=self.root)
            return
        if fill is fill_repeat and len([x for x in m if x != REST]) * 2 > need:
            messagebox.showinfo("繰り返せません", f"{li + 1}行目を2回繰り返すと{need}音を超えます。「伸ばして埋める」を使ってください。", parent=self.root)
            return
        self.edit_vars[li][1].set("".join(fill(m, need)))

    def auto_reading(self, li: int) -> None:
        """表示用の歌詞から VOICEVOX に読みを付けてもらう（数字・専門用語の読みは要確認）。"""
        text = self.edit_vars[li][0].get().strip()
        if not text:
            messagebox.showinfo("歌詞がありません", f"{li + 1}行目の歌詞（表示用）を先に入れてください。", parent=self.root)
            return
        try:
            kana = VoicevoxClient(self.cfg.voicevox_url).audio_query(text, self.cfg.speaker_read).get("kana", "")
        except VoicevoxError as e:
            messagebox.showerror("読みを付けられません", str(e), parent=self.root)
            return
        reading = reading_from_kana(kana)
        if not reading:
            messagebox.showinfo("読みを付けられません", "VOICEVOX が読みを返しませんでした。", parent=self.root)
            return
        self.edit_vars[li][1].set(reading)
        self.log(f"{li + 1}行目の読みを VOICEVOX で付けました: {reading}　※数字・専門用語の読みを確認してください")

    def _collect_block(self, strict: bool) -> dict | None:
        """編集パネルの内容をブロックにまとめる。strict なら音数が合わない行があれば None（警告を出す）。"""
        lines = []
        for li, (dv, rv) in enumerate(self.edit_vars):
            need = self.melody.phrase_lengths[li]
            m = self._line_morae(li)
            if m is None:
                if strict:
                    messagebox.showwarning("読みを直してください", f"{li + 1}行目の読みにカタカナ以外の文字があります。", parent=self.root)
                    return None
                m = [rv.get()] if rv.get() else []
            if strict and len(m) != need:
                messagebox.showwarning(
                    "音数が合いません",
                    f"{li + 1}行目が{len(m)}音です（{need}音必要）。\n「埋める▼」で伸ばすか、「AIに直してもらう」で合わせてください。",
                    parent=self.root)
                return None
            lines.append({"display": dv.get().strip(), "morae": m})
        keywords = [k.strip() for k in re.split(r"[、,，/]", self.kw_var.get()) if k.strip()]
        return {"source": self.src_var.get().strip(), "keywords": keywords, "lines": lines}

    def save_block(self) -> bool:
        if self.editor_block is None or not self.data:
            return False
        new = self._collect_block(strict=True)
        if new is None:
            return False
        bi = self.editor_block
        block = self.data["blocks"][bi - 1]
        block.update(new)
        errors = validate_block(block, self.melody.phrase_lengths, self.data.get("input"))
        block["errors"] = errors
        block["needs_review"] = bool(block.get("needs_review")) or bool(errors)
        save_lyrics(self.data, self.out_dir)
        self.editor_saved = self._snapshot()
        self._refresh_dirty()
        self._refresh_info(block)
        sel = self._selected()
        self.fill_tree()
        self.select_row(bi, sel[1] if sel and sel[0] == bi else 0)
        self.log(f"ブロック{bi}を保存しました。音声に反映するには「音声を作る」を押してください。")
        if errors:
            self.log("  ※気になる点: " + " / ".join(errors))
        return True

    def revert_block(self) -> None:
        if self.editor_block is not None:
            self.load_block(self.editor_block)

    def ai_rewrite(self) -> None:
        """手直し中の内容と指示を AI に渡し、音数の合ったブロックに整えてもらう。"""
        if self.editor_block is None or not self.data:
            messagebox.showinfo("ブロックを選んでください", "一覧でブロックの行をクリックしてから押してください。")
            return
        block = self._collect_block(strict=False)
        bi = self.editor_block

        win = tk.Toplevel(self.root)
        win.title(f"ブロック{bi}をAIに直してもらう")
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left", text=(
            "今の歌詞・読み（音数が合っていなくてもOK）を元に、AIが音数を合わせて作り直します。\n"
            "足りない音は休符ではなく、キーワードの繰り返しや伸ばし（ー）で埋めます。\n"
            "どう直してほしいか、ひとこと書けます（空のままでもOK）。例：「3行目を短く」「『養生』を行の頭に」")).pack(anchor="w")
        box = tk.Text(frm, height=3, width=60, font=(FONT, 11))
        box.pack(fill="x", pady=8)
        box.focus_set()

        def go():
            instruction = box.get("1.0", "end").strip()
            win.destroy()

            def work(log):
                return rewrite_block(block, self.data.get("input", ""), self.melody, self.cfg, instruction, log=log)

            def done(result):
                if self.editor_block != bi:  # 待っている間に別のブロックへ移っていた
                    if not self.ask_discard():
                        self.log(f"ブロック{bi}のAIの結果は使いませんでした（別のブロックを編集中）。")
                        return
                    self.load_block(bi)
                self.editor_block = None
                for li, (dv, rv) in enumerate(self.edit_vars):
                    line = result["lines"][li] if li < len(result.get("lines", [])) else {}
                    dv.set(line.get("display", ""))
                    rv.set("".join(line.get("morae", [])))
                self.src_var.set(result.get("source", ""))
                self.kw_var.set("、".join(result.get("keywords", [])))
                self.editor_block = bi
                self._refresh_dirty()
                for li in range(len(self.edit_vars)):
                    self._line_changed(li)
                if result.get("errors"):
                    self.edit_info.set("AIの結果で気になる点:" + "".join(f"\n・{e}" for e in result["errors"]))
                else:
                    self.edit_info.set("")
                self.status.set("AIが直しました。内容を確認して「このブロックを保存」を押してください。")
                self.log(f"ブロック{bi}をAIが直しました（まだ保存していません）。")

            self.run_bg("AIに直してもらっています…（1分ほどかかります）", work, done)

        bar = ttk.Frame(frm)
        bar.pack(anchor="e")
        ttk.Button(bar, text="直してもらう", style="Big.TButton", command=go).pack(side="left", padx=4)
        ttk.Button(bar, text="キャンセル", command=win.destroy).pack(side="left")
        win.bind("<Control-Return>", lambda e: go())

    def confirm_block(self) -> None:
        if self.editor_block is None:
            messagebox.showinfo("ブロックを選んでください", "確認済みにするブロックの行をクリックしてから押してください。")
            return
        if self.is_dirty() and not self.save_block():
            return
        bi = self.editor_block
        block = self.data["blocks"][bi - 1]
        shape = validate_shape(block, self.melody.phrase_lengths)
        if shape:
            messagebox.showwarning("音数が合いません", "音数が合っていない行があります。先に直してください。\n" + "\n".join(shape))
            return
        block["needs_review"] = False
        save_lyrics(self.data, self.out_dir)
        self._refresh_info(block)
        self.fill_tree()
        self.select_row(bi, 0)
        self.log(f"ブロック{bi}を確認済みにしました。")

    def delete_block(self) -> None:
        if self.editor_block is None:
            messagebox.showinfo("ブロックを選んでください", "削除するブロックの行をクリックしてから押してください。")
            return
        bi = self.editor_block
        block = self.data["blocks"][bi - 1]
        first = block["lines"][0].get("display", "") if block.get("lines") else ""
        if len(self.data["blocks"]) <= 1:
            messagebox.showinfo("削除できません", "最後の1ブロックは削除できません。")
            return
        if not messagebox.askyesno("ブロックの削除", f"ブロック{bi}（{first}…）を削除しますか？\n元に戻せません。"):
            return
        del self.data["blocks"][bi - 1]
        save_lyrics(self.data, self.out_dir)
        self.editor_block = None
        self.editor_saved = None
        self.set_editor_enabled(False)
        self.fill_tree()
        self.select_row(min(bi, len(self.data["blocks"])), 0)
        self.log(f"ブロック{bi}を削除しました。音声に反映するには「音声を作る」を押してください。")

    def open_folder(self) -> None:
        target = self.out_dir if self.out_dir and self.out_dir.exists() else OUT_ROOT
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    # ------------------------------------------------------------ 4. 音声を作る
    def make_audio(self) -> None:
        if not self.out_dir:
            messagebox.showinfo("歌詞がありません", "先に「歌詞を作る」か「前に作ったものを開く…」をしてください。")
            return
        if not self.ask_discard():
            return
        review = [i for i, b in enumerate(self.data["blocks"], 1) if b.get("needs_review")]
        if review and not messagebox.askyesno(
                "要確認のブロックがあります",
                f"ブロック {review} が要確認のままです。数字・用語・読みを確認しましたか？\nこのまま音声を作りますか？"):
            return
        mode = MODES[self.mode.get()]
        bpm, repeat, click = float(self.bpm.get()), int(self.repeat.get()), bool(self.click.get())
        self.stop()

        def work(log):
            return synthesize(self.out_dir, self.cfg, self.melody, mode=mode, bpm=bpm, repeat=repeat, click=click, log=log)

        def done(result):
            for i, errs in result.skipped:
                self.log(f"ブロック{i}は音数が合わないためスキップしました: " + " / ".join(errs))
            self.log(f"音声を作りました: {result.out_dir}")
            self.refresh_files(prefer=f"all_repeat{repeat}.wav" if repeat > 1 else "all.wav")
            self.status.set("「▶ 再生」で歌詞を表示しながら再生できます。")

        self.run_bg("音声を作っています…", work, done)

    # ------------------------------------------------------------ 5. 再生
    def _mode_dir(self) -> Path | None:
        return self.out_dir / MODES[self.mode.get()] if self.out_dir else None

    def refresh_files(self, prefer: str | None = None) -> None:
        d = self._mode_dir()
        files = sorted((p.name for p in d.glob("*.wav")), key=lambda n: (not n.startswith("all"), n)) if d and d.exists() else []
        self.files_box.configure(values=files)
        self.play_file.set(prefer if prefer in files else (files[0] if files else ""))

    def play_selected_block(self) -> None:
        bi = self.editor_block
        if bi is None:
            messagebox.showinfo("ブロックを選んでください", "再生したいブロックの行をクリックしてから押してください。")
            return
        name = f"block_{bi:02d}.wav"
        if name not in self.files_box.cget("values"):
            messagebox.showinfo("音声がありません", "先に「音声を作る」を押してください。")
            return
        self.play_file.set(name)
        self.play()

    def save_audio(self) -> None:
        d = self._mode_dir()
        name = self.play_file.get()
        if not d or not name or not (d / name).exists():
            messagebox.showinfo("音声がありません", "先に「音声を作る」を押してください。")
            return
        if self._audio_is_old(d) and not messagebox.askyesno(
                "音声が古いかもしれません", "歌詞を直した後、音声を作り直していません。\nこのまま保存しますか？"):
            return
        suffix = "" if name == "all.wav" else "_" + Path(name).stem
        default = f"{self.out_dir.name}_{self.mode.get()}{suffix}.wav"
        music = Path.home() / "Music"
        dest = filedialog.asksaveasfilename(
            title="音声を保存", initialfile=default, initialdir=music if music.exists() else Path.home(),
            defaultextension=".wav", filetypes=[("WAV 音声", "*.wav")])
        if dest:
            shutil.copyfile(d / name, dest)
            self.saved_to = Path(dest)
            self.btn_saved.configure(state="normal")
            self.saved_label.set(f"コピーしました: {dest}")
            self.log(f"音声をコピーしました: {dest}")
            self.status.set(f"コピーしました: {dest}")

    def open_saved(self) -> None:
        """「別の場所にコピー」した音声の場所をエクスプローラーで開く（ファイルを選択した状態で）。"""
        if self.saved_to and self.saved_to.exists():
            subprocess.Popen(["explorer", "/select,", str(self.saved_to)])
        elif self.saved_to:
            os.startfile(self.saved_to.parent)

    def _audio_is_old(self, d: Path) -> bool:
        """歌詞（lyrics.json）が音声より新しい＝直した後に作り直していない。"""
        try:
            return (self.out_dir / "lyrics.json").stat().st_mtime > (d / "timing.json").stat().st_mtime
        except OSError:
            return False

    def play(self) -> None:
        d = self._mode_dir()
        name = self.play_file.get()
        if not d or not name:
            messagebox.showinfo("音声がありません", "先に「音声を作る」を押してください。")
            return
        if self._audio_is_old(d) and not messagebox.askyesno(
                "音声が古いかもしれません",
                "歌詞を直した後、音声を作り直していません。\n表示される歌詞と音声がずれることがあります。このまま再生しますか？"):
            return
        path = d / name
        try:
            timing = json.loads((d / "timing.json").read_text(encoding="utf-8"))["files"].get(name, [])
        except (OSError, ValueError, KeyError):
            timing = []
        with wave.open(str(path)) as wf:
            self.play_len = wf.getnframes() / wf.getframerate()
        self.stop()
        self.data = load_lyrics(self.out_dir)  # 手で直した lyrics.json も表示に反映する
        self.play_events = timing
        self.play_times = [e["t"] for e in timing]
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        self.play_start = time.monotonic()
        self._tick()

    def stop(self) -> None:
        if self.play_job:
            self.root.after_cancel(self.play_job)
            self.play_job = None
        winsound.PlaySound(None, 0)
        self._show(None)

    def _tick(self) -> None:
        now = time.monotonic() - self.play_start
        if now > self.play_len + 0.2:
            self.play_job = None
            self._show(None)
            return
        i = bisect.bisect_right(self.play_times, now) - 1
        if i < 0:  # 歌い出し前：最初の行を予告表示
            first = self.play_events[0] if self.play_events else None
            self._show(first, sung=False)
        else:
            self._show(self.play_events[i])
        self.play_job = self.root.after(30, self._tick)

    def _show(self, ev: dict | None, sung: bool = True) -> None:
        """歌詞表示を更新する。ev=None で消去、line=None はブロック間の休み。"""
        blocks = (self.data or {}).get("blocks", [])
        self.k_morae.configure(state="normal")
        self.k_morae.delete("1.0", "end")
        if ev is None or not (0 < ev["block"] <= len(blocks)):
            self.k_header.set("")
            self.k_line.set("")
            self.k_next.set("")
            self.k_morae.configure(state="disabled")
            return
        bi = ev["block"]
        block = blocks[bi - 1]
        self.k_header.set(f"ブロック{bi}　キーワード：{'、'.join(block.get('keywords', []))}")
        if ev["line"] is None:  # ブロック間
            nxt = self.play_events[self.play_events.index(ev) + 1] if ev in self.play_events[:-1] else None
            self.k_line.set("♪")
            self.k_next.set(self._line_text(blocks, nxt["block"], 0) if nxt else "")
            self.k_morae.configure(state="disabled")
            return
        li, mi = ev["line"], ev["mora"]
        lines = block.get("lines", [])
        if li >= len(lines):
            self.k_morae.configure(state="disabled")
            return
        line = lines[li]
        self.k_line.set(line.get("display", ""))
        for k, m in enumerate(line.get("morae", [])):
            tag = "rest"
            if m == REST:
                pass  # 休符は灰色のまま
            elif sung and k < mi:
                tag = "sung"
            elif sung and k == mi:
                tag = "now"
            self.k_morae.insert("end", m, tag)
            self.k_morae.insert("end", " ")
        self.k_morae.configure(state="disabled")
        if li + 1 < len(lines):
            self.k_next.set("次：" + lines[li + 1].get("display", ""))
        else:
            self.k_next.set("")

    @staticmethod
    def _line_text(blocks: list, bi: int, li: int) -> str:
        try:
            return "次：" + blocks[bi - 1]["lines"][li].get("display", "")
        except (IndexError, KeyError):
            return ""

    def on_close(self) -> None:
        if not self.ask_discard():
            return
        self.stop()
        self.root.destroy()


def main() -> int:
    try:  # 高DPI画面で文字がにじまないようにする
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        App(root)
    except KNOWN_ERRORS as e:
        root.withdraw()
        messagebox.showerror("起動できません", str(e))
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
