from __future__ import annotations

import math
import queue
import random
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import tkinter as tk
from tkinter import messagebox, ttk


@dataclass(slots=True)
class SweepRecord:
    timestamp: str
    hz_low: int
    hz_high: int
    hz_bin_width: float
    num_samples: int
    power_db: list[float]


@dataclass(slots=True)
class SignalCandidate:
    start_hz: float
    end_hz: float
    center_hz: float
    bandwidth_hz: float
    peak_db: float
    average_db: float
    snr_db: float
    score: float
    label: str


@dataclass(slots=True)
class SpectrumSnapshot:
    timestamp: str
    frequencies_hz: list[float]
    power_db: list[float]
    candidates: list[SignalCandidate]
    sweep_count: int
    source: str


class SweepParser:
    @staticmethod
    def parse_csv_line(line: str) -> SweepRecord:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            raise ValueError(f"Malformed sweep line: {line!r}")
        timestamp = f"{parts[0]} {parts[1]}"
        hz_low = int(parts[2])
        hz_high = int(parts[3])
        hz_bin_width = float(parts[4])
        num_samples = int(parts[5])
        power_db = [float(value) for value in parts[6:] if value]
        return SweepRecord(timestamp, hz_low, hz_high, hz_bin_width, num_samples, power_db)


class SignalDetector:
    def __init__(self, threshold_db: float = 7.0, min_bins: int = 3, max_noise_span_db: float = 14.0) -> None:
        self.threshold_db = threshold_db
        self.min_bins = min_bins
        self.max_noise_span_db = max_noise_span_db

    def detect(self, frequencies_hz: list[float], power_db: list[float]) -> list[SignalCandidate]:
        if len(frequencies_hz) != len(power_db) or len(power_db) < self.min_bins:
            return []

        smoothed = self._moving_average(power_db, 3)
        noise_floor = statistics.median(smoothed)
        active = [value > noise_floor + self.threshold_db for value in smoothed]

        candidates: list[SignalCandidate] = []
        idx = 0
        while idx < len(active):
            if not active[idx]:
                idx += 1
                continue

            start = idx
            while idx < len(active) and active[idx]:
                idx += 1
            end = idx - 1
            if end - start + 1 < self.min_bins:
                continue

            segment = smoothed[start : end + 1]
            outer = self._outer_window(smoothed, start, end, width=3)
            outer_mean = statistics.mean(outer) if outer else noise_floor
            segment_mean = statistics.mean(segment)
            peak = max(segment)
            bandwidth_hz = max(frequencies_hz[end] - frequencies_hz[start], 0.0)
            center_hz = (frequencies_hz[start] + frequencies_hz[end]) / 2.0
            edge_contrast = segment_mean - outer_mean
            flatness = max(0.0, 1.0 - min(statistics.pstdev(segment), self.max_noise_span_db) / self.max_noise_span_db)
            prominence = max(0.0, segment_mean - noise_floor)

            score = min(
                1.0,
                0.45 * min(prominence / 20.0, 1.0)
                + 0.35 * min(edge_contrast / 18.0, 1.0)
                + 0.20 * flatness,
            )
            label = self._classify_candidate(bandwidth_hz, prominence, flatness)
            candidates.append(
                SignalCandidate(
                    start_hz=frequencies_hz[start],
                    end_hz=frequencies_hz[end],
                    center_hz=center_hz,
                    bandwidth_hz=bandwidth_hz,
                    peak_db=peak,
                    average_db=segment_mean,
                    snr_db=prominence,
                    score=score,
                    label=label,
                )
            )

        candidates.sort(key=lambda candidate: (candidate.score, candidate.peak_db), reverse=True)
        return candidates[:12]

    @staticmethod
    def _moving_average(values: list[float], width: int) -> list[float]:
        if width <= 1:
            return values[:]
        half_width = width // 2
        smoothed: list[float] = []
        for idx in range(len(values)):
            start = max(0, idx - half_width)
            end = min(len(values), idx + half_width + 1)
            smoothed.append(statistics.mean(values[start:end]))
        return smoothed

    @staticmethod
    def _outer_window(values: list[float], start: int, end: int, width: int) -> list[float]:
        return values[max(0, start - width) : start] + values[end + 1 : end + 1 + width]

    @staticmethod
    def _classify_candidate(bandwidth_hz: float, prominence: float, flatness: float) -> str:
        if prominence >= 8.0 and bandwidth_hz >= 20_000 and (flatness >= 0.35 or prominence >= 20.0):
            return "Likely digital stream"
        if bandwidth_hz < 25_000:
            return "Narrowband carrier"
        return "Wideband activity"


