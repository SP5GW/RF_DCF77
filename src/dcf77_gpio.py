#!/usr/bin/env python3
"""Read and decode DCF77 time signal on Raspberry Pi via GPIO.

How it works:
- Every second the transmitter sends a pulse: ~100 ms means bit 0, ~200 ms means bit 1.
- In second 59 (minute marker) there is no pulse.
- After collecting 59 bits, time/date fields are decoded (BCD + parity bits).

The script supports common DCF77 receiver modules connected to GPIO.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

try:
    import RPi.GPIO as GPIO
except ModuleNotFoundError:  # allows running --simulate mode on a regular PC
    GPIO = None


@dataclass
class DecodedDCF77:
    datetime_local: datetime
    timezone_name: str
    raw_bits: List[int]


class DCF77Decoder:
    """Decoder for a single DCF77 frame (59 bits).

    System-level role:
    - stores one full minute frame (59 payload bits)
    - decodes BCD fields (minute/hour/date)
    - validates parity groups P1/P2/P3 before returning a datetime
    """

    def __init__(self) -> None:
        """Initialize an empty bit buffer for one DCF77 frame."""
        self.bits: List[int] = []

    def reset(self) -> None:
        """Clear currently buffered frame bits."""
        self.bits.clear()

    def add_bit(self, bit: int) -> None:
        """Append one decoded bit (0/1) to the frame buffer."""
        if bit not in (0, 1):
            raise ValueError("Bit must be either 0 or 1")
        self.bits.append(bit)

    def is_complete(self) -> bool:
        """Return True when exactly 59 DCF77 payload bits are buffered."""
        return len(self.bits) == 59

    @staticmethod
    def _bcd_value(bits: List[int], positions: List[int], weights: List[int]) -> int:
        """Decode a BCD-like numeric field from selected bit positions."""
        return sum(bits[pos] * weight for pos, weight in zip(positions, weights))

    @staticmethod
    def _check_even_parity(bits: List[int], start: int, end: int, parity_pos: int) -> bool:
        """Check even parity for a bit range plus its parity bit."""
        # Even parity: data bits + parity bit must contain an even number of 1s.
        ones = sum(bits[start : end + 1]) + bits[parity_pos]
        return ones % 2 == 0

    def decode(self) -> Optional[DecodedDCF77]:
        """Validate and decode a complete frame into date/time information."""
        if not self.is_complete():
            return None

        b = self.bits

        # Start bit for civil time information.
        if b[20] != 1:
            return None

        # Minutes (21..27), parity P1 (28)
        minute = self._bcd_value(b, [21, 22, 23, 24, 25, 26, 27], [1, 2, 4, 8, 10, 20, 40])
        if not self._check_even_parity(b, 21, 27, 28):
            return None

        # Hours (29..34), parity P2 (35)
        hour = self._bcd_value(b, [29, 30, 31, 32, 33, 34], [1, 2, 4, 8, 10, 20])
        if not self._check_even_parity(b, 29, 34, 35):
            return None

        # Date: day (36..41), weekday (42..44), month (45..49), year (50..57), P3 (58)
        day = self._bcd_value(b, [36, 37, 38, 39, 40, 41], [1, 2, 4, 8, 10, 20])
        _weekday = self._bcd_value(b, [42, 43, 44], [1, 2, 4])
        month = self._bcd_value(b, [45, 46, 47, 48, 49], [1, 2, 4, 8, 10])
        year_2d = self._bcd_value(
            b,
            [50, 51, 52, 53, 54, 55, 56, 57],
            [1, 2, 4, 8, 10, 20, 40, 80],
        )
        if not self._check_even_parity(b, 36, 57, 58):
            return None

        # Daylight saving / standard time flags.
        cest = b[17] == 1
        cet = b[18] == 1
        timezone_name = "CEST" if cest and not cet else "CET"

        try:
            decoded_dt = datetime(year=2000 + year_2d, month=month, day=day, hour=hour, minute=minute)
        except ValueError:
            return None

        return DecodedDCF77(decoded_dt, timezone_name, list(self.bits))


class DCF77GPIOReceiver:
    """Receive DCF77 pulses from GPIO pin and decode frames.

    System-level role:
    1) Acquisition layer: get pulse edges from GPIO (or simulation feeder).
    2) Frame assembly layer: convert pulse length -> bit and append to frame.
    3) Trigger decode when minute marker gap is detected.
    """

    def __init__(
        self,
        pin: int,
        active_low: bool = True,
        zero_threshold_ms: float = 150.0,
        marker_gap_s: float = 1.5,
    ) -> None:
        """Store receiver configuration and initialize runtime state."""
        self.pin = pin
        self.active_low = active_low
        self.zero_threshold_ms = zero_threshold_ms
        self.marker_gap_s = marker_gap_s

        self.decoder = DCF77Decoder()
        self.pulse_start: Optional[float] = None
        self.last_rising: Optional[float] = None
        self.running = False

    @staticmethod
    def _bit_group_name(bit_index_0based: int) -> str:
        """Return human-readable DCF77 field name for a 0-based bit index."""
        # DCF77 bit map (0-based indexing in this script):
        #  0..14 reserved/weather, 15 call, 16 time shift, 17..18 TZ, 19 leap
        # 20 start, 21..27 minute, 28 P1, 29..34 hour, 35 P2,
        # 36..57 date block, 58 P3
        idx = bit_index_0based
        if idx <= 14:
            return "reserved/weather"
        if idx == 15:
            return "call bit"
        if idx == 16:
            return "time shift announcement"
        if idx in (17, 18):
            return "timezone (CEST/CET)"
        if idx == 19:
            return "leap second announcement"
        if idx == 20:
            return "time info start"
        if 21 <= idx <= 27:
            return "minutes"
        if idx == 28:
            return "parity P1 (minutes)"
        if 29 <= idx <= 34:
            return "hours"
        if idx == 35:
            return "parity P2 (hours)"
        if 36 <= idx <= 41:
            return "day of month"
        if 42 <= idx <= 44:
            return "weekday"
        if 45 <= idx <= 49:
            return "month"
        if 50 <= idx <= 57:
            return "year"
        if idx == 58:
            return "parity P3 (date)"
        return "unknown"

    def setup(self) -> None:
        """Configure GPIO pin and register edge callback in hardware mode."""
        if GPIO is None:
            raise RuntimeError("RPi.GPIO is not available. Use --simulate on a PC.")

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.IN, pull_up_down=GPIO.PUD_UP if self.active_low else GPIO.PUD_DOWN)

        GPIO.add_event_detect(
            self.pin,
            GPIO.BOTH,
            callback=self._edge_callback,
            bouncetime=5,
        )

    def cleanup(self) -> None:
        """Release GPIO resources if GPIO backend is active."""
        if GPIO is not None:
            GPIO.cleanup()

    def _is_active_level(self, level: int) -> bool:
        """Check whether current pin level means an active pulse state."""
        return (level == GPIO.LOW) if self.active_low else (level == GPIO.HIGH)

    def _edge_callback(self, channel: int) -> None:
        """Handle GPIO edge events and convert pulse to duration in ms."""
        now = time.monotonic()
        level = GPIO.input(channel)

        if self._is_active_level(level):
            # Pulse start edge.
            self.pulse_start = now
            return

        # Pulse end edge.
        if self.pulse_start is None:
            return

        pulse_ms = (now - self.pulse_start) * 1000.0
        self.pulse_start = None

        self._process_pulse(pulse_ms, now)

    def _process_pulse(self, pulse_ms: float, now: float) -> None:
        """Process a finished pulse (GPIO or simulated)."""

        if self.last_rising is not None:
            gap = now - self.last_rising
            if gap > self.marker_gap_s:
                # Missing pulse in second 59 -> end of frame.
                # This is the frame-boundary event in DCF77 minute structure.
                self._finalize_frame()

        self.last_rising = now

        bit = 0 if pulse_ms < self.zero_threshold_ms else 1
        # Pulse width model:
        # ~100 ms => bit 0
        # ~200 ms => bit 1
        self.decoder.add_bit(bit)
        bit_idx = len(self.decoder.bits) - 1
        group = self._bit_group_name(bit_idx)
        print(f"Bit {bit_idx:02d} [{group:<24}] : {bit} ({pulse_ms:.1f} ms)")

    def _finalize_frame(self) -> None:
        """Close current frame, decode if complete, print result, then reset."""
        if not self.decoder.bits:
            return

        print(f"\nMinute marker detected. Frame length: {len(self.decoder.bits)} bits")
        if self.decoder.is_complete():
            decoded = self.decoder.decode()
            if decoded:
                print(
                    "Decoded DCF77 time:",
                    decoded.datetime_local.strftime("%Y-%m-%d %H:%M"),
                    decoded.timezone_name,
                )
            else:
                print("Frame complete, but validation failed (parity/range).")
        else:
            print("Incomplete frame - discarded.")

        self.decoder.reset()

    def run(self) -> None:
        """Run receiver loop until interrupted by SIGINT/SIGTERM."""
        self.running = True

        def _stop(_sig: int, _frame: object) -> None:
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        print("DCF77 listening started. Press Ctrl+C to stop.")
        while self.running:
            time.sleep(0.2)

        self._finalize_frame()


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse command-line arguments for hardware or simulation mode."""
    parser = argparse.ArgumentParser(description="Read DCF77 signal on Raspberry Pi (GPIO).")
    parser.add_argument("--pin", type=int, default=17, help="BCM pin number (default: 17)")
    parser.add_argument(
        "--active-high",
        action="store_true",
        help="Set if your module outputs active-high pulses (default is active-low)",
    )
    parser.add_argument(
        "--zero-threshold-ms",
        type=float,
        default=150.0,
        help="Threshold in milliseconds to distinguish bit 0/1 (default: 150)",
    )
    parser.add_argument(
        "--marker-gap-s",
        type=float,
        default=1.5,
        help="Gap treated as minute marker in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run DCF77 simulation mode without GPIO (useful on PC)",
    )
    parser.add_argument(
        "--simulate-datetime",
        type=str,
        default="2026-01-15T12:34",
        help="Date/time to simulate, format YYYY-MM-DDTHH:MM",
    )
    parser.add_argument(
        "--simulate-speed",
        type=float,
        default=20.0,
        help="Simulation speed multiplier (e.g. 20 = 20x faster than realtime)",
    )
    return parser.parse_args(argv)


