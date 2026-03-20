from __future__ import annotations

import array
import math
import os
import queue
import random
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import winsound
except ImportError:
    winsound = None


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
    band_name: str = "Unknown"
    source_hint: str = "Unclassified"


@dataclass(slots=True)
class SpectrumSnapshot:
    timestamp: str
    frequencies_hz: list[float]
    power_db: list[float]
    candidates: list[SignalCandidate]
    sweep_count: int
    source: str


@dataclass(slots=True)
class LoggedSignal:
    first_seen: str
    last_seen: str
    center_hz: float
    bandwidth_hz: float
    peak_db: float
    label: str
    band_name: str
    source_hint: str
    score: float
    hit_count: int


@dataclass(slots=True)
class IQAnalysis:
    dc_i: float
    dc_q: float
    avg_magnitude: float
    peak_magnitude: float
    magnitude_stddev: float
    phase_stddev: float
    iq_correlation: float
    scatter_points: list[tuple[float, float]]
    waveform_points: list[float]


@dataclass(slots=True)
class InvestigationResult:
    center_hz: float
    bandwidth_hz: float
    demod_mode: str
    audio_path: Optional[str]
    iq_path: Optional[str]
    summary: str
    recommended_next_step: str
    iq_analysis: IQAnalysis


class BandClassifier:
    BANDS = [
        (0.53e6, 1.71e6, "AM Broadcast", "AM radio / medium wave"),
        (26.965e6, 27.405e6, "CB Radio", "Citizen band or nearby HF users"),
        (87.5e6, 108e6, "FM Broadcast", "Broadcast FM audio"),
        (108e6, 137e6, "Airband", "Aviation voice / nav"),
        (144e6, 148e6, "2m Amateur", "Amateur radio"),
        (156e6, 162.025e6, "Marine VHF", "Marine voice / AIS adjacent"),
        (162.4e6, 162.55e6, "NOAA Weather", "Weather radio"),
        (162.55e6, 174e6, "VHF High", "Land mobile / public service"),
        (216e6, 225e6, "Military Air", "Military aviation"),
        (300e6, 380e6, "UHF Military/Gov", "Military or government"),
        (400e6, 406e6, "Satellite/NOAA", "Meteorological or satellite downlink"),
        (420e6, 450e6, "70cm Amateur / ISM", "Amateur, ISM, remotes"),
        (450e6, 470e6, "UHF Land Mobile", "Business / public safety"),
        (758e6, 806e6, "700 MHz Public Safety", "Public safety / LTE"),
        (824e6, 849e6, "Cellular Uplink", "Cellular / LTE / legacy"),
        (869e6, 894e6, "Cellular Downlink", "Cellular / LTE / legacy"),
        (902e6, 928e6, "915 MHz ISM", "ISM, LoRa, telemetry"),
        (960e6, 1215e6, "Aero Radionavigation", "DME / ADS-B nearby"),
        (1090e6, 1090.1e6, "ADS-B", "Aircraft transponder"),
        (1200e6, 1300e6, "23cm Amateur", "Amateur / amateur TV"),
        (1574e6, 1577e6, "GNSS L1", "GPS/GNSS"),
        (1610e6, 1627e6, "Satellite MSS", "L-band satellite"),
        (2400e6, 2483.5e6, "2.4 GHz ISM", "Wi-Fi, Bluetooth, ISM"),
        (3300e6, 4200e6, "3.5 GHz / C-band", "5G, fixed links, radar"),
        (5150e6, 5925e6, "5 GHz ISM/UNII", "Wi-Fi / radar / unlicensed"),
    ]

    @classmethod
    def classify(cls, center_hz: float) -> tuple[str, str]:
        for low, high, band_name, source_hint in cls.BANDS:
            if low <= center_hz <= high:
                return band_name, source_hint
        return "Unknown", "Unclassified"


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
            band_name, source_hint = BandClassifier.classify(center_hz)
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
                    band_name=band_name,
                    source_hint=source_hint,
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


