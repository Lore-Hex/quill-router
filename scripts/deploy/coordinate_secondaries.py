"""Secondary traffic ramps with fully supervised retention windows.

Each worker still owns its entire ramp, 15-minute drain, rollback, final
canary and billing gate. By default the three ramps start together, after the
primary's ramp and canary have already passed on the same image; each worker's
operation-bound handoff is still checked as it appears. With the repository
variable TR_DEPLOY_SECONDARIES_SERIAL=1, a handoff instead admits only the next
ramp (the #695 serial containment). Either way, failure stops admissions and
asks in-flight ramps to roll back before another shift; existing drains finish
under their watchdogs. The caller retains the cloud reservation until all
workers and final gates end.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

REGIONS = {
    "europe-west4": "NEW_EU",
    "us-east4": "NEW_US_EAST4",
    "southamerica-east1": "NEW_SOUTHAMERICA_EAST1",
}
WORKER_TIMEOUT = 35 * 60
# Long enough for a sibling mid-ramp to reach its next shift (one 1-2 minute
# watch window) and roll itself back before it is killed.
ABORT_GRACE = 180


class RolloutInterrupted(BaseException):
    """A cancellation signal (SIGINT/SIGTERM) raised from the signal handler.

    Deliberately not InterruptedError: that is an OSError, and
    ``Popen.poll()`` swallows any OSError raised inside its ``waitpid`` call.
    A signal landing there was silently dropped, so a cancelled rollout kept
    waiting out its grace and never killed the workers. Like
    KeyboardInterrupt, it also escapes ``except Exception`` handlers.
    """


def serial_from_env() -> bool:
    return os.environ.get("TR_DEPLOY_SECONDARIES_SERIAL", "").strip() == "1"


def coordinate(script: Path, directory: Path, *, poll_seconds: float = 1,
               timeout: float = WORKER_TIMEOUT, serial: bool = True,
               abort_grace: float = ABORT_GRACE) -> int:
    operation = os.environ.get("TR_DEPLOY_MUTEX_OPERATION")
    if not operation or not os.environ.get("TR_DEPLOY_MUTEX_GENERATION"):
        raise ValueError("secondary coordination requires the outer deployment fence")
    # Validate all inputs before allowing the first mutation.
    revisions = {region: os.environ[key] for region, key in REGIONS.items()}
    abort = directory / "abort"
    workers: list[tuple[str, subprocess.Popen, float]] = []
    handed_off: set[str] = set()
    failed = False
    interrupted = False

    def check_workers() -> None:
        nonlocal failed
        for region, worker, started in workers:
            result = worker.poll()
            if result not in (None, 0):
                failed = True
                abort.touch()
            if result is None and time.monotonic() - started > timeout:
                raise TimeoutError(f"{region}: worker exceeded {timeout}s; retaining reservation")

    def handed_off_now(region: str) -> bool:
        nonlocal failed
        ready = directory / f"{region}.ready"
        if region in handed_off or not ready.exists():
            return region in handed_off
        if ready.read_text().splitlines() != [region, revisions[region], operation]:
            if serial:
                raise ValueError(f"{region}: invalid retention handoff")
            # Together, siblings may be mid-ramp: fail like a worker, so each
            # rolls back at its next shift and drains finish under watch.
            print(f"::error::{region}: invalid retention handoff", flush=True)
            failed = True
            abort.touch()
            handed_off.add(region)
            return True
        handed_off.add(region)
        print(f"{region}: canary passed; retention remains supervised", flush=True)
        return True

    try:
        for region in revisions:
            check_workers()
            if failed:
                break
            ready = directory / f"{region}.ready"
            env = dict(os.environ, TR_SECONDARY_READY_FILE=str(ready),
                       TR_SECONDARY_ABORT_FILE=str(abort))
            worker = subprocess.Popen(  # noqa: S603 - fixed local deployment script
                ["bash", str(script), "--region", region],  # noqa: S607
                env=env, start_new_session=True,
            )
            workers.append((region, worker, time.monotonic()))
            while serial:
                check_workers()
                if failed or worker.poll() == 0:
                    # Held/already-current workers can finish without handoff.
                    break
                if handed_off_now(region):
                    break
                time.sleep(poll_seconds)
        while any(worker.poll() is None for _, worker, _ in workers):
            check_workers()
            for region, _, _ in workers:
                handed_off_now(region)
            time.sleep(poll_seconds)
        check_workers()
        for region, _, _ in workers:
            handed_off_now(region)
        return 1 if failed else 0
    except RolloutInterrupted:
        interrupted = True
        raise
    finally:
        # On cancellation/timeout retain drain tags and the cloud reservation.
        # Kill the process groups, including their watchdog and CLI children,
        # rather than orphaning a traffic-changing child after this job exits.
        try:
            abort.touch()
            active = [worker for _, worker, _ in workers if worker.poll() is None]
            if not serial and not interrupted:
                # A timeout is no cancellation: siblings ramping together may be
                # part way, and a killed ramp is left split. Let each refuse its
                # next shift and roll back first (staged_traffic.sh shift_traffic).
                deadline = time.monotonic() + abort_grace
                while active and time.monotonic() < deadline:
                    time.sleep(poll_seconds)
                    active = [worker for worker in active if worker.poll() is None]
        except RolloutInterrupted:
            # A cancellation anywhere in this cleanup ends the grace early but
            # never skips the kill.
            stop([worker for _, worker, _ in workers if worker.poll() is None])
            raise
        stop(active)


def stop(active: list[subprocess.Popen]) -> None:
    for worker in active:
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for worker in active:
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()


def main() -> int:
    def interrupted(signum: int, _frame: object) -> None:
        raise RolloutInterrupted(f"secondary rollout interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    serial = serial_from_env()
    print(f"secondary ramps: {'serial' if serial else 'together'} "
          f"(TR_DEPLOY_SECONDARIES_SERIAL={os.environ.get('TR_DEPLOY_SECONDARIES_SERIAL', '')!r})", flush=True)
    with tempfile.TemporaryDirectory(prefix="tr-secondary-", dir=os.environ.get("RUNNER_TEMP")) as tmp:
        return coordinate(Path(__file__).with_name("ramp_secondaries.sh"), Path(tmp),
                          serial=serial)


if __name__ == "__main__":
    raise SystemExit(main())
