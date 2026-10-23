"""Parser and shutoff tests. They do not import Klipper."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bedtime


def klipper_offsets(data):
    """Replay virtual_sdcard's split and encode-length accounting."""
    text = data.decode("utf-8")
    lines = text.split("\n")
    lines.pop()
    offset = 0
    found = []
    for line in lines:
        found.append((offset, line))
        offset += len(line.encode("utf-8")) + 1
    return found


class LineOffsetTest(unittest.TestCase):
    def _lines(self, data):
        handle = tempfile.NamedTemporaryFile(delete=False)
        try:
            handle.write(data)
            handle.close()
            return list(bedtime.iter_gcode_lines(handle.name))
        finally:
            os.unlink(handle.name)

    def _assert_matches_klipper(self, data):
        expected = klipper_offsets(data)
        got = [(offset, raw.decode("utf-8"))
               for offset, raw, _ in self._lines(data)]
        self.assertEqual(expected, got)

    def test_lf_crlf_unicode_and_truncated_tail(self):
        data = "; note café\nM73 P1 R9\r\nG1 X1\nno newline at end".encode(
            "utf-8")
        self._assert_matches_klipper(data)
        lines = self._lines(data)
        self.assertEqual(3, len(lines))
        self.assertTrue(lines[1][1].startswith(b"M73"))
        # "; note café\n" is 13 bytes because é encodes as two bytes.
        self.assertEqual(13, lines[1][0])


class ParseTest(unittest.TestCase):
    def parse(self, text):
        handle = tempfile.NamedTemporaryFile(delete=False)
        try:
            handle.write(text.encode("utf-8"))
            handle.close()
            return bedtime.parse_gcode_file(handle.name)
        finally:
            os.unlink(handle.name)

    def test_m73_interpolation_ignores_comments_and_silent_mode(self):
        # Offsets are computed from the same splitter the parser uses.
        text = "\n".join([
            "; M73 P1 R99",
            "M73 Q10 S30",
            "M73 P10 R2",
            "G1 X1 ; M73 P1 R99",
            "M73 P50 R1 Q50 S9",
            "G1 X2",
            "m73 p100 r0",
            "G1 X3",
            "; estimated printing time (normal mode) = 2m 30s",
            "",
        ])
        timeline = self.parse(text)
        self.assertEqual("m73", timeline.source)
        self.assertEqual(150.0, timeline.total_seconds)
        self.assertEqual(3, len(timeline.points))
        first = timeline.points[0][0]
        second = timeline.points[1][0]
        self.assertIsNone(timeline.remaining_at(first - 1))
        self.assertEqual(120.0, timeline.remaining_at(first))
        mid = (first + second) // 2
        self.assertAlmostEqual(90.0, timeline.remaining_at(mid), places=3)
        # R is minutes. The silent S value on the same line is ignored,
        # so the middle marker is 60s, not 9 minutes.
        self.assertEqual(60.0, timeline.remaining_at(second))
        last = timeline.points[2][0]
        self.assertEqual(0.0, timeline.remaining_at(last))
        self.assertEqual(0.0, timeline.remaining_at(last + 500))

    def test_slope_after_last_marker(self):
        text = "\n".join([
            "M73 P0 R2",
            "G1 X1",
            "M73 P50 R1",
            "G1 X2",
            "",
        ])
        timeline = self.parse(text)
        last_offset, last_remaining = timeline.points[-1]
        prev_offset, prev_remaining = timeline.points[-2]
        self.assertEqual(60.0, last_remaining)
        self.assertEqual(120.0, prev_remaining)
        span = float(last_offset - prev_offset)
        rate = (prev_remaining - last_remaining) / span
        extra = 8
        expected = last_remaining - extra * rate
        self.assertAlmostEqual(
            expected, timeline.remaining_at(last_offset + extra), places=3)
        self.assertGreater(expected, 0.0)

    def test_rising_m73_is_ignored(self):
        text = "\n".join([
            "M73 P1 R10",
            "M73 P2 R9",
            "M73 P1 R15",
            "M73 P3 R8",
            "",
        ])
        timeline = self.parse(text)
        remainings = [point[1] for point in timeline.points]
        self.assertEqual([600.0, 540.0, 480.0], remainings)

    def test_line_numbers_and_checksum(self):
        text = "N10 M73 P1 R5*23\nN11 M73 P2 R4*11\n"
        timeline = self.parse(text)
        self.assertEqual("m73", timeline.source)
        self.assertEqual(300.0, timeline.remaining_at(timeline.points[0][0]))

    def test_cura_layer_times(self):
        text = "\n".join([
            ";FLAVOR:Marlin",
            ";TIME:100",
            "G1 X0",
            ";TIME_ELAPSED:10",
            "G1 X1",
            ";TIME_ELAPSED:40",
            "G1 X2",
            ";TIME_ELAPSED:100",
            "",
        ])
        timeline = self.parse(text)
        self.assertEqual("cura", timeline.source)
        self.assertEqual(100.0, timeline.total_seconds)
        # At the 40s mark, 60s remain.
        mark = timeline.points[1][0]
        self.assertAlmostEqual(60.0, timeline.remaining_at(mark), places=3)

    def test_byte_fallback_excludes_config_block(self):
        body = "\n".join(["G1 X%d" % (i,) for i in range(10)])
        config = "\n".join(
            ["; CONFIG_BLOCK_START"]
            + ["; setting_%d = 1" % (i,) for i in range(100)]
            + ["; estimated printing time (normal mode) = 100s"])
        text = body + "\n" + config + "\n"
        timeline = self.parse(text)
        self.assertEqual("bytes", timeline.source)
        self.assertEqual(100.0, timeline.total_seconds)
        self.assertEqual(100.0, timeline.remaining_at(timeline.body_start))
        self.assertEqual(0.0, timeline.remaining_at(timeline.body_end))
        # The config block is most of the file. A byte fraction of the
        # whole file would already look half done at the first move.
        self.assertGreater(timeline.remaining_at(timeline.body_start), 90.0)
        midpoint = (timeline.body_start + timeline.body_end) // 2
        self.assertAlmostEqual(
            50.0, timeline.remaining_at(midpoint), delta=15.0)

    def test_footer_cannot_fake_progress_at_the_first_marker(self):
        # A 2h footer plus an opening M73 that already says one minute
        # left must not look like the first layers are finished.
        text = "\n".join([
            "M73 P0 R1",
            "G1 X1",
            "M73 P100 R0",
            "; estimated printing time (normal mode) = 2h 0m 0s",
            "",
        ])
        timeline = self.parse(text)
        first = timeline.points[0][0]
        remaining = timeline.remaining_at(first)
        consumed = timeline.consumed_at(first, remaining)
        self.assertEqual(60.0, remaining)
        self.assertEqual(0.0, consumed)
        self.assertEqual(7200.0, timeline.total_seconds)
        self.assertFalse(bedtime.should_release(
            remaining, consumed, timeline.total_seconds, 180., 120.))

    def test_no_estimate(self):
        timeline = self.parse("G1 X1\nG1 X2\n")
        self.assertEqual("none", timeline.source)
        self.assertIsNone(timeline.remaining_at(0))

    def test_duration_phrases(self):
        self.assertEqual(150.0, bedtime.parse_duration("2m 30s"))
        self.assertEqual(3600 + 33 * 60 + 15,
                         bedtime.parse_duration("1h 33m 15s"))
        self.assertEqual(45.0, bedtime.parse_duration("45s"))
        self.assertIsNone(bedtime.parse_duration("soon"))