class SignalHistory:
    def __init__(self, merge_hz: float = 150_000.0) -> None:
        self.merge_hz = merge_hz
        self.entries: list[LoggedSignal] = []

    def update(self, timestamp: str, candidates: Iterable[SignalCandidate]) -> list[LoggedSignal]:
        updated: list[LoggedSignal] = []
        for candidate in candidates:
            existing = self._match(candidate)
            if existing:
                existing.last_seen = timestamp
                existing.center_hz = (existing.center_hz * existing.hit_count + candidate.center_hz) / (existing.hit_count + 1)
                existing.bandwidth_hz = max(existing.bandwidth_hz, candidate.bandwidth_hz)
                existing.peak_db = max(existing.peak_db, candidate.peak_db)
                existing.score = max(existing.score, candidate.score)
                existing.label = candidate.label
                existing.band_name = candidate.band_name
                existing.source_hint = candidate.source_hint
                existing.hit_count += 1
                updated.append(existing)
            else:
                entry = LoggedSignal(
                    first_seen=timestamp,
                    last_seen=timestamp,
                    center_hz=candidate.center_hz,
                    bandwidth_hz=candidate.bandwidth_hz,
                    peak_db=candidate.peak_db,
                    label=candidate.label,
                    band_name=candidate.band_name,
                    source_hint=candidate.source_hint,
                    score=candidate.score,
                    hit_count=1,
                )
                self.entries.append(entry)
                updated.append(entry)
        self.entries.sort(key=lambda item: (item.hit_count, item.score, item.peak_db), reverse=True)
        return updated

    def _match(self, candidate: SignalCandidate) -> Optional[LoggedSignal]:
        for entry in self.entries:
            if abs(entry.center_hz - candidate.center_hz) <= max(self.merge_hz, candidate.bandwidth_hz):
                return entry
        return None


