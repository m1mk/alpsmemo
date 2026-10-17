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
from .lyrics import LyricsError, generate, load_lyrics, save_lyrics, validate_block, validate_shape
from .mora import REST, MoraError, split_morae
from .render_read import RenderError as ReadRenderError
from .render_sing import RenderError as SingRenderError
from .synth import synthesize
from .voicevox import VoicevoxError

FONT = "Yu Gothic UI"
OUT_ROOT = PROJECT_ROOT / "out"
KNOWN_ERRORS = (ConfigError, LyricsError, VoicevoxError, ReadRenderError, SingRenderError)
MODES = {"歌唱": "sing", "読み上げ": "read"}


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
        ttk.Button(top, text="前に作ったものを開く…", command=self.open_dir).pack(side="left", padx=6)
        self.status = tk.StringVar(value="テーマ名を入れ、文章を貼り付けて「歌詞を作る」を押してください。")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="left", padx=12)

        where = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        where.pack(fill="x")
        self.where = tk.StringVar(value=f"保存先：{OUT_ROOT / '（テーマ名）'}　※歌詞も音声も自動で保存されます")
        ttk.Button(where, text="保存先フォルダを開く", command=self.open_folder).pack(side="left")
        ttk.Label(where, textvariable=self.where, foreground="#555").pack(side="left", padx=8)

        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=2)

        # 1. 文章
        f1 = ttk.LabelFrame(left, text="1. 覚えたい文章", padding=6)
        f1.pack(fill="x")
        self.input = tk.Text(f1, height=6, wrap="char", font=(FONT, 11))
        self.input.pack(fill="x")
        self.btn_lyrics = ttk.Button(f1, text="歌詞を作る", style="Big.TButton", command=self.make_lyrics)
        self.btn_lyrics.pack(anchor="e", pady=(6, 0))

        # 2. 歌詞の確認
        f2 = ttk.LabelFrame(left, text="2. 歌詞の確認（数字・用語・読みを確認。ダブルクリックで直す）", padding=6)
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
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_line())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.detail = tk.StringVar()
        ttk.Label(f2, textvariable=self.detail, wraplength=700, justify="left").pack(fill="x", pady=4)
        bar = ttk.Frame(f2)
        bar.pack(fill="x")
        ttk.Button(bar, text="選んだ行を直す", command=self.edit_line).pack(side="left")
        ttk.Button(bar, text="このブロックを確認済みにする", command=self.confirm_block).pack(side="left", padx=6)
        ttk.Button(bar, text="このブロックを削除", command=self.delete_block).pack(side="left")

        # 3. 音声
        f3w = ttk.LabelFrame(left, text="3. 音声を作る", padding=6)
        f3w.pack(fill="x")
        f3 = ttk.Frame(f3w)
        f3.pack(fill="x")
        self.mode = tk.StringVar(value="歌唱" if self.cfg.mode == "sing" else "読み上げ")
        self.bpm = tk.IntVar(value=int(self.cfg.bpm))
        self.repeat = tk.IntVar(value=self.cfg.repeat)
        self.click = tk.BooleanVar(value=self.cfg.click)
        ttk.Label(f3, text="モード").pack(side="left")
        ttk.Combobox(f3, textvariable=self.mode, values=list(MODES), width=8, state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(f3, text="テンポ").pack(side="left")
        ttk.Spinbox(f3, from_=60, to=200, increment=5, textvariable=self.bpm, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(f3, text="繰り返し").pack(side="left")
        ttk.Spinbox(f3, from_=1, to=10, textvariable=self.repeat, width=4).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(f3, text="クリック音", variable=self.click).pack(side="left")
        self.btn_synth = ttk.Button(f3w, text="音声を作る", style="Big.TButton", command=self.make_audio)
        self.btn_synth.pack(anchor="e", pady=(6, 0))

        # 4. 再生（歌詞表示つき）
        f4 = ttk.LabelFrame(right, text="4. 再生", padding=8)
        f4.pack(fill="both", expand=True)
        pbar = ttk.Frame(f4)
        pbar.pack(fill="x")
        self.play_file = tk.StringVar()
        self.files_box = ttk.Combobox(pbar, textvariable=self.play_file, width=16, state="readonly")
        self.files_box.pack(side="left")
        ttk.Button(pbar, text="▶ 再生", style="Big.TButton", command=self.play).pack(side="left", padx=6)
        ttk.Button(pbar, text="■ 停止", command=self.stop).pack(side="left")
        ttk.Button(pbar, text="選んだブロックを再生", command=self.play_selected_block).pack(side="left", padx=6)
        sbar = ttk.Frame(f4)
        sbar.pack(fill="x", pady=(6, 0))
        ttk.Button(sbar, text="この音声を保存…", command=self.save_audio).pack(side="left")
        ttk.Label(sbar, text="（左の一覧で選んでいる音声を、好きな場所に .wav で保存）", foreground="#666").pack(side="left", padx=6)

        self.k_header = tk.StringVar()
        ttk.Label(f4, textvariable=self.k_header, font=(FONT, 12), foreground="#666").pack(anchor="w", pady=(16, 4))
        self.k_line = tk.StringVar()
        ttk.Label(f4, textvariable=self.k_line, font=(FONT, 30, "bold"), wraplength=440).pack(anchor="w", pady=4)
        self.k_morae = tk.Text(f4, height=2, width=18, font=(FONT, 22), relief="flat", background=self.root.cget("background"),
                               wrap="char", cursor="arrow")
        self.k_morae.tag_configure("sung", foreground="#1565c0")
        self.k_morae.tag_configure("now", foreground="#ffffff", background="#1565c0")
        self.k_morae.tag_configure("rest", foreground="#999999")
        self.k_morae.configure(state="disabled")
        self.k_morae.pack(fill="x", pady=4)
        self.k_next = tk.StringVar()
        ttk.Label(f4, textvariable=self.k_next, font=(FONT, 16), foreground="#888", wraplength=440).pack(anchor="w", pady=(12, 0))

        # ログ
        f5 = ttk.LabelFrame(self.root, text="ログ", padding=4)
        f5.pack(fill="x", padx=10, pady=(0, 8))
        self.log_box = tk.Text(f5, height=6, font=(FONT, 9), state="disabled", wrap="char")
        self.log_box.pack(fill="x")

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
        for b in (self.btn_lyrics, self.btn_synth):
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
                for b in (self.btn_lyrics, self.btn_synth):
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

    def open_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=OUT_ROOT if OUT_ROOT.exists() else PROJECT_ROOT, title="テーマのフォルダを選ぶ")
        if d:
            try:
                self.load_dir(Path(d))
            except KNOWN_ERRORS as e:
                messagebox.showerror("開けません", str(e))

    def load_dir(self, out_dir: Path) -> None:
        self.stop()
        self.out_dir = out_dir
        self.where.set(f"保存先：{out_dir}（自動保存）")
        self.data = load_lyrics(out_dir)
        self.theme.set(out_dir.name)
        if self.data.get("input"):
            self.input.delete("1.0", "end")
            self.input.insert("1.0", self.data["input"])
        self.fill_tree()
        self.refresh_files()
        review = [i for i, b in enumerate(self.data["blocks"], 1) if b.get("needs_review")]
        self.status.set("歌詞を確認してから「音声を作る」を押してください。" + (f"（要確認: ブロック{review}）" if review else ""))
        self.log(f"開きました: {out_dir}")

    # ------------------------------------------------------------ 2. 歌詞の確認
    def fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
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

    def show_detail(self) -> None:
        s = self._selected()
        if not s or not self.data:
            return
        block = self.data["blocks"][s[0] - 1]
        text = f"原文: {block.get('source', '')}\nキーワード: {'、'.join(block.get('keywords', []))}"
        if block.get("needs_review"):
            text += "\n★要確認" + "".join(f"\n・{e}" for e in block.get("errors", []))
        self.detail.set(text)

    def edit_line(self) -> None:
        s = self._selected()
        if not s:
            messagebox.showinfo("行を選んでください", "直したい行をクリックしてから押してください。")
            return
        bi, li = s
        block = self.data["blocks"][bi - 1]
        line = block["lines"][li]
        need = self.melody.phrase_lengths[li]

        win = tk.Toplevel(self.root)
        win.title(f"ブロック{bi} {li + 1}行目を直す")
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both")
        ttk.Label(frm, text="歌詞（表示用）").grid(row=0, column=0, sticky="w")
        disp = tk.StringVar(value=line.get("display", ""))
        ttk.Entry(frm, textvariable=disp, width=40, font=(FONT, 12)).grid(row=0, column=1, pady=4)
        ttk.Label(frm, text="読み（カタカナ）").grid(row=1, column=0, sticky="w")
        reading = tk.StringVar(value="".join(line.get("morae", [])))
        ttk.Entry(frm, textvariable=reading, width=40, font=(FONT, 12)).grid(row=1, column=1, pady=4)
        info = tk.StringVar()
        info_label = ttk.Label(frm, textvariable=info, font=(FONT, 11))
        info_label.grid(row=2, column=1, sticky="w")
        ttk.Label(frm, foreground="#555", justify="left", text=(
            f"この行は{need}音で歌います。消したい部分は読みから消して、「足りない分を休符で埋める」を押してください。\n"
            "休符（＿）の音は歌わずに休みます。行をまるごと消すときは、読みを空にしてから同じボタンを押します。"
        )).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def check(*_):
            try:
                m = split_morae(reading.get())
            except MoraError as e:
                info.set(str(e))
                info_label.configure(foreground="#c00000")
                return None
            ok = len(m) == need
            info.set(f"{'・'.join(m)}　（{len(m)}音 / {need}音必要）")
            info_label.configure(foreground="#2e7d32" if ok else "#c00000")
            return m if ok else None

        reading.trace_add("write", check)
        check()

        def save():
            m = check()
            if m is None:
                messagebox.showwarning("音数が合いません", f"読みを{need}音にしてください。", parent=win)
                return
            line["display"] = disp.get().strip()
            line["morae"] = m
            errors = validate_block(block, self.melody.phrase_lengths, self.data.get("input"))
            block["errors"] = errors
            block["needs_review"] = bool(block.get("needs_review")) or bool(errors)
            save_lyrics(self.data, self.out_dir)
            self.fill_tree()
            self.tree.selection_set(f"{bi}:{li}")
            self.log(f"ブロック{bi} {li + 1}行目を直しました。音声に反映するには「音声を作る」を押してください。")
            win.destroy()

        def pad_rests():
            try:
                n = len(split_morae(reading.get()))
            except MoraError as e:
                messagebox.showwarning("読みを直してください", str(e), parent=win)
                return
            if n > need:
                messagebox.showinfo("音が多すぎます", f"読みが{n}音あります。{need}音以下になるまで消してください。", parent=win)
                return
            reading.set(reading.get().rstrip() + REST * (need - n))

        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="足りない分を休符で埋める", command=pad_rests).pack(side="left", padx=4)
        ttk.Button(bar, text="この行を保存", command=save).pack(side="left", padx=4)
        ttk.Button(bar, text="キャンセル", command=win.destroy).pack(side="left")

    def confirm_block(self) -> None:
        s = self._selected()
        if not s:
            messagebox.showinfo("行を選んでください", "確認済みにするブロックの行をクリックしてから押してください。")
            return
        block = self.data["blocks"][s[0] - 1]
        shape = validate_shape(block, self.melody.phrase_lengths)
        if shape:
            messagebox.showwarning("音数が合いません", "音数が合っていない行があります。先に直してください。\n" + "\n".join(shape))
            return
        block["needs_review"] = False
        save_lyrics(self.data, self.out_dir)
        self.fill_tree()
        self.tree.selection_set(f"{s[0]}:{s[1]}")
        self.log(f"ブロック{s[0]}を確認済みにしました。")

    def delete_block(self) -> None:
        s = self._selected()
        if not s:
            messagebox.showinfo("行を選んでください", "削除するブロックの行をクリックしてから押してください。")
            return
        bi = s[0]
        block = self.data["blocks"][bi - 1]
        first = block["lines"][0].get("display", "") if block.get("lines") else ""
        if len(self.data["blocks"]) <= 1:
            messagebox.showinfo("削除できません", "最後の1ブロックは削除できません。")
            return
        if not messagebox.askyesno("ブロックの削除", f"ブロック{bi}（{first}…）を削除しますか？\n元に戻せません。"):
            return
        del self.data["blocks"][bi - 1]
        save_lyrics(self.data, self.out_dir)
        self.fill_tree()
        self.log(f"ブロック{bi}を削除しました。音声に反映するには「音声を作る」を押してください。")

    def open_folder(self) -> None:
        target = self.out_dir if self.out_dir and self.out_dir.exists() else OUT_ROOT
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    # ------------------------------------------------------------ 3. 音声を作る
    def make_audio(self) -> None:
        if not self.out_dir:
            messagebox.showinfo("歌詞がありません", "先に「歌詞を作る」か「前に作ったものを開く…」をしてください。")
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

    # ------------------------------------------------------------ 4. 再生
    def _mode_dir(self) -> Path | None:
        return self.out_dir / MODES[self.mode.get()] if self.out_dir else None

    def refresh_files(self, prefer: str | None = None) -> None:
        d = self._mode_dir()
        files = sorted((p.name for p in d.glob("*.wav")), key=lambda n: (not n.startswith("all"), n)) if d and d.exists() else []
        self.files_box.configure(values=files)
        self.play_file.set(prefer if prefer in files else (files[0] if files else ""))

    def play_selected_block(self) -> None:
        s = self._selected()
        if not s:
            messagebox.showinfo("行を選んでください", "再生したいブロックの行をクリックしてから押してください。")
            return
        name = f"block_{s[0]:02d}.wav"
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
            self.log(f"音声を保存しました: {dest}")
            self.status.set(f"保存しました: {dest}")

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
        self.data = load_lyrics(self.out_dir)
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
