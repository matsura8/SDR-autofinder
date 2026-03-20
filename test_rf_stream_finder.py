import unittest

from rf_stream_finder import SignalDetector, SweepAssembler, SweepParser


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


if __name__ == "__main__":
    unittest.main()
