# Turn a Voron bed heater off shortly before a print ends.
#
# Slicers already write a remaining-time map into the gcode (M73 from
# OrcaSlicer, PrusaSlicer and SuperSlicer; TIME_ELAPSED from Cura).
# This extra reads that map once when a print starts, then watches
# virtual_sdcard.file_position. When the time left, scaled by the live
# M220 speed factor, drops to lead_time, it sets the bed target and
# leaves the hotend alone so the last layers still finish.
#
# File offsets match klippy/extras/virtual_sdcard.py: lines are split on
# '\n', the newline counts as one byte, and a trailing partial line is
# ignored. Comparing those offsets with file_position is what makes the
# shutoff land on the right move instead of on a byte-progress guess.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import os
import re
import threading

_DURATION_UNITS = (
    ("d", 86400),
    ("h", 3600),
    ("m", 60),
    ("s", 1),
)
_DURATION_RE = re.compile(r"(\d+)\s*([dhms])\b", re.IGNORECASE)
_FOOTER_TIME_RE = re.compile(
    r"estimated printing time \(normal mode\)\s*=\s*(.+)",
    re.IGNORECASE,
)


def format_duration(seconds):
    """Short console form: '45 s', '3 min', '2 h 14 min'."""
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return "%d s" % (seconds,)
    minutes = int(round(seconds / 60.0))
    if minutes < 90:
        return "%d min" % (minutes,)
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return "%d h" % (hours,)
    return "%d h %d min" % (hours, mins)


def parse_duration(text):
    """Parse a slicer footer like '1h 33m 15s' into seconds."""
    matches = _DURATION_RE.findall(text or "")
    if not matches:
        return None
    total = 0
    for value, unit in matches:
        scale = dict(_DURATION_UNITS)[unit.lower()]
        total += int(value) * scale
    return float(total)


def iter_gcode_lines(path):
    """Yield (start_offset, line_bytes_without_lf, next_offset)."""
    offset = 0
    pending = b""
    with open(path, "rb") as gcode_file:
        while True:
            chunk = gcode_file.read(1024 * 1024)
            if not chunk:
                break
            pending += chunk
            parts = pending.split(b"\n")
            pending = parts.pop()
            for part in parts:
                next_offset = offset + len(part) + 1
                yield offset, part, next_offset
                offset = next_offset


def _line_text(part):
    if part.endswith(b"\r"):
        part = part[:-1]
    return part.decode("utf-8", "replace").strip()


def _command_tokens(text):
    """G-code words on a line, excluding comments and an optional checksum."""
    if not text or text.startswith(";"):
        return []
    code = text.split(";", 1)[0].split("*", 1)[0].strip()
    tokens = code.split()
    if (tokens and tokens[0].upper().startswith("N")
            and tokens[0][1:].isdigit()):
        tokens = tokens[1:]
    return tokens


def _word_value(tokens, letter):
    letter = letter.upper()
    for token in tokens[1:]:
        if token[:1].upper() == letter and len(token) > 1:
            try:
                return float(token[1:])
            except ValueError:
                return None
    return None


class Timeline:
    """Where in the file the slicer says the remaining time is."""

    def __init__(self, source, total_seconds, points, body_start, body_end):
        self.source = source
        self.total_seconds = total_seconds
        # (file_offset, remaining_seconds), sorted, remaining non-increasing
        self.points = points
        self.body_start = body_start
        self.body_end = body_end

    def remaining_at(self, offset):
        """Slicer-estimated seconds left at this file offset, or None."""
        if self.source == "bytes":
            return self._remaining_from_bytes(offset)
        points = self.points
        if not points or offset < points[0][0]:
            return None
        lo = 0
        hi = len(points) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if points[mid][0] <= offset:
                lo = mid
            else:
                hi = mid - 1
        if lo + 1 < len(points):
            start, start_remaining = points[lo]
            end, end_remaining = points[lo + 1]
            span = end - start
            if span <= 0:
                return end_remaining
            frac = (offset - start) / float(span)
            return start_remaining + (end_remaining - start_remaining) * frac
        # Past the last marker the slicer stops talking. Keep the slope of
        # the previous minute so a lead shorter than that step still fires.
        if len(points) >= 2:
            prev, prev_remaining = points[-2]
            last, last_remaining = points[-1]
            span = last - prev
            if span > 0 and prev_remaining > last_remaining:
                rate = (prev_remaining - last_remaining) / float(span)
                return max(0., last_remaining - (offset - last) * rate)
        return max(0., points[-1][1])

    def _remaining_from_bytes(self, offset):
        if not self.total_seconds:
            return None
        span = self.body_end - self.body_start
        if span <= 0:
            return None
        if offset <= self.body_start:
            return float(self.total_seconds)
        if offset >= self.body_end:
            return 0.
        done = (offset - self.body_start) / float(span)
        return self.total_seconds * (1. - done)

    def consumed_at(self, offset, remaining):
        # Prefer the markers' own clock. A footer that says the print is
        # hours long must not make a tiny remaining-time at the first
        # marker look like the first layers are already done.
        if remaining is None:
            return None
        if self.points:
            return max(0., self.points[0][1] - remaining)
        if self.total_seconds is not None:
            return max(0., self.total_seconds - remaining)
        return None


