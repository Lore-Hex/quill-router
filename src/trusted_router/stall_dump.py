"""Nonblocking stack dumps and an opt-in, GIL-independent early stall sampler.

The router's C faulthandler timer remains the exit backstop. A separate, small
Python process requests earlier dumps using faulthandler's C SIGUSR2 handler;
unlike a Python sampling thread, this still works when a native RPC holds the
GIL. Only timestamps and fixed phase codes cross the pipe, never request data.
Importing this module has no watchdog or startup side effects.
"""
from __future__ import annotations

import _thread
import faulthandler
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import CodeType

DIAGNOSTIC_FAILURES = 0
DROPPED_DIAGNOSTIC_FAILURES = 0
_signal_lock = threading.Lock()
_signal_owner: LoopStallDiagnostics | None = None

SAMPLE_SECONDS = (5.0, 15.0)
PULSE_SECONDS = 1.0
_BEAT = struct.Struct("!cd")


def _write(fd: int, data: bytes) -> bool:
    try:
        return os.write(fd, data) == len(data)
    except OSError:
        return False


def diagnostic_failure(fd: int) -> None:
    """No logging handlers, exception text, or writes to a blocking descriptor."""
    global DIAGNOSTIC_FAILURES, DROPPED_DIAGNOSTIC_FAILURES
    DIAGNOSTIC_FAILURES += 1
    try:
        if not os.get_blocking(fd) and _write(fd, b"serve.stall_diagnostics_unavailable\n"):
            return
    except OSError:
        pass
    DROPPED_DIAGNOSTIC_FAILURES += 1


class LoopStallDiagnostics:
    """Off-loop setup, pulse and cleanup; the serving loop only publishes a tuple.

    A native daemon bootstrap has no Thread.start() handshake on the loop.
    It owns partial allocations until the pulse thread starts, then transfers
    them to that thread. stop() only sets an event, including during Popen.
    SIGUSR2 registration is serialized off-loop and cannot be undone by another
    owner's failure. A successful helper retains its C handler after shutdown
    for signals already in flight; a subsequent finished owner's replacement
    can safely register the new dump descriptor.
    """

    def __init__(
        self, fd: int, stall_seconds: float, *, watchdog_code: CodeType | None = None,
    ) -> None:
        self._dump_fd = fd
        self._stall_seconds = stall_seconds
        self._watchdog_code = watchdog_code
        self._loop_thread = threading.get_ident()
        self._last_tick = (b"T", time.monotonic())
        self._stopped = threading.Event()
        self._ready = threading.Event()
        self._startup_finished = threading.Event()
        self._finished = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self.dropped_records = 0
        # Unlike Thread.start(), this does not wait for Python thread bootstrap.
        _thread.start_new_thread(self._start, ())

    def beat(self, phase: bytes = b"T") -> None:
        if not self._stopped.is_set():
            self._last_tick = (phase, time.monotonic())

    def stop(self) -> None:
        self._stopped.set()

    def _register_signal(self) -> None:
        global _signal_owner
        with _signal_lock:
            if _signal_owner is not None and not _signal_owner._finished.is_set():
                raise RuntimeError("stall sampler already owns SIGUSR2")
            faulthandler.register(signal.SIGUSR2, file=self._dump_fd, all_threads=True)
            _signal_owner = self

    def _release_failed_signal(self) -> None:
        global _signal_owner
        with _signal_lock:
            if _signal_owner is self:
                faulthandler.unregister(signal.SIGUSR2)
                _signal_owner = None

    def _close(self, fd: int | None) -> None:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                diagnostic_failure(self._dump_fd)

    def _reap(self) -> None:
        if self._process is not None:
            try:
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            except BaseException:
                # This thread must never invoke threading.excepthook/stderr.
                diagnostic_failure(self._dump_fd)

    def _start(self) -> None:
        read_fd = pulse_fd = None
        transferred = False
        registered = False
        try:
            if self._stopped.is_set():
                return
            thresholds = [value for value in SAMPLE_SECONDS if value < self._stall_seconds]
            if not thresholds or os.get_blocking(self._dump_fd):
                raise ValueError("no safe early sampling destination or threshold")
            read_fd, self._fd = os.pipe()
            os.set_blocking(self._fd, False)
            pulse_fd = os.dup(self._fd)
            self._register_signal()
            registered = True
            if self._stopped.is_set():
                return
            self._process = subprocess.Popen(  # noqa: S603 - fixed isolated helper, no shell
                [sys.executable, "-I", str(Path(__file__).resolve()),
                 str(os.getpid()), str(PULSE_SECONDS), *(str(value) for value in thresholds)],
                stdin=read_fd, stdout=subprocess.DEVNULL, stderr=self._dump_fd,
                close_fds=True,
            )
            if self._stopped.is_set():
                return
            self._thread = threading.Thread(
                target=self._pulse, args=(pulse_fd,), name="loop-stall-pulse", daemon=True,
            )
            self._thread.start()
            transferred = True
            self._ready.set()
        except BaseException:
            diagnostic_failure(self._dump_fd)
        finally:
            self._close(read_fd)
            if not transferred:
                self._close(pulse_fd)
                self._close(self._fd)
                self._reap()
                if registered and self._process is None:
                    self._release_failed_signal()
            self._startup_finished.set()
            if not transferred:
                self._finished.set()

    def _phase(self, fallback: bytes) -> bytes:
        # Observe the fixed watchdog wrapper off-loop. No pre-arm diagnostic
        # call is needed, and no locals/source/request data are read or sent.
        frame = sys._current_frames().get(self._loop_thread)
        try:
            while frame is not None:
                if frame.f_code is self._watchdog_code:
                    return b"A"
                frame = frame.f_back
            return fallback
        finally:
            del frame

    def _pulse(self, fd: int) -> None:
        try:
            assert self._process is not None and self._fd is not None
            while not self._stopped.is_set() and self._process.poll() is None:
                phase, timestamp = self._last_tick
                if not _write(self._fd, _BEAT.pack(self._phase(phase), timestamp)):
                    self.dropped_records += 1
                if not _write(fd, _BEAT.pack(b"P", time.monotonic())):
                    self.dropped_records += 1
                self._stopped.wait(PULSE_SECONDS)
        except BaseException:
            diagnostic_failure(self._dump_fd)
        finally:
            self._close(fd)
            self._close(self._fd)
            self._reap()
            self._startup_finished.wait()
            self._finished.set()


