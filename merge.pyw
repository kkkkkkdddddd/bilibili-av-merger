import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from collections import defaultdict, deque
from pathlib import Path
from tkinter import filedialog, ttk
from uuid import uuid4

DOWNLOADS = Path(__file__).resolve().parent.parent
BACKGROUND = "#F0EEE7"
PAPER = "#FCFBF7"
INK = "#192B25"
MUTED = "#54665D"
LINE = "#D6DDD3"
GREEN = "#18523E"
FONT = "Microsoft YaHei UI"
MEDIA = {".mp4", ".m4a", ".m4s"}
NO_WINDOW = subprocess.CREATE_NO_WINDOW
BILI_NAME = re.compile(r"^(\d+-\d+)-(\d+)$")


def find_pairs(folder):
    groups = defaultdict(lambda: {"video": [], "audio": []})
    for path in folder.iterdir():
        match = BILI_NAME.fullmatch(path.stem)
        if path.suffix.lower() not in MEDIA or not match or not path.is_file():
            continue
        clip, stream = match.groups()
        is_audio = path.suffix.lower() == ".m4a" or stream.startswith("302")
        groups[clip]["audio" if is_audio else "video"].append(path)
    pairs = [
        (tracks["video"][0], tracks["audio"][0])
        for tracks in groups.values()
        if len(tracks["video"]) == len(tracks["audio"]) == 1
    ]
    return sorted(pairs, key=lambda pair: max(p.stat().st_mtime for p in pair), reverse=True)


def default_output(video):
    output = video.with_name(f"{video.stem}_合并.mp4")
    number = 2
    while output.exists():
        output = video.with_name(f"{video.stem}_合并_{number}.mp4")
        number += 1
    return output


def video_duration(ffmpeg, video):
    probe = Path(ffmpeg).with_name("ffprobe.exe")
    if not probe.is_file():
        probe = shutil.which("ffprobe")
    if not probe:
        return 0.0
    try:
        result = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        )
        return max(0.0, float(result.stdout.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


class MergeJob:
    def __init__(self, ffmpeg, video, audio, output):
        self.ffmpeg = ffmpeg
        self.video = video
        self.audio = audio
        self.output = output
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    def run(self):
        temporary = self.output.with_name(f".合并-{uuid4().hex}.mp4")
        errors = deque(maxlen=9)
        command = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-n", "-loglevel", "error",
            "-nostats", "-progress", "pipe:1",
            "-i", str(self.video), "-i", str(self.audio),
            "-map", "0:V:0", "-map", "1:a:0", "-c", "copy",
            "-movflags", "+faststart", str(temporary),
        ]
        result = ("error", "合并未完成。")
        try:
            duration = video_duration(self.ffmpeg, self.video)
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                )
            with self.process.stdout as stream:
                for line in stream:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us" and duration and value.lstrip("-").isdigit():
                        progress = min(99, max(0, 100 * int(value) / (duration * 1_000_000)))
                        self.events.put(("progress", progress))
                    elif line.strip() and not re.fullmatch(r"\w+=.*", line.strip()):
                        errors.append(line.strip())
            code = self.process.wait()
            with self.lock:
                if self.cancelled.is_set():
                    return
                if code:
                    message = "\n".join(errors) or f"FFmpeg 退出代码 {code}"
                    result = ("error", message)
                else:
                    # On Windows rename fails when the destination already exists.
                    temporary.rename(self.output)
                    result = ("done", self.output)
        except FileExistsError:
            result = ("error", "目标文件已存在，请选择一个新文件名。")
        except OSError as exc:
            result = ("error", str(exc))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                result = ("error", f"清理临时文件失败：{exc}")
            if self.cancelled.is_set() and result[0] != "done":
                result = ("cancelled", None)
            self.events.put(result)