def _set_bcd(bits: List[int], positions: List[int], weights: List[int], value: int) -> None:
    """Encode integer value into selected bit positions using BCD weights."""
    for pos in positions:
        bits[pos] = 0

    for pos, weight in sorted(zip(positions, weights), key=lambda item: item[1], reverse=True):
        if value >= weight:
            bits[pos] = 1
            value -= weight


def _is_last_sunday(year: int, month: int, day: int, weekday_iso: int) -> bool:
    """Return True if a given date is the last Sunday of the month."""
    if weekday_iso != 7:
        return False
    return day + 7 > 31


def _is_cest_for_date(dt: datetime) -> bool:
    """Approximate EU DST rule for DCF77: active from late March to late October.

    Note: this is date-level approximation for simulation purposes.
    Real DCF77 timezone transitions are defined precisely around transition times.
    """
    m = dt.month
    if 4 <= m <= 9:
        return True
    if m <= 2 or m >= 11:
        return False

    # March / October boundary by last Sunday rule (date-level approximation).
    if m == 3:
        if _is_last_sunday(dt.year, 3, dt.day, dt.isoweekday()):
            return True
        return dt.day > 24 and dt.isoweekday() != 7

    # m == 10
    if _is_last_sunday(dt.year, 10, dt.day, dt.isoweekday()):
        return False
    return dt.day < 25 or dt.isoweekday() != 7


