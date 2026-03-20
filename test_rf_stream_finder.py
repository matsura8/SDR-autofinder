import unittest

from rf_stream_finder import BandClassifier, SignalAnalyzer, SignalCandidate, SignalDetector, SignalHistory, SweepAssembler, SweepParser


class SweepParserTests(unittest.TestCase):
    def test_parse_csv_line(self) -> None:
        line = "2019-01-03, 11:57:34.967805, 2400000000, 2405000000, 1000000.00, 20, -64.72, -63.36, -60.91"
        record = SweepParser.parse_csv_line(line)
        self.assertEqual(record.timestamp, "2019-01-03 11:57:34.967805")
        self.assertEqual(record.hz_low, 2_400_000_000)
        self.assertEqual(record.hz_high, 2_405_000_000)
        self.assertEqual(record.num_samples, 20)
        self.assertEqual(record.power_db, [-64.72, -63.36, -60.91])


class SweepAssemblerTests(unittest.TestCase):
    def test_flushes_on_timestamp_change(self) -> None:
        detector = SignalDetector(threshold_db=5.0, min_bins=2)
        assembler = SweepAssembler(detector=detector, source="test")
        first = SweepParser.parse_csv_line(
            "2019-01-03, 11:57:34.967805, 2400000000, 2402000000, 1000000.00, 20, -80, -50"
        )
        second = SweepParser.parse_csv_line(
            "2019-01-03, 11:57:35.000000, 2400000000, 2402000000, 1000000.00, 20, -82, -81"
        )
        self.assertIsNone(assembler.push(first))
        snapshot = assembler.push(second)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.sweep_count, 1)
        self.assertEqual(len(snapshot.frequencies_hz), 2)


class SignalDetectorTests(unittest.TestCase):
    def test_detects_wideband_plateau_as_likely_digital(self) -> None:
        detector = SignalDetector(threshold_db=6.0, min_bins=3)
        frequencies_hz = [915_000_000 + idx * 50_000 for idx in range(20)]
        power_db = [-84.0] * 20
        for idx in range(6, 11):
            power_db[idx] = -42.0 + (idx % 2) * 0.7
        candidates = detector.detect(frequencies_hz, power_db)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].label, "Likely digital stream")
        self.assertGreater(candidates[0].score, 0.5)


class SignalHistoryTests(unittest.TestCase):
    def test_merges_repeat_hits_near_same_frequency(self) -> None:
        history = SignalHistory(merge_hz=100_000)
        first = SignalCandidate(100.0, 140.0, 120.0, 40.0, -30.0, -35.0, 15.0, 0.7, "Wideband activity")
        second = SignalCandidate(110.0, 150.0, 125.0, 40.0, -28.0, -33.0, 16.0, 0.8, "Wideband activity")
        history.update("2026-03-19 18:00:00.0", [first])
        history.update("2026-03-19 18:00:02.0", [second])
        self.assertEqual(len(history.entries), 1)
        self.assertEqual(history.entries[0].hit_count, 2)
        self.assertEqual(history.entries[0].last_seen, "2026-03-19 18:00:02.0")
        self.assertEqual(history.entries[0].band_name, "Unknown")


class BandClassifierTests(unittest.TestCase):
    def test_classifies_fm_broadcast(self) -> None:
        band_name, source_hint = BandClassifier.classify(99_900_000)
        self.assertEqual(band_name, "FM Broadcast")
        self.assertIn("Broadcast FM", source_hint)


class SignalAnalyzerTests(unittest.TestCase):
    def test_choose_mode_prefers_digital_for_digital_candidates(self) -> None:
        candidate = SignalCandidate(0, 0, 915_000_000, 300_000, -40.0, -45.0, 20.0, 0.9, "Likely digital stream")
        self.assertEqual(SignalAnalyzer._choose_mode(candidate), "DIGITAL")

    def test_choose_mode_prefers_wfm_for_wide_signals(self) -> None:
        candidate = SignalCandidate(0, 0, 99_900_000, 200_000, -40.0, -45.0, 12.0, 0.7, "Wideband activity")
        self.assertEqual(SignalAnalyzer._choose_mode(candidate), "WFM")

    def test_iq_analysis_returns_points_and_metrics(self) -> None:
        iq = [complex(0.1, 0.2), complex(-0.2, 0.1), complex(0.0, -0.3), complex(0.25, 0.05)] * 100
        analysis = SignalAnalyzer._analyze_iq(iq)
        self.assertTrue(analysis.scatter_points)
        self.assertTrue(analysis.waveform_points)
        self.assertGreater(analysis.peak_magnitude, 0.0)


if __name__ == "__main__":
    unittest.main()