class SweepAssembler:
    def __init__(self, detector: SignalDetector, source: str) -> None:
        self.detector = detector
        self.source = source
        self.current_timestamp: Optional[str] = None
        self.records: list[SweepRecord] = []
        self.sweep_count = 0

    def push(self, record: SweepRecord) -> Optional[SpectrumSnapshot]:
        if self.current_timestamp is None:
            self.current_timestamp = record.timestamp
        if record.timestamp != self.current_timestamp:
            snapshot = self._flush()
            self.current_timestamp = record.timestamp
            self.records = [record]
            return snapshot
        self.records.append(record)
        return None

    def finalize(self) -> Optional[SpectrumSnapshot]:
        if not self.records:
            return None
        return self._flush()

    def _flush(self) -> SpectrumSnapshot:
        ordered = sorted(self.records, key=lambda item: item.hz_low)
        frequencies_hz: list[float] = []
        power_db: list[float] = []
        for record in ordered:
            for idx, value in enumerate(record.power_db):
                frequencies_hz.append(record.hz_low + (idx + 0.5) * record.hz_bin_width)
                power_db.append(value)

        self.sweep_count += 1
        snapshot = SpectrumSnapshot(
            timestamp=self.current_timestamp or "",
            frequencies_hz=frequencies_hz,
            power_db=power_db,
            candidates=self.detector.detect(frequencies_hz, power_db),
            sweep_count=self.sweep_count,
            source=self.source,
        )
        self.records = []
        return snapshot