def build_simulated_frame(dt: datetime) -> List[int]:
    """Build a valid 59-bit DCF77 frame for the given date/time.

    System-level intent:
    - generate a complete frame that passes the same decoder/validation path
    - set timezone flags (CET/CEST), encode BCD fields, and compute parity bits
    """
    bits = [0] * 59
    bits[20] = 1  # time info start bit

    # Time zone flags based on approximate EU DST date rules.
    cest = _is_cest_for_date(dt)
    bits[17] = 1 if cest else 0
    bits[18] = 0 if cest else 1

    minute_positions = [21, 22, 23, 24, 25, 26, 27]
    hour_positions = [29, 30, 31, 32, 33, 34]
    day_positions = [36, 37, 38, 39, 40, 41]
    weekday_positions = [42, 43, 44]
    month_positions = [45, 46, 47, 48, 49]
    year_positions = [50, 51, 52, 53, 54, 55, 56, 57]

    _set_bcd(bits, minute_positions, [1, 2, 4, 8, 10, 20, 40], dt.minute)
    _set_bcd(bits, hour_positions, [1, 2, 4, 8, 10, 20], dt.hour)
    _set_bcd(bits, day_positions, [1, 2, 4, 8, 10, 20], dt.day)
    _set_bcd(bits, weekday_positions, [1, 2, 4], dt.isoweekday())
    _set_bcd(bits, month_positions, [1, 2, 4, 8, 10], dt.month)
    _set_bcd(bits, year_positions, [1, 2, 4, 8, 10, 20, 40, 80], dt.year % 100)

    bits[28] = sum(bits[21:28]) % 2
    bits[35] = sum(bits[29:35]) % 2
    bits[58] = sum(bits[36:58]) % 2

    return bits