def _main_wait(parent_pid: int) -> str:
    try:
        value = Path(f"/proc/{parent_pid}/wchan").read_text().strip()
        return value if value and all(c.isalnum() or c == "_" for c in value) else "unknown"
    except OSError:
        return "unavailable"


def _sample_parent(parent_pid: int, pulse_seconds: float, thresholds: tuple[float, ...]) -> None:
    """Child entry: no app imports, Python logging, RPCs, or parent GIL needed."""
    pending = b""
    last_tick: float | None = None
    last_pulse = time.monotonic()
    phase = "tick"
    emitted: set[float] = set()
    while os.getppid() == parent_pid:
        readable, _, _ = select.select([0], [], [], 0.1)
        if readable:
            chunk = os.read(0, 4096)
            if not chunk:
                return
            pending += chunk
            while len(pending) >= _BEAT.size:
                kind, timestamp = _BEAT.unpack(pending[:_BEAT.size])
                pending = pending[_BEAT.size:]
                if kind == b"S":
                    return
                if kind == b"P":
                    last_pulse = timestamp
                else:
                    if timestamp != last_tick:
                        emitted.clear()
                    last_tick = timestamp
                    phase = "rearm" if kind == b"A" else "tick"
        if last_tick is None:
            continue
        now = time.monotonic()
        age = now - last_tick
        for threshold in thresholds:
            if age < threshold or threshold in emitted:
                continue
            emitted.add(threshold)
            python_age = now - last_pulse
            # A pulse just after the last tick must not be called "running"
            # five seconds later. Allow scheduling jitter of three pulse periods.
            progress = "running" if python_age < 3 * pulse_seconds else "stalled"
            _write(2, (
                f"serve.loop_stall_sample threshold_seconds={threshold:g} "
                f"loop_age_seconds={age:.3f} loop_phase={phase} "
                f"python_age_seconds={python_age:.3f} python_progress={progress} "
                f"pid={parent_pid} main_wait={_main_wait(parent_pid)}\n"
            ).encode("ascii"))
            # Check parenthood again to stop signaling when the serving process
            # exits. The C handler dumps all stacks without acquiring the GIL.
            if os.getppid() != parent_pid:
                return
            try:
                os.kill(parent_pid, signal.SIGUSR2)
            except ProcessLookupError:
                return
            break  # Keep two overdue signals separate rather than coalescing.


def stall_dump_fd(stderr_fd: int) -> int:
    """A descriptor on stderr whose writes fail rather than wait when it is full.

    Reopening the descriptor through /proc gives the same pipe or file a file
    description of its own, so it can be non-blocking (and appending) without
    changing how anything else writes to stderr. Where that is not possible,
    stderr itself. A failure is counted; it never logs to blocking stderr.
    """
    was_blocking = os.get_blocking(stderr_fd)
    try:
        fd = os.open(
            f"/proc/self/fd/{stderr_fd}",
            os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError:
        diagnostic_failure(stderr_fd)
        return stderr_fd
    if os.get_blocking(stderr_fd) != was_blocking:
        # One shared description after all: put stderr back as it was.
        os.close(fd)
        os.set_blocking(stderr_fd, was_blocking)
        diagnostic_failure(stderr_fd)
        return stderr_fd
    return fd


if __name__ == "__main__":  # isolated interpreter, launched only by the class above
    _sample_parent(int(sys.argv[1]), float(sys.argv[2]),
                   tuple(float(value) for value in sys.argv[3:]))