def _monotonic_points(points):
    """Drop markers that claim the print got longer. Later is safer."""
    kept = []
    for offset, remaining in points:
        if kept and remaining > kept[-1][1]:
            continue
        kept.append((offset, remaining))
    return kept


def parse_gcode_file(path):
    """Build a Timeline from one gcode file. Safe to call off the reactor."""
    m73_points = []
    elapsed_points = []
    cura_total = None
    footer_total = None
    body_start = None
    body_end = None
    file_end = 0

    for offset, raw, next_offset in iter_gcode_lines(path):
        file_end = next_offset
        text = _line_text(raw)
        if not text:
            continue
        if text.startswith(";"):
            body = text[1:].strip()
            upper = body.upper()
            if upper.startswith("TIME_ELAPSED:"):
                try:
                    elapsed_points.append(
                        (offset, float(body.split(":", 1)[1].strip())))
                except ValueError:
                    pass
            elif upper.startswith("TIME:"):
                try:
                    cura_total = float(body.split(":", 1)[1].strip())
                except ValueError:
                    pass
            else:
                footer = _FOOTER_TIME_RE.search(body)
                if footer:
                    parsed = parse_duration(footer.group(1))
                    if parsed is not None and footer_total is None:
                        footer_total = parsed
                        if body_end is None:
                            body_end = offset
            if body_end is None and (
                    upper.startswith("CONFIG_BLOCK_START")
                    or "prusaslicer_config = begin" in body.lower()):
                body_end = offset
            continue

        tokens = _command_tokens(text)
        if not tokens:
            continue
        command = tokens[0].upper()
        if body_start is None and (
                command in ("G0", "G1", "G2", "G3", "M73")
                or command.startswith("PRINT_")):
            body_start = offset
        if command == "M73":
            # R is normal-mode minutes. S is the silent-mode twin; a Voron
            # does not print in silent mode, so S must not drive the bed.
            remaining_min = _word_value(tokens, "R")
            if remaining_min is not None and remaining_min >= 0.:
                m73_points.append((offset, remaining_min * 60.))

    if body_start is None:
        body_start = 0
    if body_end is None:
        body_end = file_end

    m73_points = _monotonic_points(m73_points)
    total = footer_total if footer_total is not None else cura_total

    if len(m73_points) >= 2:
        if total is None:
            total = m73_points[0][1]
        return Timeline("m73", total, m73_points, body_start, body_end)

    if cura_total is not None and len(elapsed_points) >= 2:
        points = []
        for mark_offset, elapsed in elapsed_points:
            points.append((mark_offset, max(0., cura_total - elapsed)))
        points = _monotonic_points(points)
        if len(points) >= 2:
            return Timeline("cura", cura_total, points, body_start, body_end)

    if total is not None and body_end > body_start:
        return Timeline("bytes", total, [], body_start, body_end)
    return Timeline("none", total, [], body_start, body_end)


def should_release(wall_remaining, slicer_consumed, total_seconds,
                   lead_time, min_elapsed):
    """True when the bed should turn off.

    wall_remaining is slicer time left divided by the live speed factor.
    slicer_consumed is slicer time already printed, so a long PRINT_START
    (heat soak, gantry level) does not count as progress through the part.
    """
    if wall_remaining is None or slicer_consumed is None:
        return False
    if total_seconds is not None and total_seconds < lead_time + min_elapsed:
        return False
    if slicer_consumed < min_elapsed:
        return False
    return wall_remaining <= lead_time