class SignalAnalyzer:
    def __init__(self, status_callback: Callable[[str], None]) -> None:
        self.status_callback = status_callback
        self.last_audio_path: Optional[str] = None

    def analyze_candidate(self, candidate: SignalCandidate, config: dict) -> InvestigationResult:
        mode = self._choose_mode(candidate)
        self.status_callback(f"Analyzing {candidate.center_hz / 1_000_000:.3f} MHz as {mode}.")
        if config["source_mode"] == "simulation":
            iq_path = self._write_simulated_iq(candidate)
        else:
            iq_path = self._capture_hardware_iq(candidate, config)

        if mode == "DIGITAL":
            iq_samples, _sample_rate = self._read_iq_file(iq_path)
            iq_analysis = self._analyze_iq(iq_samples)
            summary = (
                f"Best-effort classifier marked this signal as digital/unhandled. IQ capture saved for later analysis.\n"
                f"Center: {candidate.center_hz / 1_000_000:.3f} MHz\n"
                f"Bandwidth: {candidate.bandwidth_hz / 1_000:.1f} kHz\n"
                f"Band: {candidate.band_name}\n"
                f"Likely source: {candidate.source_hint}\n"
                f"Peak: {candidate.peak_db:.1f} dB\n"
                f"IQ file: {iq_path}\n"
                f"IQ metrics: DC(I)={iq_analysis.dc_i:+.3f}, DC(Q)={iq_analysis.dc_q:+.3f}, "
                f"|IQ| avg={iq_analysis.avg_magnitude:.3f}, corr={iq_analysis.iq_correlation:+.3f}"
            )
            return InvestigationResult(
                center_hz=candidate.center_hz,
                bandwidth_hz=candidate.bandwidth_hz,
                demod_mode=mode,
                audio_path=None,
                iq_path=iq_path,
                summary=summary,
                recommended_next_step="Inspect the IQ clip with protocol-specific tools or compare with known digital allocations.",
                iq_analysis=iq_analysis,
            )

        iq_samples, sample_rate = self._read_iq_file(iq_path)
        iq_analysis = self._analyze_iq(iq_samples)
        audio_samples = self._demodulate(iq_samples, sample_rate, mode)
        audio_path = self._write_wav(audio_samples, 48_000, candidate.center_hz, mode)
        self.last_audio_path = audio_path
        summary = (
            f"Demodulated {mode} audio preview.\n"
            f"Center: {candidate.center_hz / 1_000_000:.3f} MHz\n"
            f"Bandwidth: {candidate.bandwidth_hz / 1_000:.1f} kHz\n"
            f"Band: {candidate.band_name}\n"
            f"Likely source: {candidate.source_hint}\n"
            f"Peak: {candidate.peak_db:.1f} dB\n"
            f"Audio: {audio_path}\n"
            f"IQ: {iq_path}\n"
            f"IQ metrics: DC(I)={iq_analysis.dc_i:+.3f}, DC(Q)={iq_analysis.dc_q:+.3f}, "
            f"|IQ| avg={iq_analysis.avg_magnitude:.3f}, phase std={iq_analysis.phase_stddev:.3f}"
        )
        return InvestigationResult(
            center_hz=candidate.center_hz,
            bandwidth_hz=candidate.bandwidth_hz,
            demod_mode=mode,
            audio_path=audio_path,
            iq_path=iq_path,
            summary=summary,
            recommended_next_step="Listen for intelligible content, then refine bandwidth/gain or move to protocol-specific decoding if needed.",
            iq_analysis=iq_analysis,
        )

    def play_audio(self, path: Optional[str]) -> bool:
        if not path or not Path(path).exists() or winsound is None:
            return False
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True

    @staticmethod
    def _choose_mode(candidate: SignalCandidate) -> str:
        if candidate.label == "Likely digital stream":
            return "DIGITAL"
        if candidate.bandwidth_hz >= 140_000:
            return "WFM"
        if candidate.bandwidth_hz >= 20_000:
            return "NFM"
        return "AM"

    def _capture_hardware_iq(self, candidate: SignalCandidate, config: dict) -> str:
        executable = self._find_hackrf_transfer()
        if not executable:
            raise RuntimeError("hackrf_transfer was not found.")
        sample_rate = 2_000_000
        duration_s = 1.5
        samples = int(sample_rate * duration_s)
        iq_path = str(Path(tempfile.gettempdir()) / f"rf_capture_{int(candidate.center_hz)}_{int(time.time())}.iq")
        command = [
            executable,
            "-f",
            str(int(candidate.center_hz)),
            "-s",
            str(sample_rate),
            "-n",
            str(samples),
            "-l",
            str(config["lna_gain"]),
            "-g",
            str(config["vga_gain"]),
            "-r",
            iq_path,
        ]
        if config["amp_enable"]:
            command.extend(["-a", "1"])
        if config["antenna_enable"]:
            command.extend(["-p", "1"])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "hackrf_transfer failed").strip())
        return iq_path

    def _write_simulated_iq(self, candidate: SignalCandidate) -> str:
        sample_rate = 2_000_000
        duration_s = 1.0
        total = int(sample_rate * duration_s)
        audio_freq = 1000.0
        path = str(Path(tempfile.gettempdir()) / f"rf_sim_{int(candidate.center_hz)}_{int(time.time())}.iq")
        raw = array.array("b")
        mode = self._choose_mode(candidate)
        phase = 0.0
        for idx in range(total):
            t = idx / sample_rate
            if mode in {"WFM", "NFM"}:
                deviation = 60_000 if mode == "WFM" else 5_000
                phase += 2 * math.pi * deviation * math.sin(2 * math.pi * audio_freq * t) / sample_rate
                i = int(max(-127, min(127, math.cos(phase) * 100)))
                q = int(max(-127, min(127, math.sin(phase) * 100)))
            else:
                envelope = 0.55 + 0.35 * math.sin(2 * math.pi * audio_freq * t)
                i = int(max(-127, min(127, envelope * 110 * math.cos(2 * math.pi * 8_000 * t))))
                q = int(max(-127, min(127, envelope * 110 * math.sin(2 * math.pi * 8_000 * t))))
            raw.extend([i, q])
        with open(path, "wb") as handle:
            raw.tofile(handle)
        return path

    @staticmethod
    def _read_iq_file(path: str, sample_rate: int = 2_000_000) -> tuple[list[complex], int]:
        raw = array.array("b")
        with open(path, "rb") as handle:
            raw.frombytes(handle.read())
        iq_samples: list[complex] = []
        for idx in range(0, len(raw) - 1, 2):
            iq_samples.append(complex(raw[idx] / 128.0, raw[idx + 1] / 128.0))
        return iq_samples, sample_rate

    @staticmethod
    def _analyze_iq(iq_samples: list[complex]) -> IQAnalysis:
        if not iq_samples:
            return IQAnalysis(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [], [])
        i_values = [sample.real for sample in iq_samples]
        q_values = [sample.imag for sample in iq_samples]
        magnitudes = [abs(sample) for sample in iq_samples]
        phases = [math.atan2(sample.imag, sample.real) for sample in iq_samples if sample.real or sample.imag]
        dc_i = statistics.fmean(i_values)
        dc_q = statistics.fmean(q_values)
        avg_mag = statistics.fmean(magnitudes)
        peak_mag = max(magnitudes)
        mag_std = statistics.pstdev(magnitudes) if len(magnitudes) > 1 else 0.0
        phase_std = statistics.pstdev(phases) if len(phases) > 1 else 0.0
        if len(i_values) > 1 and len(q_values) > 1:
            i_mean = statistics.fmean(i_values)
            q_mean = statistics.fmean(q_values)
            covariance = statistics.fmean((i - i_mean) * (q - q_mean) for i, q in zip(i_values, q_values))
            denom = (statistics.pstdev(i_values) or 1.0) * (statistics.pstdev(q_values) or 1.0)
            corr = covariance / denom if denom else 0.0
        else:
            corr = 0.0
        step = max(1, len(iq_samples) // 1500)
        scatter = [(iq_samples[idx].real, iq_samples[idx].imag) for idx in range(0, len(iq_samples), step)][:1500]
        wave_step = max(1, len(magnitudes) // 500)
        waveform = [magnitudes[idx] for idx in range(0, len(magnitudes), wave_step)][:500]
        return IQAnalysis(dc_i, dc_q, avg_mag, peak_mag, mag_std, phase_std, corr, scatter, waveform)

    def _demodulate(self, iq_samples: list[complex], sample_rate: int, mode: str) -> list[int]:
        if mode in {"WFM", "NFM"}:
            baseband = self._fm_demod(iq_samples)
        else:
            baseband = self._am_demod(iq_samples)
        filtered = self._remove_dc(baseband)
        return self._resample_audio(filtered, sample_rate, 48_000)

    @staticmethod
    def _fm_demod(iq_samples: list[complex]) -> list[float]:
        if len(iq_samples) < 2:
            return []
        output: list[float] = []
        previous = iq_samples[0]
        for sample in iq_samples[1:]:
            product = sample * previous.conjugate()
            output.append(math.atan2(product.imag, product.real))
            previous = sample
        return output

    @staticmethod
    def _am_demod(iq_samples: list[complex]) -> list[float]:
        return [abs(sample) for sample in iq_samples]

    @staticmethod
    def _remove_dc(values: list[float]) -> list[float]:
        if not values:
            return []
        mean = statistics.fmean(values)
        return [value - mean for value in values]

    @staticmethod
    def _resample_audio(values: list[float], source_rate: int, target_rate: int) -> list[int]:
        if not values:
            return []
        step = max(1, int(source_rate / target_rate))
        reduced = [statistics.fmean(values[idx : idx + step]) for idx in range(0, len(values), step)]
        peak = max((abs(value) for value in reduced), default=1.0) or 1.0
        scale = 28000 / peak
        return [int(max(-32767, min(32767, value * scale))) for value in reduced]

    @staticmethod
    def _write_wav(samples: list[int], sample_rate: int, center_hz: float, mode: str) -> str:
        path = str(Path(tempfile.gettempdir()) / f"rf_audio_{mode.lower()}_{int(center_hz)}_{int(time.time())}.wav")
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            pcm = array.array("h", samples)
            handle.writeframes(pcm.tobytes())
        return path

    @staticmethod
    def _find_hackrf_transfer() -> Optional[str]:
        candidates = [
            shutil.which("hackrf_transfer"),
            str(Path.home() / "radioconda" / "Library" / "bin" / "hackrf_transfer.exe"),
            str(Path.home() / "miniconda3" / "Library" / "bin" / "hackrf_transfer.exe"),
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


class IQCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, background="#07111b", highlightthickness=0, **kwargs)
        self.analysis: Optional[IQAnalysis] = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_analysis(self, analysis: Optional[IQAnalysis]) -> None:
        self.analysis = analysis
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(220, self.winfo_width())
        height = max(220, self.winfo_height())
        if not self.analysis:
            self.create_text(width / 2, height / 2, text="IQ analyzer will appear after a stream capture.", fill="#6b8aa7", font=("Segoe UI", 13, "bold"))
            return

        top_h = int(height * 0.62)
        left_pad = 24
        right_pad = 24
        bottom_pad = 22
        mid_x = width / 2
        mid_y = top_h / 2
        radius = min((width - left_pad - right_pad) / 2, (top_h - 30) / 2) - 10

        self.create_text(16, 10, anchor="nw", text="Constellation / IQ Scatter", fill="#d9edf7", font=("Segoe UI", 10, "bold"))
        self.create_oval(mid_x - radius, mid_y - radius, mid_x + radius, mid_y + radius, outline="#31516a")
        self.create_line(mid_x - radius, mid_y, mid_x + radius, mid_y, fill="#173247")
        self.create_line(mid_x, mid_y - radius, mid_x, mid_y + radius, fill="#173247")
        for i_value, q_value in self.analysis.scatter_points:
            x = mid_x + i_value * radius * 0.95
            y = mid_y - q_value * radius * 0.95
            self.create_oval(x - 1, y - 1, x + 1, y + 1, outline="", fill="#2dd4bf")

        wave_top = top_h + 28
        wave_bottom = height - bottom_pad
        self.create_text(16, top_h + 6, anchor="nw", text="Magnitude Envelope", fill="#d9edf7", font=("Segoe UI", 10, "bold"))
        self.create_rectangle(left_pad, wave_top, width - right_pad, wave_bottom, outline="#31516a")
        if self.analysis.waveform_points:
            peak = max(self.analysis.waveform_points) or 1.0
            points: list[float] = []
            for idx, value in enumerate(self.analysis.waveform_points):
                x = left_pad + idx / max(1, len(self.analysis.waveform_points) - 1) * (width - left_pad - right_pad)
                y = wave_bottom - (value / peak) * (wave_bottom - wave_top)
                points.extend((x, y))
            self.create_line(points, fill="#ffd166", width=2)


class RFStreamFinderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RF Stream Finder")
        self.root.geometry("1440x940")
        self.root.configure(background="#09131e")

        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: Optional[HackRFSweepWorker] = None
        self.current_snapshot: Optional[SpectrumSnapshot] = None
        self.signal_history = SignalHistory()
        self.analyzer = SignalAnalyzer(self._queue_status)
        self.history_entries: list[LoggedSignal] = []
        self.last_analysis: Optional[InvestigationResult] = None

        self.source_mode = tk.StringVar(value="simulation")
        self.start_mhz_var = tk.StringVar(value="1")
        self.stop_mhz_var = tk.StringVar(value="6000")
        self.bin_width_var = tk.StringVar(value="5000000")
        self.lna_var = tk.StringVar(value="24")
        self.vga_var = tk.StringVar(value="20")
        self.amp_var = tk.BooleanVar(value=False)
        self.antenna_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Idle")
        self.strongest_var = tk.StringVar(value="No sweep yet")
        self.sweep_count_var = tk.StringVar(value="0")
        self.last_update_var = tk.StringVar(value="Never")
        self.analysis_var = tk.StringVar(value="No stream analyzed yet")

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
        container.rowconfigure(2, weight=1)

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
        sidebar.columnconfigure(0, weight=1)

        ttk.Label(sidebar, text="Status", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        status_frame = ttk.Frame(sidebar, style="Panel.TFrame")
        status_frame.grid(row=1, column=0, sticky="ew", pady=(8, 14))
        status_frame.columnconfigure(1, weight=1)
        self._add_status_row(status_frame, 0, "State", self.status_var)
        self._add_status_row(status_frame, 1, "Sweeps", self.sweep_count_var)
        self._add_status_row(status_frame, 2, "Updated", self.last_update_var)
        self._add_status_row(status_frame, 3, "Strongest", self.strongest_var)
        self._add_status_row(status_frame, 4, "Analysis", self.analysis_var)

        ttk.Label(sidebar, text="Detected Streams", style="Header.TLabel").grid(row=2, column=0, sticky="nw")
        self.candidate_tree = ttk.Treeview(sidebar, columns=("center", "bandwidth", "band", "peak", "score"), show="headings", height=10)
        for name, label, width in (("center", "Center", 100), ("bandwidth", "Bandwidth", 90), ("band", "Band", 120), ("peak", "Peak", 70), ("score", "Confidence", 80)):
            self.candidate_tree.heading(name, text=label)
            self.candidate_tree.column(name, width=width, anchor="center")
        self.candidate_tree.grid(row=3, column=0, sticky="nsew", pady=(8, 14))
        self.candidate_tree.bind("<<TreeviewSelect>>", self._on_candidate_select)

        action_bar = ttk.Frame(sidebar, style="Panel.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew")
        action_bar.columnconfigure(0, weight=1)
        action_bar.columnconfigure(1, weight=1)
        ttk.Button(action_bar, text="Analyze Selected", style="Accent.TButton", command=self.analyze_selected_stream).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(action_bar, text="Play Last Audio", command=self.play_last_audio).grid(row=0, column=1, sticky="ew")

        notebook = ttk.Notebook(container)
        notebook.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(14, 0))

        history_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=12)
        investigation_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=12)
        log_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=12)
        notebook.add(history_tab, text="Found Signals")
        notebook.add(investigation_tab, text="Investigation")
        notebook.add(log_tab, text="Event Log")

        history_tab.rowconfigure(1, weight=1)
        history_tab.columnconfigure(0, weight=1)
        ttk.Label(history_tab, text="Persistent Signal Log", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.history_tree = ttk.Treeview(history_tab, columns=("first", "last", "center", "band", "source", "hits", "label", "peak"), show="headings", height=8)
        for name, label, width in (
            ("first", "First Seen", 150),
            ("last", "Last Seen", 150),
            ("center", "Center", 100),
            ("band", "Band", 130),
            ("source", "Likely Source", 180),
            ("hits", "Hits", 60),
            ("label", "Type", 150),
            ("peak", "Peak", 80),
        ):
            self.history_tree.heading(name, text=label)
            self.history_tree.column(name, width=width, anchor="center")
        self.history_tree.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        investigation_tab.rowconfigure(1, weight=1)
        investigation_tab.rowconfigure(2, weight=2)
        investigation_tab.columnconfigure(0, weight=1)
        ttk.Label(investigation_tab, text="Stream Investigation", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.investigation_text = tk.Text(investigation_tab, height=12, background="#081520", foreground="#e3f2fd", insertbackground="#e3f2fd", relief="flat", wrap="word", font=("Consolas", 10))
        self.investigation_text.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        self.investigation_text.configure(state="disabled")
        self.iq_canvas = IQCanvas(investigation_tab)
        self.iq_canvas.grid(row=2, column=0, sticky="nsew")

        log_tab.rowconfigure(1, weight=1)
        log_tab.columnconfigure(0, weight=1)
        ttk.Label(log_tab, text="Continuous Event Log", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_tab, height=10, background="#081520", foreground="#e3f2fd", insertbackground="#e3f2fd", relief="flat", wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
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
                elif event_type == "analysis":
                    self._handle_analysis(payload)  # type: ignore[arg-type]
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
        updated_entries = self.signal_history.update(snapshot.timestamp, snapshot.candidates)
        if updated_entries:
            self._refresh_history()
            if any(entry.hit_count == 1 for entry in updated_entries):
                newest = updated_entries[0]
                self._log(
                    f"Logged signal {newest.center_hz / 1_000_000:.3f} MHz"
                    f" ({newest.label}, {newest.band_name}, {newest.peak_db:.1f} dB)."
                )

    def _handle_status(self, status: str) -> None:
        self.status_var.set(status)
        self._log(status)
        if "stopped" in status.lower() or "not found" in status.lower():
            self.worker = None

    def _handle_analysis(self, result: InvestigationResult) -> None:
        self.last_analysis = result
        self.analysis_var.set(result.demod_mode)
        self._set_text(
            self.investigation_text,
            result.summary + "\n\nRecommended next step:\n" + result.recommended_next_step,
        )
        self.iq_canvas.set_analysis(result.iq_analysis)
        if result.audio_path:
            played = self.analyzer.play_audio(result.audio_path)
            self._log(
                f"Analysis complete for {result.center_hz / 1_000_000:.3f} MHz"
                f" using {result.demod_mode}. Audio {'started' if played else 'saved'}."
            )
        else:
            self._log(
                f"Analysis complete for {result.center_hz / 1_000_000:.3f} MHz"
                f" using {result.demod_mode}. No audio preview available."
            )

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
                    candidate.band_name,
                    f"{candidate.peak_db:.1f} dB",
                    f"{candidate.score * 100:.0f}%",
                ),
            )

    def _refresh_history(self) -> None:
        self.history_tree.delete(*self.history_tree.get_children())
        self.history_entries = list(self.signal_history.entries)
        for index, entry in enumerate(self.history_entries[:200]):
            self.history_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    entry.first_seen,
                    entry.last_seen,
                    f"{entry.center_hz / 1_000_000:.3f} MHz",
                    entry.band_name,
                    entry.source_hint,
                    entry.hit_count,
                    entry.label,
                    f"{entry.peak_db:.1f} dB",
                ),
            )

    def _on_candidate_select(self, _event: object) -> None:
        selected = self.candidate_tree.selection()
        self.canvas.set_highlight(int(selected[0]) if selected else None)

    def analyze_selected_stream(self) -> None:
        if not self.current_snapshot:
            messagebox.showinfo("No Spectrum", "Run a scan and select a stream first.")
            return
        selected = self.candidate_tree.selection()
        if not selected:
            messagebox.showinfo("No Selection", "Select a detected stream to investigate.")
            return
        candidate = self.current_snapshot.candidates[int(selected[0])]
        if self.worker:
            self._log("Pausing live scan for stream investigation.")
            self.stop_scan()
        self.analysis_var.set("Capturing")
        self._set_text(
            self.investigation_text,
            f"Capturing IQ around {candidate.center_hz / 1_000_000:.3f} MHz for analysis...",
        )
        threading.Thread(target=self._analyze_candidate_thread, args=(candidate,), daemon=True).start()

    def _analyze_candidate_thread(self, candidate: SignalCandidate) -> None:
        try:
            config = self._current_config()
            result = self.analyzer.analyze_candidate(candidate, config)
            self.event_queue.put(("analysis", result))
        except Exception as exc:  # noqa: BLE001
            self._queue_status(f"Analysis failed: {exc}")

    def play_last_audio(self) -> None:
        if not self.last_analysis or not self.last_analysis.audio_path:
            messagebox.showinfo("No Audio", "Analyze a stream first to generate audio output.")
            return
        if not self.analyzer.play_audio(self.last_analysis.audio_path):
            messagebox.showerror("Playback Failed", "The last audio file is unavailable for playback.")
            return
        self._log(f"Replaying audio from {self.last_analysis.audio_path}")

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    @staticmethod
    def _set_text(widget: tk.Text, message: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", message)
        widget.configure(state="disabled")

    def _on_close(self) -> None:
        self.stop_scan()
        self.root.after(200, self.root.destroy)

    def _current_config(self) -> dict:
        return {
            "source_mode": self.source_mode.get(),
            "start_mhz": int(float(self.start_mhz_var.get())),
            "stop_mhz": int(float(self.stop_mhz_var.get())),
            "bin_width_hz": int(float(self.bin_width_var.get())),
            "lna_gain": int(float(self.lna_var.get())),
            "vga_gain": int(float(self.vga_var.get())),
            "amp_enable": self.amp_var.get(),
            "antenna_enable": self.antenna_var.get(),
        }

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
