"""
CryptoHybrid AI -- watchdog_btc.py
Monitors main_cryptohybrid.py and automatically restarts it on crash or freeze.
Designed to run unattended 24/7. The bat file starts this; this starts the trader.

Failure conditions detected:
  1. Process has exited (any exit code)
  2. No output for 10 minutes (frozen / deadlocked)
  3. Near-zero CPU for 10 minutes while output is also absent (hung)

Restart limits: max 5 restarts per 1-hour rolling window, then stops and alerts.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

BASE_DIR        = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")                                   # own .env (primary)
load_dotenv(BASE_DIR.parent / "TideTraderAI" / ".env")             # sibling template .env fallback (no override)

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

MAIN_SCRIPT     = BASE_DIR / "main_cryptohybrid.py"
LOG_FILE        = BASE_DIR / "logs" / "watchdog.log"
SHUTDOWN_FLAG   = BASE_DIR / "logs" / "shutdown.flag"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

FREEZE_TIMEOUT  = 600   # seconds without output = frozen (10 min)
CPU_HANG_SECS   = 600   # seconds at near-zero CPU = hung (10 min)
OUTPUT_GUARD    = 300   # CPU hang only fires if output also missing for 5+ min
CHECK_INTERVAL  = 30    # health-check loop interval (seconds)
HEARTBEAT_EVERY = 1800  # log heartbeat every 30 minutes
RESTART_DELAY   = 30    # seconds to wait before restarting (Kraken API cooldown)
MAX_RESTARTS    = 5     # max restarts allowed in one RESTART_WINDOW
RESTART_WINDOW  = 3600  # rolling window for restart counter (1 hour in seconds)

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

# ALBION STANDING RULE: all log timestamps are UTC (never BST/local). See main_cryptohybrid.py.
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("CryptoHybrid.Watchdog")

# ──────────────────────────────────────────────────────────────────────────────
# Pushover (self-contained -- watchdog must never import notifier_btc)
# ──────────────────────────────────────────────────────────────────────────────

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_PO_USER      = os.getenv("PUSHOVER_USER_KEY",  "")
_PO_TOKEN     = os.getenv("PUSHOVER_API_TOKEN", "")


def _push(title: str, message: str, priority: int = 0) -> None:
    """Fire-and-forget Pushover notification. Never raises."""
    if not _PO_USER or not _PO_TOKEN:
        return
    try:
        requests.post(
            _PUSHOVER_URL,
            data={
                "token":    _PO_TOKEN,
                "user":     _PO_USER,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=5,
        )
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Output reader thread
# ──────────────────────────────────────────────────────────────────────────────

class _OutputReader(threading.Thread):
    """
    Daemon thread that drains the subprocess stdout+stderr pipe.
    Updates last_output_at on every line received for freeze detection.
    Echoes trader output to the watchdog console (not to watchdog.log).
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__(daemon=True, name="OutputReader")
        self._proc          = proc
        self.last_output_at = time.monotonic()  # start of life = "just had output"

    def run(self) -> None:
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                self.last_output_at = time.monotonic()
                try:
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text:
                        print(f"  {text}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Watchdog
# ──────────────────────────────────────────────────────────────────────────────

class Watchdog:

    def __init__(self) -> None:
        self._proc:           Optional[subprocess.Popen] = None
        self._reader:         Optional[_OutputReader]    = None
        self._restart_times:  list                       = []   # monotonic timestamps
        self._proc_start:     float                      = 0.0
        self._heartbeat_at:   float                      = 0.0
        self._cpu_zero_since: Optional[float]            = None
        self._stopped:        bool                       = False

    # ── Process lifecycle ─────────────────────────────────────────────────────

    def _launch(self) -> None:
        """Start main_cryptohybrid.py as a monitored subprocess."""
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        kwargs: dict = {}
        if sys.platform == "win32":
            # Separate process group: watchdog Ctrl+C won't automatically
            # kill the trader; we send CTRL_C_EVENT explicitly on shutdown.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        self._proc = subprocess.Popen(
            [sys.executable, "-u", str(MAIN_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into the readable pipe
            cwd=str(BASE_DIR),
            env=env,
            bufsize=0,
            **kwargs,
        )
        self._reader         = _OutputReader(self._proc)
        self._reader.start()
        self._proc_start     = time.monotonic()
        self._heartbeat_at   = time.monotonic()
        self._cpu_zero_since = None
        log.info("CryptoHybrid AI started (PID %d)", self._proc.pid)

    def _terminate(self, grace_secs: int = 15) -> None:
        """
        Gracefully stop the subprocess: send SIGINT (Ctrl+C), wait, then force-kill.
        On Windows uses CTRL_C_EVENT so the trader's KeyboardInterrupt handler fires.
        """
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return

        pid = self._proc.pid
        try:
            log.info("Sending shutdown signal to CryptoHybrid AI (PID %d)...", pid)
            if sys.platform == "win32":
                os.kill(pid, signal.CTRL_C_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)

            try:
                self._proc.wait(timeout=grace_secs)
                log.info("CryptoHybrid AI stopped cleanly (PID %d)", pid)
            except subprocess.TimeoutExpired:
                log.warning("Grace period expired -- force-killing PID %d", pid)
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                log.info("CryptoHybrid AI force-killed (PID %d)", pid)

        except Exception as exc:
            log.warning("Error during shutdown signal (PID %d): %s -- forcing kill", pid, exc)
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            self._proc = None

    # ── Health checks ─────────────────────────────────────────────────────────

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _check_frozen(self) -> Optional[str]:
        """Return failure reason if no output for FREEZE_TIMEOUT seconds."""
        if self._reader is None:
            return None
        silent_secs = time.monotonic() - self._reader.last_output_at
        if silent_secs >= FREEZE_TIMEOUT:
            return f"No output for {int(silent_secs // 60)} minutes (process frozen)"
        return None

    def _check_cpu_hung(self) -> Optional[str]:
        """
        Return failure reason if near-zero CPU for CPU_HANG_SECS.
        Requires output to also be missing for OUTPUT_GUARD seconds first,
        preventing false positives during normal candle-wait sleeps
        (CryptoHybrid sleeps up to 5 minutes between ticks but always logs).
        """
        if not self._alive() or self._reader is None:
            return None
        silent_secs = time.monotonic() - self._reader.last_output_at
        if silent_secs < OUTPUT_GUARD:
            self._cpu_zero_since = None
            return None
        try:
            import psutil
            cpu = psutil.Process(self._proc.pid).cpu_percent(interval=1)
            if cpu < 0.5:
                if self._cpu_zero_since is None:
                    self._cpu_zero_since = time.monotonic()
                elif time.monotonic() - self._cpu_zero_since >= CPU_HANG_SECS:
                    zero_mins = int((time.monotonic() - self._cpu_zero_since) // 60)
                    return f"0% CPU for {zero_mins} minutes (process hung)"
            else:
                self._cpu_zero_since = None
        except Exception:
            pass
        return None

    # ── Restart management ────────────────────────────────────────────────────

    def _prune_restarts(self) -> None:
        cutoff = time.monotonic() - RESTART_WINDOW
        self._restart_times = [t for t in self._restart_times if t > cutoff]

    def _at_limit(self) -> bool:
        self._prune_restarts()
        return len(self._restart_times) >= MAX_RESTARTS

    def _shutdown_requested(self) -> bool:
        """
        Detect the dashboard's shutdown.flag. This is a full-stack stop signal:
        the main engine exits on it and we (the watchdog) must NOT relaunch it.
        """
        return SHUTDOWN_FLAG.exists()

    def _do_restart(self, reason: str) -> bool:
        """
        Stop the trader, wait RESTART_DELAY, relaunch.
        Returns True if the new process is confirmed alive.
        """
        self._prune_restarts()
        attempt = len(self._restart_times) + 1

        log.warning("-" * 60)
        log.warning("RESTART ATTEMPT %d/%d -- %s", attempt, MAX_RESTARTS, reason)
        log.warning("-" * 60)

        _push(
            f"CryptoHybrid AI - Restarting (attempt {attempt}/{MAX_RESTARTS})",
            f"Crash/freeze detected: {reason}\n\nRestarting in {RESTART_DELAY}s.",
            priority=1,
        )

        self._terminate()

        log.info("Waiting %d seconds before restart (API cooldown)...", RESTART_DELAY)
        time.sleep(RESTART_DELAY)

        try:
            self._launch()
        except Exception as exc:
            log.error("Failed to launch CryptoHybrid AI: %s", exc)
            return False

        if self._alive():
            self._restart_times.append(time.monotonic())
            log.info("CryptoHybrid AI is running again (PID %d)", self._proc.pid)
            _push(
                "CryptoHybrid AI - Restarted Successfully",
                f"System restarted after: {reason}. Running normally.",
            )
            return True

        log.error("Restart failed -- process not alive after launch")
        return False

    def _handle_limit_exceeded(self) -> None:
        """Log and alert when max restarts exceeded. Stops the trader."""
        self._prune_restarts()
        msg = (
            f"CryptoHybrid AI crashed {MAX_RESTARTS} times within 1 hour. "
            f"Watchdog has stopped attempting restarts. "
            f"Manual intervention required."
        )
        log.error("=" * 60)
        log.error("MAX RESTARTS EXCEEDED -- WATCHDOG STOPPING")
        log.error(msg)
        log.error("Review logs/cryptohybrid.log for crash details.")
        log.error("=" * 60)
        _push(
            "CryptoHybrid AI - URGENT: Manual Intervention Required",
            (
                f"CryptoHybrid AI crashed {MAX_RESTARTS} times in 1 hour. "
                f"System stopped. Please check logs and restart manually."
            ),
            priority=1,
        )
        self._terminate()

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def _maybe_heartbeat(self) -> None:
        if time.monotonic() - self._heartbeat_at < HEARTBEAT_EVERY:
            return
        uptime_h = (time.monotonic() - self._proc_start) / 3600
        log.info(
            "Watchdog heartbeat -- CryptoHybrid AI running normally for %.1f hours",
            uptime_h,
        )
        self._heartbeat_at = time.monotonic()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("=" * 60)
        log.info("  CryptoHybrid AI Watchdog -- Starting")
        log.info("  Monitoring:      %s", MAIN_SCRIPT.name)
        log.info("  Freeze timeout:  %d min (no output)", FREEZE_TIMEOUT // 60)
        log.info("  CPU hang check:  %d min at 0%% (after %d min silence)",
                 CPU_HANG_SECS // 60, OUTPUT_GUARD // 60)
        log.info("  Max restarts:    %d per hour", MAX_RESTARTS)
        log.info("  Restart delay:   %d seconds", RESTART_DELAY)
        log.info("  Heartbeat every: %d min", HEARTBEAT_EVERY // 60)
        log.info("  Watchdog log:    %s", LOG_FILE)
        log.info("=" * 60)

        try:
            self._launch()
        except Exception as exc:
            log.error("FATAL: Could not start CryptoHybrid AI: %s", exc)
            return

        while not self._stopped:
            try:
                time.sleep(CHECK_INTERVAL)

                if self._stopped:
                    break

                # ── Full-stack shutdown requested (dashboard button) ───────
                # Stop the engine and the watchdog itself -- do NOT relaunch.
                if self._shutdown_requested():
                    log.info("shutdown.flag detected -- clean full shutdown, not restarting")
                    self.shutdown()
                    SHUTDOWN_FLAG.unlink(missing_ok=True)
                    return

                # ── Check 1: process has exited ────────────────────────────
                if not self._alive():
                    # A clean, intentional exit under shutdown.flag is NOT a crash.
                    if self._shutdown_requested():
                        log.info("Engine exited under shutdown.flag -- clean shutdown, not restarting")
                        self.shutdown()
                        SHUTDOWN_FLAG.unlink(missing_ok=True)
                        return
                    exit_code = self._proc.returncode if self._proc else "unknown"
                    reason    = f"Process exited (exit code {exit_code})"
                    log.warning(reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(reason)
                    continue

                # ── Check 2: frozen (no output for 10 minutes) ────────────
                freeze_reason = self._check_frozen()
                if freeze_reason:
                    log.warning(freeze_reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(freeze_reason)
                    continue

                # ── Check 3: hung (0% CPU after 5+ min silence) ───────────
                cpu_reason = self._check_cpu_hung()
                if cpu_reason:
                    log.warning(cpu_reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(cpu_reason)
                    continue

                # All healthy
                self._maybe_heartbeat()

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error("Watchdog loop error (non-fatal, continuing): %s", exc)

    def shutdown(self) -> None:
        """Clean shutdown on Ctrl+C. Stops the trader gracefully first."""
        log.info("")
        log.info("=" * 60)
        log.info("  CryptoHybrid AI Watchdog -- Shutdown requested")
        log.info("=" * 60)
        self._stopped = True
        self._terminate(grace_secs=20)
        _push(
            "CryptoHybrid AI - Shutdown",
            "CryptoHybrid AI shutdown requested -- system stopped cleanly.",
        )
        log.info("Watchdog stopped cleanly.")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    watchdog = Watchdog()
    try:
        watchdog.run()
    except KeyboardInterrupt:
        watchdog.shutdown()
    sys.exit(0)