class ReleaseDecisionTest(unittest.TestCase):
    def test_speed_factor_moves_the_shutoff(self):
        lead = 180.
        guard = 120.
        total = 3600.
        # 4 min of slicer time left, 10 min into a 1 h print, full speed.
        self.assertFalse(bedtime.should_release(
            240., 600., total, lead, guard))
        # Same place in the file at 200%: 2 min of wall time left.
        self.assertTrue(bedtime.should_release(
            240. / 2., 600., total, lead, guard))
        # At 50% that 4 min of slicer time is 8 min of wall time.
        self.assertFalse(bedtime.should_release(
            240. / 0.5, 600., total, lead, guard))

    def test_short_print_and_first_layer_guard(self):
        self.assertFalse(bedtime.should_release(
            30., 200., 200., 180., 120.))
        # Long enough print, but the toolpath has only just started.
        self.assertFalse(bedtime.should_release(
            30., 60., 3600., 180., 120.))
        self.assertTrue(bedtime.should_release(
            180., 120., 3600., 180., 120.))

    def test_unknown_position_does_not_release(self):
        self.assertFalse(bedtime.should_release(
            None, 500., 3600., 180., 120.))
        self.assertFalse(bedtime.should_release(
            10., None, 3600., 180., 120.))


class FormatTest(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual("45 s", bedtime.format_duration(45))
        self.assertEqual("3 min", bedtime.format_duration(180))
        self.assertEqual("2 h 14 min", bedtime.format_duration(2 * 3600 + 14 * 60))


if __name__ == "__main__":
    unittest.main()
