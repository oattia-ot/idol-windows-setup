"""
Structured logging with console + file output.
Uses rich when available for colored, readable output; falls back to plain print.

Long detail strings (paths, multi-part status) are split across lines so the
installer console stays readable instead of wrapping mid-path.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_log_file: Optional[Path] = None
_min_level = logging.INFO
_use_rich = False

try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.theme import Theme

    _console = Console(
        theme=Theme(
            {
                "info": "cyan",
                "success": "bold green",
                "warning": "bold #FF8C00",  # true orange
                "error": "bold red",
                "debug": "dim",
            }
        )
    )
    _use_rich = True
except ImportError:
    _console = None


def _split_detail(detail: str) -> List[str]:
    """
    Break a long detail string into readable lines.
    Splits on '; ' first (common for multi-part status), then soft-wraps
    very long remaining segments at path-friendly boundaries.
    """
    if not detail:
        return []
    # Prefer semantic breaks
    parts = [p.strip() for p in re.split(r";\s*", detail) if p.strip()]
    lines: List[str] = []
    for part in parts:
        if len(part) <= 100:
            lines.append(part)
            continue
        # Soft-wrap long paths / sentences at backslash or space near 90 chars
        remaining = part
        while len(remaining) > 100:
            cut = -1
            window = remaining[:100]
            for sep in ("\\", "/", " ", "-"):
                idx = window.rfind(sep)
                if idx > 40:
                    cut = idx + (0 if sep in (" ",) else 1)
                    break
            if cut <= 0:
                cut = 100
            lines.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            lines.append(remaining)
    return lines


class KDLogger:
    """Thin wrapper around stdlib logging + optional rich."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("kd-installer")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        self.logger.propagate = False

    def initialize(self, log_directory: str | Path, min_level: str = "INFO") -> Path:
        global _log_file, _min_level
        log_dir = Path(log_directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        _log_file = log_dir / f"kd-install-{stamp}.log"
        _min_level = getattr(logging, min_level.upper(), logging.INFO)

        # File handler (always plain)
        fh = logging.FileHandler(_log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.logger.addHandler(fh)

        # Console handler
        if _use_rich:
            rh = RichHandler(
                console=_console,
                show_time=True,
                show_path=False,
                rich_tracebacks=True,
                markup=True,
            )
            rh.setLevel(_min_level)
            self.logger.addHandler(rh)
        else:
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(_min_level)
            ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            self.logger.addHandler(ch)

        self.info(f"Log file: {_log_file}")
        return _log_file

    def _before_log(self) -> None:
        """End any live status line so the next log row starts on a new line."""
        try:
            status_line_end()
        except Exception:
            pass

    def debug(self, msg: str) -> None:
        self._before_log()
        self.logger.debug(msg)

    def info(self, msg: str) -> None:
        self._before_log()
        # Section / phase headers (e.g. "=== Phase 1: ...") in bold yellow
        if _use_rich and msg.startswith("==="):
            self.logger.info(f"[bold yellow]{msg}[/bold yellow]")
        else:
            self.logger.info(msg)

    def info_orange(self, msg: str) -> None:
        """INFO-level line with the entire message in orange (rich) or plain."""
        self._before_log()
        if _use_rich:
            self.logger.info(f"[bold #FF8C00]{msg}[/bold #FF8C00]")
        else:
            self.logger.info(msg)

    def success(self, msg: str) -> None:
        self._before_log()
        if _use_rich:
            self.logger.info(f"[success]{msg}[/success]")
        else:
            self.logger.info(f"[OK] {msg}")

    def warn(self, msg: str) -> None:
        self._before_log()
        self.logger.warning(msg)

    def error(self, msg: str) -> None:
        self._before_log()
        self.logger.error(msg)

    def step_result(
        self,
        step: str,
        success: bool,
        detail: str = "",
        *,
        warning: bool = False,
    ) -> None:
        """
        Log a step outcome. Long multi-part details are printed on following
        indented lines so paths and status fragments stay readable.

        When ``warning=True`` (soft success / degraded), the headline is
        ``[WARN]`` and the message is emitted in orange via ``warn`` instead
        of green OK or red FAILED.
        """
        if warning:
            status = "WARN"
        elif success:
            status = "OK"
        else:
            status = "FAILED"
        headline = f"[{status}] {step}"
        detail_lines = _split_detail(detail)

        def _emit(msg: str) -> None:
            if warning:
                self.warn(msg)
            elif success:
                self.success(msg)
            else:
                self.error(msg)

        if not detail_lines:
            _emit(headline)
            return

        # Single short detail stays on one line
        if len(detail_lines) == 1 and len(detail_lines[0]) <= 100:
            _emit(f"{headline} - {detail_lines[0]}")
            return

        # Multi-line: headline, then indented detail rows
        _emit(headline)
        for line in detail_lines:
            self.info(f"         {line}")


def format_elapsed(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS for console clock display."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Single-line live status (overwrite in place with \\r)
# ---------------------------------------------------------------------------
_status_lock = threading.Lock()
_status_width = 0  # last printed width, for clearing


def status_line(msg: str) -> None:
    """
    Rewrite one console status line in place (carriage return).
    Does not append a newline, so only the time / progress text changes.
    Safe to call from background threads (clock, NiFi wait).
    """
    global _status_width
    text = (msg or "").replace("\r", " ").replace("\n", " ").rstrip()
    # Prefer raw stdout so rich handlers do not force a new log line
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            pad = max(0, _status_width - len(text))
            stream.write("\r" + text + (" " * pad))
            stream.flush()
            _status_width = max(len(text), _status_width)
        except Exception:
            pass


def status_line_clear() -> None:
    """
    Erase the in-place status line and park the cursor at column 0
    (same line). Prefer status_line_end() before normal log output so
    the next message starts on a new line.
    """
    global _status_width
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            if _status_width > 0:
                stream.write("\r" + (" " * _status_width) + "\r")
                stream.flush()
            _status_width = 0
        except Exception:
            pass


def status_line_end() -> None:
    """
    End the live status line so the next print/log starts on a fresh line.
    Clears residual characters, then writes a newline when a status was active.
    """
    global _status_width
    stream = getattr(sys, "__stdout__", None) or sys.stdout
    with _status_lock:
        try:
            if _status_width > 0:
                stream.write("\r" + (" " * _status_width) + "\r\n")
                stream.flush()
                _status_width = 0
        except Exception:
            pass


def status_line_finish(final_msg: str = "") -> None:
    """End the live line, then print a normal finished message with newline."""
    status_line_end()
    if final_msg:
        stream = getattr(sys, "__stdout__", None) or sys.stdout
        try:
            stream.write(final_msg.rstrip() + "\n")
            stream.flush()
        except Exception:
            pass




class ElapsedClock:
    """
    Background elapsed-time clock with optional progress-based ETA.

    Usage::

        with ElapsedClock("Install", interval=15, total_units=10) as clock:
            for item in items:
                work(item)
                clock.advance(1)   # or clock.set_progress(done, total)

    Ticks every ``interval`` seconds, e.g.::

        [clock] Install still running... elapsed 2:45 | 3/10 components | ~ETA 6:25 left

    ETA = elapsed * (total/done - 1) once at least one unit is complete.
    """

    def __init__(
        self,
        label: str = "Setup",
        interval: float = 15.0,
        total_units: int = 0,
        unit_label: str = "steps",
    ) -> None:
        self.label = label or "Setup"
        self.interval = max(5.0, float(interval))
        self.unit_label = unit_label or "steps"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0: float = 0.0
        self._lock = threading.Lock()
        self._done: float = 0.0
        self._total: float = float(max(0, total_units))
        self._current_task: str = ""

    def __enter__(self) -> "ElapsedClock":
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kd-clock-{self.label}",
            daemon=True,
        )
        self._thread.start()
        total_note = (
            f", {int(self._total)} {self.unit_label} planned"
            if self._total > 0
            else ""
        )
        log.info(
            f"  [clock] {self.label} started (clock every {int(self.interval)}s{total_note})"
        )
        return self

    @property
    def elapsed_seconds(self) -> float:
        if not self._t0:
            return 0.0
        return time.monotonic() - self._t0

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = float(max(0, total))

    def set_progress(self, done: float, total: Optional[float] = None) -> None:
        """Set absolute progress (e.g. components finished / total)."""
        with self._lock:
            self._done = max(0.0, float(done))
            if total is not None:
                self._total = float(max(0, total))

    def advance(self, units: float = 1.0) -> None:
        with self._lock:
            self._done = max(0.0, self._done + float(units))

    def set_task(self, name: str) -> None:
        """Optional label for the current unit of work (shown on ticks)."""
        with self._lock:
            self._current_task = (name or "").strip()

    def eta_seconds(self) -> Optional[float]:
        """
        Estimate seconds remaining from linear progress.
        Needs done > 0 and total > done; returns None if unknown.
        """
        with self._lock:
            done, total = self._done, self._total
        if done <= 0 or total <= 0 or done >= total:
            return 0.0 if total > 0 and done >= total else None
        elapsed = self.elapsed_seconds
        if elapsed < 1.0:
            return None
        # elapsed / done = rate per unit → remaining units * rate
        remaining_units = total - done
        return (elapsed / done) * remaining_units

    @staticmethod
    def _fmt_progress(done: float, total: float) -> str:
        if total <= 0:
            return ""
        pct = min(100.0, (done / total) * 100.0)
        # show integers when values are whole
        if abs(done - round(done)) < 1e-6 and abs(total - round(total)) < 1e-6:
            return f"{int(round(done))}/{int(round(total))} ({pct:.0f}%)"
        return f"{done:.1f}/{total:.0f} ({pct:.0f}%)"

    def _tick_message(self) -> str:
        elapsed = self.elapsed_seconds
        with self._lock:
            done, total = self._done, self._total
            task = self._current_task
        parts = [
            f"  [clock] {self.label} still running... elapsed {format_elapsed(elapsed)}"
        ]
        if total > 0:
            parts.append(self._fmt_progress(done, total))
            parts.append(self.unit_label)
        eta = self.eta_seconds()
        if eta is not None and eta > 0:
            parts.append(f"~ETA {format_elapsed(eta)} left")
        elif total > 0 and done >= total:
            parts.append("ETA complete")
        elif total > 0 and done <= 0:
            parts.append("ETA calculating...")
        if task:
            parts.append(f"now: {task}")
        head, *rest = parts
        if not rest:
            return head
        return head + " | " + " | ".join(rest)

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None
        elapsed = time.monotonic() - self._t0
        status = "failed" if exc_type else "finished"
        with self._lock:
            done, total = self._done, self._total
        progress = ""
        if total > 0:
            progress = f" | {self._fmt_progress(done, total)}"
        # End live line, then one permanent log line on a new row
        status_line_end()
        log.info(
            f"  [clock] {self.label} {status} - total elapsed {format_elapsed(elapsed)}{progress}"
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                # Single updating line (no new log rows every 15s)
                status_line(self._tick_message())
            except Exception:
                pass




# Singleton used by the rest of the package
log = KDLogger()