class App:
    def __init__(self, root):
        self.root = root
        self.paths = {key: tk.StringVar() for key in ("video", "audio", "output")}
        self.job = None
        self.close_pending = False
        self.result = None
        self.pairs = []
        self.controls = []
        self.ffmpeg = shutil.which("ffmpeg")
        root.title("音视频合并")
        root.geometry("840x670")
        root.minsize(700, 630)
        root.configure(bg=BACKGROUND)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.configure_style()
        self.build()
        self.scan()

    def configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Track.TEntry", fieldbackground="#FFFFFF", foreground=INK,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=7,
                        insertcolor=INK, font=(FONT, 10))
        style.configure("Pair.TCombobox", padding=5, fieldbackground="#FFFFFF",
                        foreground=INK, bordercolor=LINE, arrowcolor=GREEN)
        style.map("Pair.TCombobox", fieldbackground=[("readonly", "#FFFFFF")],
                  foreground=[("readonly", INK)])
        style.configure("Merge.Horizontal.TProgressbar", troughcolor="#E4E9DE",
                        background=GREEN, lightcolor=GREEN, darkcolor=GREEN,
                        bordercolor="#E4E9DE", thickness=5)

    def label(self, parent, text, size=10, color=INK, bg=PAPER, bold=False, **extra):
        return tk.Label(parent, text=text, bg=bg, fg=color, anchor="w",
                        font=(FONT, size, "bold" if bold else "normal"), **extra)

    def button(self, parent, text, action, primary=False, **extra):
        return tk.Button(parent, text=text, command=action, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=GREEN if primary else LINE,
                         bg=GREEN if primary else PAPER, fg="#FFFFFF" if primary else GREEN,
                         activebackground="#28765B" if primary else "#E6EDDF",
                         activeforeground="#FFFFFF" if primary else GREEN,
                         disabledforeground="#7F8E85", cursor="hand2",
                         font=(FONT, 10, "bold"), padx=13, pady=7, **extra)

    def build(self):
        hero = tk.Frame(self.root, bg=INK, padx=30, pady=20)
        hero.pack(fill="x")
        self.label(hero, "音视频合并", 25, "#FFFFFF", INK, True).pack(anchor="w")
        self.label(hero, "将分开的画面和声音合并为一个 MP4", 10, "#D1DDC9", INK).pack(anchor="w", pady=(6, 0))

        card = tk.Frame(self.root, bg=PAPER, padx=22, pady=16,
                        highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=26, pady=(20, 18))
        card.grid_columnconfigure(1, weight=1)
        self.label(card, "B 站文件配对", bold=True).grid(row=0, column=0, sticky="w", padx=(0, 13))
        self.pair_box = ttk.Combobox(card, state="readonly", style="Pair.TCombobox", font=(FONT, 10))
        self.pair_box.grid(row=0, column=1, sticky="ew", padx=(0, 9))
        self.pair_box.bind("<<ComboboxSelected>>", self.select_pair)
        refresh = self.button(card, "重新扫描", self.scan)
        refresh.grid(row=0, column=2, sticky="ew")
        self.controls.append(refresh)
        tk.Frame(card, bg=LINE, height=1).grid(row=1, columnspan=3, sticky="ew", pady=(13, 9))
        for row, key, title in [(2, "video", "视频文件"), (3, "audio", "音频文件"), (4, "output", "输出文件")]:
            self.label(card, title, bold=True).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(card, textvariable=self.paths[key], style="Track.TEntry", font=(FONT, 10))
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 9), pady=4)
            choose = self.button(card, "另存为…" if key == "output" else "选择…",
                                 lambda field=key: self.browse(field))
            choose.grid(row=row, column=2, sticky="ew", pady=4)
            self.controls.extend((entry, choose))
        self.label(card, "直接复制两条轨道，保留原画质和音质。", 9, MUTED).grid(
            row=5, column=1, sticky="w", pady=(3, 9))

        self.progress = ttk.Progressbar(card, style="Merge.Horizontal.TProgressbar", maximum=100)
        self.progress.grid(row=6, columnspan=3, sticky="ew", pady=(8, 5))
        self.status = self.label(card, "", 10, MUTED, wraplength=560, justify="left")
        self.status.grid(row=7, columnspan=3, sticky="w", pady=(1, 5))
        card.grid_rowconfigure(8, weight=1)
        self.details = tk.Text(card, height=4, wrap="word", relief="flat", bd=0,
                               bg="#F4F2EA", fg=INK, font=(FONT, 9), padx=8, pady=6)
        self.details.grid(row=8, columnspan=3, sticky="nsew", pady=(3, 6))
        self.details.grid_remove()
        actions = tk.Frame(card, bg=PAPER)
        actions.grid(row=9, columnspan=3, sticky="ew", pady=(5, 0))
        self.start_btn = self.button(actions, "开始合并", self.start, primary=True)
        self.start_btn.pack(side="right")
        self.cancel_btn = self.button(actions, "取消合并", self.cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=8)
        self.show_btn = self.button(actions, "打开输出位置", self.show_output, state="disabled")
        self.show_btn.pack(side="left")

    def scan(self):
        try:
            self.pairs = find_pairs(DOWNLOADS)
        except OSError as exc:
            self.pairs = []
            self.show_details(str(exc))
        if self.pairs:
            self.pair_box.configure(values=[video.name for video, _ in self.pairs])
            self.pair_box.current(0)
            self.select_pair()
        else:
            self.pair_box.configure(values=[])
            self.pair_box.set("未找到可自动配对的文件")
            self.status.configure(text="请手动选择对应的视频和音频。")
        if not self.ffmpeg:
            self.status.configure(text="找不到 ffmpeg.exe，请检查系统 PATH 后重新打开工具。")
            self.start_btn.configure(state="disabled")

    def select_pair(self, _event=None):
        index = self.pair_box.current()
        if index < 0:
            return
        video, audio = self.pairs[index]
        self.paths["video"].set(str(video))
        self.paths["audio"].set(str(audio))
        self.paths["output"].set(str(default_output(video)))
        self.status.configure(text=f"已识别 {len(self.pairs)} 组文件，已填入当前配对。")

    def browse(self, field):
        current = self.paths[field].get()
        parent = str(Path(current).parent) if current else str(DOWNLOADS)
        if field == "output":
            chosen = filedialog.asksaveasfilename(
                title="保存合并后的视频", initialdir=parent,
                initialfile=Path(current).name if current else "合并.mp4",
                defaultextension=".mp4", filetypes=[("MP4 视频", "*.mp4")], confirmoverwrite=False,
            )
        else:
            patterns = "*.mp4 *.m4s *.mkv *.mov *.webm" if field == "video" else "*.m4a *.aac *.m4s *.mp4 *.mp3 *.opus *.wav *.flac *.webm"
            chosen = filedialog.askopenfilename(
                title="选择视频" if field == "video" else "选择音频",
                initialdir=parent, filetypes=[("媒体文件", patterns), ("所有文件", "*.*")],
            )
        if not chosen:
            return
        self.paths[field].set(chosen)
        if field == "video":
            self.paths["output"].set(str(default_output(Path(chosen))))

    def show_details(self, message):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", message)
        self.details.configure(state="disabled")
        self.details.grid()

    def start(self):
        self.details.grid_remove()
        try:
            if not all(value.get().strip() for value in self.paths.values()):
                raise ValueError("请选择视频、音频和输出文件。")
            video, audio, output = (
                Path(self.paths[key].get().strip().strip('"')).resolve()
                for key in ("video", "audio", "output")
            )
            if not video.is_file() or not audio.is_file():
                raise ValueError("找不到选定的视频或音频文件。")
            if video == audio or output in (video, audio):
                raise ValueError("视频、音频和输出必须是三个不同的文件。")
            if output.suffix.lower() != ".mp4" or not output.parent.is_dir():
                raise ValueError("请选择现有文件夹，并使用 .mp4 作为输出扩展名。")
            if output.exists():
                raise ValueError("目标文件已存在，请选择一个新文件名。")
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc), fg="#8A3C29")
            return
        self.result = None
        self.show_btn.configure(state="disabled")
        self.set_busy(True)
        self.progress["value"] = 0
        self.status.configure(text="正在合并音视频…", fg=MUTED)
        self.job = MergeJob(self.ffmpeg, video, audio, output)
        threading.Thread(target=self.job.run, daemon=True).start()
        self.root.after(60, self.poll)

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for control in self.controls:
            control.configure(state=state)
        self.pair_box.configure(state="disabled" if busy else "readonly")
        self.start_btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def poll(self):
        while True:
            try:
                event, value = self.job.events.get_nowait()
            except queue.Empty:
                self.root.after(60, self.poll)
                return
            if event == "progress":
                self.progress["value"] = value
                continue
            self.job = None
            if self.close_pending:
                self.root.destroy()
                return
            self.set_busy(False)
            if event == "done":
                self.progress["value"] = 100
                self.status.configure(text=f"已生成 {value.name}", fg=GREEN)
                self.result = value
                self.show_btn.configure(state="normal")
                self.paths["output"].set(str(default_output(Path(self.paths["video"].get().strip().strip('"')))))
            else:
                self.progress["value"] = 0
                if event == "error":
                    self.status.configure(text="合并失败。请检查文件能否播放、轨道是否选对。", fg="#8A3C29")
                    self.show_details(value)
                else:
                    self.status.configure(text="已取消合并。", fg=MUTED)
            return

    def show_output(self):
        if self.result:
            subprocess.Popen(["explorer.exe", "/select,", str(self.result)])

    def cancel(self):
        self.cancel_btn.configure(state="disabled")
        self.status.configure(text="正在取消…", fg=MUTED)
        self.job.cancel()

    def close(self):
        if self.job:
            self.close_pending = True
            self.cancel()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
