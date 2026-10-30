"""Nonblocking stack dumps and an opt-in, GIL-independent early stall sampler.

The router's C faulthandler timer remains the exit backstop. A separate, small
Python process requests earlier dumps using faulthandler's C SIGUSR2 handler;
unlike a Python sampling thread, this still works when a native RPC holds the
GIL. Only timestamps and fixed phase codes cross the pipe, never request data.
Importing this module has no watchdog or startup side effects.
"""
from __future__ import annotations

import logging
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

# Preserve the existing router fallback log names and behavior.
logger = logging.getLogger("trusted_router.serve")

SAMPLE_SECONDS = (5.0, 15.0)
PULSE_SECONDS = 1.0
_BEAT = struct.Struct("!cd")


def _write(fd: int, data: bytes) -> None:
    try:
        os.write(fd, data)
    except OSError:
        # Diagnostics must never wait for a full pipe/log collector or fail a
        # request when the helper has exited. Each heartbeat is < PIPE_BUF.
        pass


class LoopStallDiagnostics:
    """One helper per serving process; SIGUSR2 is reserved for its stack dumps.

    The pulse thread tests whether *any* Python thread can run. Its absence is
    evidence of process-wide Python starvation, not proof of a GIL bug (CPU
    suspension also stops it). The helper is reaped by that thread, not the
    event loop. Shutdown stops sampling without waiting on either thread/process.
    """

    def __init__(self, fd: int, stall_seconds: float) -> None:
        import faulthandler

        thresholds = [value for value in SAMPLE_SECONDS if value < stall_seconds]
        if not thresholds:
            raise ValueError("no sampling threshold before stall exit")
        if os.get_blocking(fd):
            # A signal handler writing a full blocking pipe would itself freeze
            # the router. The Linux /proc descriptor is nonblocking; fail closed
            # elsewhere rather than changing stderr's shared file description.
            raise ValueError("early samples require a nonblocking descriptor")
        read_fd, self._fd = os.pipe()
        pulse_fd: int | None = None
        registered = False
        try:
            os.set_blocking(self._fd, False)
            self._stopped = threading.Event()
            pulse_fd = os.dup(self._fd)
            faulthandler.register(signal.SIGUSR2, file=fd, all_threads=True)
            registered = True
            self._process = subprocess.Popen(  # noqa: S603 - fixed local helper, no shell
                [sys.executable, "-I", str(Path(__file__).resolve()),
                 str(os.getpid()), str(PULSE_SECONDS), *(str(value) for value in thresholds)],
                stdin=read_fd, stdout=subprocess.DEVNULL, stderr=fd,
                close_fds=True,
            )
            self._thread = threading.Thread(
                target=self._pulse, args=(pulse_fd,), name="loop-stall-pulse", daemon=True,
            )
            self._thread.start()
        except BaseException:
            if pulse_fd is not None:
                os.close(pulse_fd)
            os.close(self._fd)
            # A thread-start failure must not leave a zombie child.
            if hasattr(self, "_process"):
                self._process.kill()
                self._process.wait()
            if registered:
                faulthandler.unregister(signal.SIGUSR2)
            raise
        finally:
            os.close(read_fd)

    def beat(self, phase: bytes = b"T") -> None:
        if not self._stopped.is_set():
            _write(self._fd, _BEAT.pack(phase, time.monotonic()))

    def stop(self) -> None:
        if not self._stopped.is_set():
            self.beat(b"S")
            self._stopped.set()
            os.close(self._fd)
        # Leave the signal handler registered: a sample may already be in
        # flight. It has no Python callback and keeps the same dump descriptor.

    def _pulse(self, fd: int) -> None:
        try:
            while not self._stopped.is_set() and self._process.poll() is None:
                _write(fd, _BEAT.pack(b"P", time.monotonic()))
                self._stopped.wait(PULSE_SECONDS)
        finally:
            os.close(fd)
            # Closing both writers lets the helper see EOF, including when the
            # stop record was dropped. Bound cleanup if the helper itself hangs.
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


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
                    last_tick = timestamp
                    phase = "rearm" if kind == b"A" else "tick"
                    emitted.clear()
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
    stderr itself, logged.
    """
    was_blocking = os.get_blocking(stderr_fd)
    try:
        fd = os.open(
            f"/proc/self/fd/{stderr_fd}",
            os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError as exc:
        logger.warning("serve.stall_dump_blocking reason=%s", type(exc).__name__)
        return stderr_fd
    if os.get_blocking(stderr_fd) != was_blocking:
        # One shared description after all: put stderr back as it was.
        os.close(fd)
        os.set_blocking(stderr_fd, was_blocking)
        logger.warning("serve.stall_dump_blocking reason=shared_description")
        return stderr_fd
    return fd


if __name__ == "__main__":  # isolated interpreter, launched only by the class above
    _sample_parent(int(sys.argv[1]), float(sys.argv[2]),
                   tuple(float(value) for value in sys.argv[3:]))