class Bedtime:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.heater_name = config.get("heater", "heater_bed")
        self.lead_time = config.getfloat("lead_time", 900., minval=0.)
        self.target_temp = config.getfloat("target_temp", 0., minval=0.)
        self.min_elapsed = config.getfloat("min_elapsed", 120., minval=0.)
        self.keep_off = config.getboolean("keep_off", True)
        self.enabled = config.getboolean("enabled", True)
        self.poll_interval = config.getfloat(
            "poll_interval", 1., minval=0.2, maxval=30.)
        self.heater = None
        self.sdcard = None
        self.print_stats = None
        self.gcode_move = None
        self.job_key = None
        self.timeline = None
        self.scanning = False
        self.triggered = False
        self.hold_noted = False
        self.last_wall = None
        self.state = "idle" if self.enabled else "disabled"
        self._generation = 0
        self._notes = set()
        self.gcode.register_command(
            "BEDTIME_STATUS", self.cmd_BEDTIME_STATUS,
            desc=self.cmd_BEDTIME_STATUS_help)
        self.gcode.register_command(
            "BEDTIME_ENABLE", self.cmd_BEDTIME_ENABLE,
            desc=self.cmd_BEDTIME_ENABLE_help)
        self.gcode.register_command(
            "BEDTIME_DISABLE", self.cmd_BEDTIME_DISABLE,
            desc=self.cmd_BEDTIME_DISABLE_help)
        self.gcode.register_command(
            "BEDTIME_NOW", self.cmd_BEDTIME_NOW,
            desc=self.cmd_BEDTIME_NOW_help)
        self.gcode.register_command(
            "BEDTIME_LEAD", self.cmd_BEDTIME_LEAD,
            desc=self.cmd_BEDTIME_LEAD_help)
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:ready",
                                            self._handle_ready)

    def _handle_connect(self):
        self.sdcard = self.printer.lookup_object("virtual_sdcard", None)
        self.print_stats = self.printer.lookup_object("print_stats", None)
        self.gcode_move = self.printer.lookup_object("gcode_move", None)
        if self.sdcard is None or self.print_stats is None:
            raise self.printer.config_error(
                "bedtime requires [virtual_sdcard]")
        pheaters = self.printer.lookup_object("heaters")
        # Raises config_error if the name is not a known heater. On a
        # Voron 2.4 the bed section is [heater_bed].
        self.heater = pheaters.lookup_heater(self.heater_name)

    def _handle_ready(self):
        self.reactor.register_timer(self._tick, self.reactor.NOW)

    def get_status(self, eventtime):
        total = None
        source = ""
        if self.timeline is not None:
            total = self.timeline.total_seconds
            source = self.timeline.source
        return {
            "enabled": self.enabled,
            "heater": self.heater_name,
            "lead_time": self.lead_time,
            "target_temp": self.target_temp,
            "keep_off": self.keep_off,
            "min_elapsed": self.min_elapsed,
            "state": self.state,
            "source": source,
            "total_time": total,
            "remaining": self.last_wall,
            "triggered": self.triggered,
        }

    def _note(self, key, message):
        if key in self._notes:
            return
        self._notes.add(key)
        self.gcode.respond_info(message)
        logging.info("bedtime: %s", message)

    def _heater_label(self):
        if self.heater_name in ("heater_bed", "bed"):
            return "bed"
        return self.heater_name

    def _classify(self):
        if not self.enabled and not self.triggered:
            return "disabled"
        if self.triggered:
            return "triggered"
        if self.scanning:
            return "scanning"
        timeline = self.timeline
        if timeline is None or self.job_key is None:
            return "idle"
        if timeline.source == "none":
            return "no_estimate"
        total = timeline.total_seconds
        if total is not None and total < self.lead_time + self.min_elapsed:
            return "too_short"
        return "armed"

    def _refresh_state(self):
        self.state = self._classify()

    def _clear_job(self):
        self._generation += 1
        self.job_key = None
        self.timeline = None
        self.scanning = False
        self.triggered = False
        self.hold_noted = False
        self.last_wall = None
        self._notes.clear()
        self._refresh_state()

    def _start_job(self, key, path):
        self._clear_job()
        self.job_key = key
        self.scanning = True
        self._refresh_state()
        generation = self._generation

        def worker():
            timeline = None
            error = None
            try:
                timeline = parse_gcode_file(path)
            except Exception as scan_error:
                logging.exception("bedtime scan failed")
                error = scan_error

            def deliver(eventtime):
                if generation != self._generation:
                    return
                self.scanning = False
                if error is not None or timeline is None:
                    self.timeline = Timeline("none", None, [], 0, 0)
                    self._note(
                        "scan-error",
                        "Bedtime: could not read the gcode file. "
                        "The bed will stay on.")
                else:
                    self.timeline = timeline
                    self._announce_plan()
                self._refresh_state()
            self.reactor.register_async_callback(deliver)

        threading.Thread(target=worker, name="bedtime", daemon=True).start()

    def _announce_plan(self):
        if not self.enabled:
            return
        label = self._heater_label()
        lead = format_duration(self.lead_time)
        timeline = self.timeline
        total = timeline.total_seconds
        total_text = format_duration(total) if total is not None else None
        if timeline.source == "none":
            self._note(
                "plan",
                "Bedtime: this file has no remaining-time marks, so the "
                "%s stays on. In OrcaSlicer leave 'Disable set remaining "
                "print time' unchecked. In PrusaSlicer or SuperSlicer "
                "enable 'Supports remaining times'." % (label,))
            return
        if self._classify() == "too_short":
            self._note(
                "plan",
                "Bedtime: this print (%s) is shorter than the lead (%s) "
                "plus the first-layer guard (%s), so the %s stays on."
                % (total_text, lead, format_duration(self.min_elapsed),
                   label))
            return
        if timeline.source == "bytes":
            self._note(
                "plan",
                "Bedtime: %s turns off %s before the end. Only a rough "
                "%s file-size estimate was found. Enable M73 remaining "
                "time in the slicer for a closer shutoff."
                % (label, lead, total_text))
            return
        via = "M73" if timeline.source == "m73" else "Cura layer times"
        estimate = total_text if total_text is not None else "unknown"
        self._note(
            "plan",
            "Bedtime: %s turns off %s before the end "
            "(slicer estimate %s, from %s)."
            % (label, lead, estimate, via))

    def _speed_factor(self, eventtime):
        if self.gcode_move is None:
            return 1.
        try:
            speed = float(
                self.gcode_move.get_status(eventtime)["speed_factor"])
        except Exception:
            logging.exception("bedtime speed factor")
            return 1.
        if speed <= 0.:
            return 1.
        return speed

    def _set_target(self, eventtime):
        status = self.heater.get_status(eventtime)
        if status["target"] <= self.target_temp + 0.05:
            return False
        self.heater.set_temp(self.target_temp)
        return True

    def _release(self, eventtime, wall_remaining):
        self.triggered = True
        label = self._heater_label()
        try:
            changed = self._set_target(eventtime)
        except self.printer.command_error as command_error:
            self.triggered = False
            self._note("release-error",
                       "Bedtime: could not set %s: %s"
                       % (self.heater_name, command_error))
            self._refresh_state()
            return
        self._refresh_state()
        if not changed:
            self._note(
                "release",
                "Bedtime: %s is already at %.0fC."
                % (label, self.target_temp))
            return
        self._note(
            "release",
            "Bedtime: %s set to %.0fC with about %s left, so the part "
            "can release. BEDTIME_DISABLE to let it heat again."
            % (label, self.target_temp, format_duration(wall_remaining)))

    def _hold(self, eventtime):
        if not self.keep_off or not self.triggered:
            return
        try:
            changed = self._set_target(eventtime)
        except self.printer.command_error:
            logging.exception("bedtime hold")
            return
        if changed and not self.hold_noted:
            self.hold_noted = True
            self.gcode.respond_info(
                "Bedtime: holding the %s at %.0fC. "
                "BEDTIME_DISABLE to leave it alone."
                % (self._heater_label(), self.target_temp))

    def _consider(self, eventtime):
        timeline = self.timeline
        if (timeline is None or timeline.source == "none"
                or not self.enabled):
            return
        remaining = timeline.remaining_at(self.sdcard.file_position)
        if remaining is None:
            return
        speed = self._speed_factor(eventtime)
        wall = remaining / speed
        self.last_wall = wall
        consumed = timeline.consumed_at(self.sdcard.file_position, remaining)
        if should_release(wall, consumed, timeline.total_seconds,
                          self.lead_time, self.min_elapsed):
            self._release(eventtime, wall)

    def _poll(self, eventtime):
        if self.printer.is_shutdown() or self.sdcard is None:
            return
        stats = self.print_stats.get_status(eventtime)
        state = stats.get("state")
        if state not in ("printing", "paused"):
            if self.job_key is not None:
                self._clear_job()
            return
        path = self.sdcard.file_path()
        if not path:
            return
        try:
            stat_result = os.stat(path)
        except OSError:
            logging.exception("bedtime stat")
            return
        mtime_ns = getattr(stat_result, "st_mtime_ns",
                           int(stat_result.st_mtime * 1e9))
        key = (path, stat_result.st_size, mtime_ns)
        if key != self.job_key:
            self._start_job(key, path)
            return
        # BEDTIME_DISABLE clears triggered, so a manual "off now" still
        # holds the target until disarm even if early shutoff is off.
        if self.triggered and self.keep_off:
            self._hold(eventtime)
        if state != "printing" or self.triggered or not self.enabled:
            self._refresh_state()
            return
        self._consider(eventtime)
        self._refresh_state()

    def _tick(self, eventtime):
        try:
            self._poll(eventtime)
        except Exception:
            logging.exception("bedtime poll")
        return eventtime + self.poll_interval

    cmd_BEDTIME_STATUS_help = "Show when the bed will turn off"

    def cmd_BEDTIME_STATUS(self, gcmd):
        label = self._heater_label()
        lead = format_duration(self.lead_time)
        target = "%.0fC" % (self.target_temp,)
        state = self._classify()
        detail = {
            "idle": "no print is running",
            "disabled": "early shutoff is off",
            "scanning": "reading the gcode file",
            "armed": "waiting for the lead time",
            "triggered": "heater is at the release target",
            "no_estimate": "this file has no remaining-time marks",
            "too_short": "this print is too short to cool early",
        }.get(state, state)
        remaining = ""
        if self.last_wall is not None and state in ("armed", "triggered"):
            remaining = ", about %s left" % (format_duration(self.last_wall),)
        source = ""
        if self.timeline is not None and self.timeline.source not in (
                "", "none"):
            source = " (%s)" % (self.timeline.source,)
        gcmd.respond_info(
            "Bedtime: %s. %s turns off %s early, target %s%s%s."
            % (detail, label, lead, target, remaining, source))

    cmd_BEDTIME_ENABLE_help = "Arm early bed shutoff"

    def cmd_BEDTIME_ENABLE(self, gcmd):
        self.enabled = True
        if self.timeline is not None and "plan" not in self._notes:
            self._announce_plan()
        self._refresh_state()
        gcmd.respond_info(
            "Bedtime: early shutoff on. The %s turns off %s before the end."
            % (self._heater_label(), format_duration(self.lead_time)))

    cmd_BEDTIME_DISABLE_help = "Keep the bed hot until the print ends"

    def cmd_BEDTIME_DISABLE(self, gcmd):
        self.enabled = False
        self.triggered = False
        self.hold_noted = False
        self._refresh_state()
        gcmd.respond_info(
            "Bedtime: early shutoff off. The %s target was not changed."
            % (self._heater_label(),))

    cmd_BEDTIME_NOW_help = "Turn the bed off now"

    def cmd_BEDTIME_NOW(self, gcmd):
        if self.heater is None:
            raise gcmd.error("Bedtime is not ready")
        self.triggered = True
        self.hold_noted = False
        try:
            self._set_target(self.reactor.monotonic())
        except self.printer.command_error as command_error:
            self.triggered = False
            raise gcmd.error(str(command_error))
        self._refresh_state()
        gcmd.respond_info(
            "Bedtime: %s set to %.0fC."
            % (self._heater_label(), self.target_temp))

    cmd_BEDTIME_LEAD_help = "Set or show the early shutoff lead time"

    def cmd_BEDTIME_LEAD(self, gcmd):
        if "MINUTES" in gcmd.params:
            self.lead_time = gcmd.get_float("MINUTES", minval=0.) * 60.
        elif "SECONDS" in gcmd.params:
            self.lead_time = gcmd.get_float("SECONDS", minval=0.)
        self._refresh_state()
        gcmd.respond_info(
            "Bedtime: lead time is %s. "
            "BEDTIME_LEAD MINUTES=5 to change it."
            % (format_duration(self.lead_time),))


def load_config(config):
    return Bedtime(config)