def run_simulation(args: argparse.Namespace) -> int:
    """Replay a synthetic DCF77 frame through the same pulse-processing path."""
    # Simulation feeds synthetic pulses into the same _process_pulse() path
    # used by GPIO callback, so decode behavior is exercised end-to-end.
    try:
        simulated_dt = datetime.strptime(args.simulate_datetime, "%Y-%m-%dT%H:%M")
    except ValueError:
        print("Invalid --simulate-datetime format. Use YYYY-MM-DDTHH:MM", file=sys.stderr)
        return 2

    receiver = DCF77GPIOReceiver(
        pin=args.pin,
        active_low=not args.active_high,
        zero_threshold_ms=args.zero_threshold_ms,
        marker_gap_s=args.marker_gap_s,
    )
    frame = build_simulated_frame(simulated_dt)

    print("DCF77 simulation mode started (no GPIO).")
    print(f"Simulated date/time: {simulated_dt.strftime('%Y-%m-%d %H:%M')}")

    now = time.monotonic()
    sleep_scale = args.simulate_speed if args.simulate_speed > 0 else 1.0

    for bit in frame:
        pulse_ms = 100.0 if bit == 0 else 200.0
        receiver._process_pulse(pulse_ms, now)
        now += 1.0
        time.sleep(1.0 / sleep_scale)

    # Simulate missing pulse in second 59 -> minute marker.
    now += args.marker_gap_s + 0.1
    receiver._finalize_frame()
    return 0


def main(argv: List[str]) -> int:
    """Program entrypoint: run simulation or real GPIO receiver."""
    args = parse_args(argv)

    if args.simulate:
        return run_simulation(args)

    receiver = DCF77GPIOReceiver(
        pin=args.pin,
        active_low=not args.active_high,
        zero_threshold_ms=args.zero_threshold_ms,
        marker_gap_s=args.marker_gap_s,
    )

    try:
        receiver.setup()
        receiver.run()
        return 0
    finally:
        receiver.cleanup()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))