class HackRFSweepWorker:
    def __init__(
        self,
        config: dict,
        on_snapshot: Callable[[SpectrumSnapshot], None],
        on_status: Callable[[str], None],
    ) -> None:
        self.config = config
        self.on_snapshot = on_snapshot
        self.on_status = on_status
        self.detector = SignalDetector()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.process: Optional[subprocess.Popen[str]] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self) -> None:
        if self.config["source_mode"] == "simulation":
            self._run_simulation()
            return

        executable = self._find_hackrf_sweep()
        if not executable:
            self.on_status("hackrf_sweep was not found. Switch to Simulation mode or install HackRF tools.")
            return

        command = [
            executable,
            "-f",
            f'{self.config["start_mhz"]}:{self.config["stop_mhz"]}',
            "-w",
            str(self.config["bin_width_hz"]),
            "-l",
            str(self.config["lna_gain"]),
            "-g",
            str(self.config["vga_gain"]),
        ]
        if self.config["amp_enable"]:
            command.extend(["-a", "1"])
        if self.config["antenna_enable"]:
            command.extend(["-p", "1"])

        self.on_status("Launching HackRF sweep process.")
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self.on_status("hackrf_sweep could not be launched from the detected install path.")
            return
        stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        stderr_thread.start()

        assembler = SweepAssembler(self.detector, source="HackRF")
        assert self.process.stdout is not None
        for raw_line in self.process.stdout:
            if self.stop_event.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = SweepParser.parse_csv_line(line)
            except ValueError as exc:
                self.on_status(f"Ignored sweep row: {exc}")
                continue
            snapshot = assembler.push(record)
            if snapshot:
                self.on_snapshot(snapshot)

        final_snapshot = assembler.finalize()
        if final_snapshot:
            self.on_snapshot(final_snapshot)
        self.on_status("Sweep stopped.")

    def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        for line in self.process.stderr:
            if self.stop_event.is_set():
                return
            text = line.strip()
            if text:
                self.on_status(text)

    def _run_simulation(self) -> None:
        self.on_status("Simulation mode active. Generating synthetic RF activity.")
        start_hz = self.config["start_mhz"] * 1_000_000
        stop_hz = self.config["stop_mhz"] * 1_000_000
        bin_width_hz = self.config["bin_width_hz"]
        total_bins = max(32, int((stop_hz - start_hz) / bin_width_hz))
        frequencies_hz = [start_hz + (idx + 0.5) * bin_width_hz for idx in range(total_bins)]
        sweep_count = 0

        streams = [
            {"center": 433.92e6, "width": 150e3, "power": -42.0},
            {"center": 915.00e6, "width": 600e3, "power": -38.0},
            {"center": 920.60e6, "width": 90e3, "power": -55.0},
        ]

        while not self.stop_event.is_set():
            sweep_count += 1
            baseline = -82.0 + math.sin(time.time() / 4.0) * 1.5
            power_db: list[float] = []
            for frequency in frequencies_hz:
                value = baseline + random.uniform(-4.0, 3.0)
                for stream in streams:
                    distance = abs(frequency - stream["center"])
                    half_width = stream["width"] / 2.0
                    if distance <= half_width:
                        edge_penalty = (distance / max(half_width, 1.0)) * 4.5
                        value = max(value, stream["power"] - edge_penalty + random.uniform(-1.2, 1.2))
                power_db.append(value)

            self.on_snapshot(
                SpectrumSnapshot(
                    timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"),
                    frequencies_hz=frequencies_hz,
                    power_db=power_db,
                    candidates=self.detector.detect(frequencies_hz, power_db),
                    sweep_count=sweep_count,
                    source="Simulation",
                )
            )
            time.sleep(0.35)

    @staticmethod
    def _find_hackrf_sweep() -> Optional[str]:
        candidates = [
            shutil.which("hackrf_sweep"),
            str(Path.home() / "radioconda" / "Library" / "bin" / "hackrf_sweep.exe"),
            str(Path.home() / "miniconda3" / "Library" / "bin" / "hackrf_sweep.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None


class SpectrumCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, background="#07111b", highlightthickness=0, **kwargs)
        self.snapshot: Optional[SpectrumSnapshot] = None
        self.highlight_index: Optional[int] = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_snapshot(self, snapshot: SpectrumSnapshot) -> None:
        self.snapshot = snapshot
        self.redraw()

    def set_highlight(self, index: Optional[int]) -> None:
        self.highlight_index = index
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 200)
        height = max(self.winfo_height(), 200)
        if not self.snapshot or not self.snapshot.frequencies_hz:
            self.create_text(
                width / 2,
                height / 2,
                text="Start a scan to view the spectrum.",
                fill="#6b8aa7",
                font=("Segoe UI", 15, "bold"),
            )
            return

        freqs = self.snapshot.frequencies_hz
        powers = self.snapshot.power_db
        left_pad, right_pad, top_pad, bottom_pad = 58, 20, 20, 36
        plot_w = max(1, width - left_pad - right_pad)
        plot_h = max(1, height - top_pad - bottom_pad)

        min_power = math.floor(min(powers) / 10.0) * 10 - 5
        max_power = math.ceil(max(powers) / 10.0) * 10 + 5
        if max_power - min_power < 30:
            max_power = min_power + 30

        min_freq, max_freq = freqs[0], freqs[-1]
        freq_span = max(max_freq - min_freq, 1.0)

        for step in range(6):
            y = top_pad + (plot_h * step / 5)
            db = max_power - (max_power - min_power) * step / 5
            self.create_line(left_pad, y, width - right_pad, y, fill="#173247")
            self.create_text(left_pad - 10, y, text=f"{db:.0f}", fill="#7ca2bd", anchor="e", font=("Segoe UI", 9))

        for step in range(6):
            x = left_pad + (plot_w * step / 5)
            freq = min_freq + freq_span * step / 5
            self.create_line(x, top_pad, x, height - bottom_pad, fill="#102435")
            self.create_text(x, height - bottom_pad + 16, text=self._format_frequency(freq), fill="#7ca2bd", font=("Segoe UI", 9))

        for index, candidate in enumerate(self.snapshot.candidates[:8]):
            x0 = left_pad + ((candidate.start_hz - min_freq) / freq_span) * plot_w
            x1 = left_pad + ((candidate.end_hz - min_freq) / freq_span) * plot_w
            color = "#ffd166" if index == self.highlight_index else "#2dd4bf"
            stipple = "" if index == self.highlight_index else "gray25"
            self.create_rectangle(x0, top_pad, x1, height - bottom_pad, outline="", fill=color, stipple=stipple)
            self.create_text(
                max(left_pad + 6, min(x0 + 6, width - right_pad - 90)),
                top_pad + 14 + index * 16,
                anchor="w",
                text=f"{index + 1}. {candidate.label}",
                fill=color,
                font=("Segoe UI", 9, "bold"),
            )

        points: list[float] = []
        for freq, power in zip(freqs, powers):
            x = left_pad + ((freq - min_freq) / freq_span) * plot_w
            y = top_pad + (max_power - power) / (max_power - min_power) * plot_h
            points.extend((x, y))

        self.create_line(points, fill="#9be7ff", width=2, smooth=False)
        self.create_rectangle(left_pad, top_pad, width - right_pad, height - bottom_pad, outline="#31516a")
        self.create_text(
            left_pad,
            8,
            anchor="nw",
            text=f"{self.snapshot.source} sweep {self.snapshot.sweep_count}  |  {self.snapshot.timestamp}",
            fill="#d9edf7",
            font=("Segoe UI", 10, "bold"),
        )

    @staticmethod
    def _format_frequency(hz: float) -> str:
        if hz >= 1_000_000_000:
            return f"{hz / 1_000_000_000:.3f} GHz"
        if hz >= 1_000_000:
            return f"{hz / 1_000_000:.3f} MHz"
        if hz >= 1_000:
            return f"{hz / 1_000:.1f} kHz"
        return f"{hz:.0f} Hz"


class RFStreamFinderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RF Stream Finder")
        self.root.geometry("1380x820")
        self.root.configure(background="#09131e")

        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: Optional[HackRFSweepWorker] = None
        self.current_snapshot: Optional[SpectrumSnapshot] = None

        self.source_mode = tk.StringVar(value="simulation")
        self.start_mhz_var = tk.StringVar(value="430")
        self.stop_mhz_var = tk.StringVar(value="930")
        self.bin_width_var = tk.StringVar(value="100000")
        self.lna_var = tk.StringVar(value="24")
        self.vga_var = tk.StringVar(value="20")
        self.amp_var = tk.BooleanVar(value=False)
        self.antenna_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Idle")
        self.strongest_var = tk.StringVar(value="No sweep yet")
        self.sweep_count_var = tk.StringVar(value="0")
        self.last_update_var = tk.StringVar(value="Never")

        self._build_ui()
        self.root.after(100, self._pump_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#09131e")
        style.configure("Panel.TFrame", background="#102130")
        style.configure("Panel.TLabel", background="#102130", foreground="#ecf6ff")
        style.configure("Header.TLabel", background="#102130", foreground="#ffffff", font=("Segoe UI", 12, "bold"))
        style.configure("StatusValue.TLabel", background="#102130", foreground="#7fe7d2", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        container = ttk.Frame(self.root, style="Root.TFrame", padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=3)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(1, weight=1)

        control = ttk.Frame(container, style="Panel.TFrame", padding=14)
        control.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 14))
        for column in range(10):
            control.columnconfigure(column, weight=1)

        ttk.Label(control, text="RF Stream Finder", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(control, text="Mode", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 4))
        ttk.Combobox(control, textvariable=self.source_mode, values=["simulation", "hardware"], state="readonly", width=14).grid(row=2, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(control, text="Start MHz", style="Panel.TLabel").grid(row=1, column=1, sticky="w", pady=(12, 4))
        ttk.Entry(control, textvariable=self.start_mhz_var, width=10).grid(row=2, column=1, sticky="ew", padx=(0, 8))
        ttk.Label(control, text="Stop MHz", style="Panel.TLabel").grid(row=1, column=2, sticky="w", pady=(12, 4))
        ttk.Entry(control, textvariable=self.stop_mhz_var, width=10).grid(row=2, column=2, sticky="ew", padx=(0, 8))
        ttk.Label(control, text="Bin Width Hz", style="Panel.TLabel").grid(row=1, column=3, sticky="w", pady=(12, 4))
        ttk.Entry(control, textvariable=self.bin_width_var, width=12).grid(row=2, column=3, sticky="ew", padx=(0, 8))
        ttk.Label(control, text="LNA Gain", style="Panel.TLabel").grid(row=1, column=4, sticky="w", pady=(12, 4))
        ttk.Entry(control, textvariable=self.lna_var, width=8).grid(row=2, column=4, sticky="ew", padx=(0, 8))
        ttk.Label(control, text="VGA Gain", style="Panel.TLabel").grid(row=1, column=5, sticky="w", pady=(12, 4))
        ttk.Entry(control, textvariable=self.vga_var, width=8).grid(row=2, column=5, sticky="ew", padx=(0, 8))
        ttk.Checkbutton(control, text="RF Amp", variable=self.amp_var).grid(row=2, column=6, sticky="w")
        ttk.Checkbutton(control, text="Antenna Bias", variable=self.antenna_var).grid(row=2, column=7, sticky="w")
        ttk.Button(control, text="Start Scan", style="Accent.TButton", command=self.start_scan).grid(row=2, column=8, sticky="ew", padx=(10, 6))
        ttk.Button(control, text="Stop", command=self.stop_scan).grid(row=2, column=9, sticky="ew")

        spectrum_panel = ttk.Frame(container, style="Panel.TFrame", padding=10)
        spectrum_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        spectrum_panel.rowconfigure(0, weight=1)
        spectrum_panel.columnconfigure(0, weight=1)
        self.canvas = SpectrumCanvas(spectrum_panel)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        sidebar = ttk.Frame(container, style="Panel.TFrame", padding=12)
        sidebar.grid(row=1, column=1, sticky="nsew")
        sidebar.rowconfigure(2, weight=1)
        sidebar.rowconfigure(4, weight=1)
        sidebar.columnconfigure(0, weight=1)

        ttk.Label(sidebar, text="Status", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        status_frame = ttk.Frame(sidebar, style="Panel.TFrame")
        status_frame.grid(row=1, column=0, sticky="ew", pady=(8, 14))
        status_frame.columnconfigure(1, weight=1)
        self._add_status_row(status_frame, 0, "State", self.status_var)
        self._add_status_row(status_frame, 1, "Sweeps", self.sweep_count_var)
        self._add_status_row(status_frame, 2, "Updated", self.last_update_var)
        self._add_status_row(status_frame, 3, "Strongest", self.strongest_var)

        ttk.Label(sidebar, text="Detected Streams", style="Header.TLabel").grid(row=2, column=0, sticky="nw")
        self.candidate_tree = ttk.Treeview(sidebar, columns=("center", "bandwidth", "peak", "score"), show="headings", height=10)
        for name, label, width in (("center", "Center", 110), ("bandwidth", "Bandwidth", 90), ("peak", "Peak", 70), ("score", "Confidence", 80)):
            self.candidate_tree.heading(name, text=label)
            self.candidate_tree.column(name, width=width, anchor="center")
        self.candidate_tree.grid(row=3, column=0, sticky="nsew", pady=(8, 14))
        self.candidate_tree.bind("<<TreeviewSelect>>", self._on_candidate_select)

        ttk.Label(sidebar, text="Event Log", style="Header.TLabel").grid(row=4, column=0, sticky="nw")
        self.log_text = tk.Text(sidebar, height=10, background="#081520", foreground="#e3f2fd", insertbackground="#e3f2fd", relief="flat", wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        self.log_text.configure(state="disabled")

    def _add_status_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=f"{label}:", style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Label(parent, textvariable=variable, style="StatusValue.TLabel").grid(row=row, column=1, sticky="w", pady=2, padx=(8, 0))

    def start_scan(self) -> None:
        if self.worker:
            self._log("A scan is already running.")
            return

        try:
            config = {
                "source_mode": self.source_mode.get(),
                "start_mhz": int(float(self.start_mhz_var.get())),
                "stop_mhz": int(float(self.stop_mhz_var.get())),
                "bin_width_hz": int(float(self.bin_width_var.get())),
                "lna_gain": int(float(self.lna_var.get())),
                "vga_gain": int(float(self.vga_var.get())),
                "amp_enable": self.amp_var.get(),
                "antenna_enable": self.antenna_var.get(),
            }
        except ValueError:
            messagebox.showerror("Invalid Settings", "Please provide numeric scan settings.")
            return

        if config["start_mhz"] >= config["stop_mhz"]:
            messagebox.showerror("Invalid Range", "Start frequency must be below stop frequency.")
            return
        if config["bin_width_hz"] < 2_445:
            messagebox.showerror("Invalid Bin Width", "HackRF sweep bin width must be at least 2445 Hz.")
            return

        self.status_var.set("Starting")
        self._log(
            f"Starting {config['source_mode']} scan from {config['start_mhz']} MHz to {config['stop_mhz']} MHz"
            f" with {config['bin_width_hz']} Hz bins."
        )
        self.worker = HackRFSweepWorker(config, self._queue_snapshot, self._queue_status)
        self.worker.start()

    def stop_scan(self) -> None:
        if not self.worker:
            return
        self._log("Stopping scan.")
        worker = self.worker
        self.worker = None
        self.status_var.set("Stopping")
        threading.Thread(target=worker.stop, daemon=True).start()

    def _queue_snapshot(self, snapshot: SpectrumSnapshot) -> None:
        self.event_queue.put(("snapshot", snapshot))

    def _queue_status(self, status: str) -> None:
        self.event_queue.put(("status", status))

    def _pump_events(self) -> None:
        try:
            while True:
                event_type, payload = self.event_queue.get_nowait()
                if event_type == "snapshot":
                    self._handle_snapshot(payload)  # type: ignore[arg-type]
                elif event_type == "status":
                    self._handle_status(str(payload))
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._pump_events)

    def _handle_snapshot(self, snapshot: SpectrumSnapshot) -> None:
        self.current_snapshot = snapshot
        self.status_var.set(f"Scanning via {snapshot.source}")
        self.sweep_count_var.set(str(snapshot.sweep_count))
        self.last_update_var.set(datetime.now().strftime("%H:%M:%S"))
        if snapshot.candidates:
            strongest = snapshot.candidates[0]
            self.strongest_var.set(f"{strongest.center_hz / 1_000_000:.3f} MHz / {strongest.peak_db:.1f} dB")
        else:
            self.strongest_var.set("No candidate streams")
        self.canvas.set_snapshot(snapshot)
        self._refresh_candidates(snapshot.candidates)

    def _handle_status(self, status: str) -> None:
        self.status_var.set(status)
        self._log(status)
        if "stopped" in status.lower() or "not found" in status.lower():
            self.worker = None

    def _refresh_candidates(self, candidates: Iterable[SignalCandidate]) -> None:
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        for index, candidate in enumerate(candidates):
            self.candidate_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    f"{candidate.center_hz / 1_000_000:.3f} MHz",
                    self._human_bandwidth(candidate.bandwidth_hz),
                    f"{candidate.peak_db:.1f} dB",
                    f"{candidate.score * 100:.0f}%",
                ),
            )

    def _on_candidate_select(self, _event: object) -> None:
        selected = self.candidate_tree.selection()
        self.canvas.set_highlight(int(selected[0]) if selected else None)

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        self.stop_scan()
        self.root.after(200, self.root.destroy)

    @staticmethod
    def _human_bandwidth(bandwidth_hz: float) -> str:
        if bandwidth_hz >= 1_000_000:
            return f"{bandwidth_hz / 1_000_000:.2f} MHz"
        if bandwidth_hz >= 1_000:
            return f"{bandwidth_hz / 1_000:.0f} kHz"
        return f"{bandwidth_hz:.0f} Hz"


def main() -> None:
    root = tk.Tk()
    app = RFStreamFinderApp(root)
    app._log("Ready. Use Simulation mode to preview the UI without hardware.")
    root.mainloop()


if __name__ == "__main__":
    main()
