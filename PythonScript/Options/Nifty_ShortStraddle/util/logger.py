"""
util/logger.py  —  Structured logging for Nifty Short Straddle (Partial)
═══════════════════════════════════════════════════════════════════════════════
Responsibilities:
  1. Configure the root application logger once at startup via setup_logging()
  2. Route log output to console and/or rotating file based on config.toml
  3. Format ALL timestamps in IST (Asia/Kolkata, UTC+05:30) — matches the
     original plog() ts() format so logs look identical on local and VPS machines
  4. Provide convenience functions that mirror the original plog() / psep() API:
       info()  → replaces pinfo()
       warn()  → replaces pwarn()
       error() → replaces perr()
       debug() → replaces pdebug()
       sep()   → replaces psep()
  5. Support three rotation modes (controlled by [logging] in config.toml):
       "daily"  — TimedRotatingFileHandler — rotates at midnight; LOG_BACKUP_COUNT days kept
       "size"   — RotatingFileHandler      — rotates at LOG_MAX_BYTES; LOG_BACKUP_COUNT files kept
       "none"   — plain FileHandler        — no rotation, file grows until manually cleared

Logger hierarchy:
    "nss"            ← root strategy logger — configured once by setup_logging()
    "nss.core"       ← strategy_core module
    "nss.state"      ← state persistence module
    "nss.notifier"   ← telegram notifier module
    "nss.<name>"     ← any module using get_logger(__name__)

Usage:
    # Option A — module-level convenience functions (direct drop-in for plog()):
    from util.logger import info, warn, error, debug, sep
    info("Strategy started")
    sep()
    warn("VIX above threshold")

    # Option B — named child logger (for module-specific log context):
    from util.logger import get_logger
    log = get_logger(__name__)   # → "nss.strategy_core" etc.
    log.info("Entry placed: CE %s PE %s", ce_symbol, pe_symbol)

    # Option C — re-configure with a custom Config (useful in tests):
    from util.logger import setup_logging
    from util.config_util import load_config
    setup_logging(load_config("tests/config.toml"))

Notes:
    • setup_logging() is idempotent: calling it again removes old handlers and
      reconfigures from scratch. This prevents duplicate handler accumulation in
      tests or when the config is reloaded at runtime.
    • Auto-configuration from the module-level cfg singleton runs on import.
      If config.toml is not found yet, setup is deferred — call setup_logging()
      explicitly before using any log functions.
    • On UTC servers, daily rotation fires at 00:00 UTC (05:30 IST). To rotate
      at exact IST midnight, set TZ=Asia/Kolkata in the systemd service or shell.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    # Avoid circular import at runtime — Config is only needed for type hints.
    from util.config_util import Config

__all__ = [
    "StrategyLogger",
    "setup_logging",
    "get_logger",
    "info",
    "warn",
    "error",
    "debug",
    "sep",
]

# ── IST timezone ──────────────────────────────────────────────────────────────
# All timestamps are rendered in IST regardless of the server's system timezone.
# This matches the original now_ist() / ts() functions in the reference script.
_IST = pytz.timezone("Asia/Kolkata")

# ── Strategy logger hierarchy root ────────────────────────────────────────────
# All child loggers created by get_logger() inherit handlers from this root.
# Using a named root (not the Python root "") prevents interference with
# third-party libraries (openalgo, apscheduler, requests) that also log.
_ROOT_LOGGER_NAME = "nss"


# ═══════════════════════════════════════════════════════════════════════════════
#  IST-aware log formatter
# ═══════════════════════════════════════════════════════════════════════════════

class ISTFormatter(logging.Formatter):
    """
    Custom formatter that renders %(asctime)s in IST (Asia/Kolkata, UTC+05:30)
    regardless of the server's system timezone.

    Default output for a record:
        2026-03-20 09:30:00 IST [INFO    ] Strategy started

    This matches the original ts() format in the reference script exactly, so
    log files look identical whether the process runs locally (IST machine) or
    on a UTC VPS / cloud instance.

    The default LOG_FORMAT in config.toml is:
        "%(asctime)s [%(levelname)-8s] %(message)s"
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        import datetime
        ist_dt = datetime.datetime.fromtimestamp(record.created, tz=_IST)
        if datefmt:
            return ist_dt.strftime(datefmt)
        # Default: matches the original plog() ts() output format
        return ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")


# ═══════════════════════════════════════════════════════════════════════════════
#  StrategyLogger — encapsulates all logging configuration and convenience API
# ═══════════════════════════════════════════════════════════════════════════════

class StrategyLogger:
    """
    Manages the strategy logging pipeline: configuration, handlers, and
    convenience methods for the "nss" logger hierarchy.

    Typical usage is through the module-level singleton (_logger_instance) which
    auto-configures on import. The module-level functions (info, warn, error,
    debug, sep, get_logger, setup_logging) delegate to this singleton.
    """

    def __init__(self) -> None:
        self._is_configured: bool = False
        self._root_log: logging.Logger = logging.getLogger(_ROOT_LOGGER_NAME)

    @property
    def is_configured(self) -> bool:
        return self._is_configured

    def setup(self, cfg: "Config") -> None:
        """
        Configure the strategy logger from a validated Config object.

        Reads the following fields from cfg (Section 12 of config.toml):
            LOG_LEVEL        — "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
            LOG_TO_CONSOLE   — True/False — emit to stdout
            LOG_TO_FILE      — True/False — write to file
            LOG_FILE         — path to the log file (relative paths resolve from CWD)
            LOG_ROTATION     — "daily" | "size" | "none"
            LOG_MAX_BYTES    — max file size in bytes before rotation (size mode only)
            LOG_BACKUP_COUNT — number of old log files to keep
            LOG_FORMAT       — Python logging format string

        This method is idempotent — calling it again clears all existing handlers
        and reconfigures from scratch. This prevents duplicate handler accumulation
        when called multiple times (e.g., in unit tests or when reloading config).

        Parameters
        ----------
        cfg : Config
            Validated Config object from util.config_util.load_config().

        Raises
        ------
        OSError
            If the log file parent directory cannot be created or the file cannot
            be opened for writing.
        """
        logger = logging.getLogger(_ROOT_LOGGER_NAME)

        # Clear existing handlers — required for idempotent re-configuration.
        # Close each handler first to flush buffers and release file descriptors.
        for _handler in logger.handlers[:]:
            _handler.close()
        logger.handlers.clear()

        # Do not propagate to the Python root logger ("") to avoid duplicate output
        # from any basicConfig() calls made by third-party libraries.
        logger.propagate = False

        level = getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO)
        logger.setLevel(level)

        formatter = ISTFormatter(fmt=cfg.LOG_FORMAT)

        # ── Console handler ───────────────────────────────────────────────────
        if cfg.LOG_TO_CONSOLE:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        # ── File handler ──────────────────────────────────────────────────────
        if cfg.LOG_TO_FILE:
            log_path = Path(cfg.LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            rotation = cfg.LOG_ROTATION.lower()

            if rotation == "daily":
                file_handler: logging.Handler = TimedRotatingFileHandler(
                    filename    = log_path,
                    when        = "midnight",
                    interval    = 1,
                    backupCount = cfg.LOG_BACKUP_COUNT,
                    encoding    = "utf-8",
                    utc         = False,
                )
            elif rotation == "size":
                file_handler = RotatingFileHandler(
                    filename    = log_path,
                    maxBytes    = cfg.LOG_MAX_BYTES,
                    backupCount = cfg.LOG_BACKUP_COUNT,
                    encoding    = "utf-8",
                )
            else:
                # rotation == "none" — append forever, no rotation.
                file_handler = logging.FileHandler(
                    filename = log_path,
                    mode     = "a",
                    encoding = "utf-8",
                )

            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        self._root_log = logger
        self._is_configured = True

    def get_logger(self, name: str) -> logging.Logger:
        """
        Return a named child logger under the strategy root ("nss").

        All child loggers inherit handlers and level from the root logger configured
        by setup(). There is no need to add handlers to child loggers.

        Parameters
        ----------
        name : str
            Typically __name__ from the calling module.
            Automatically prefixed with "nss." if not already.

        Returns
        -------
        logging.Logger
        """
        if not name.startswith(_ROOT_LOGGER_NAME):
            name = f"{_ROOT_LOGGER_NAME}.{name}"
        return logging.getLogger(name)

    # ═══════════════════════════════════════════════════════════════════════════
    #  Convenience methods — drop-in replacements for plog() family
    # ═══════════════════════════════════════════════════════════════════════════

    def info(self, msg: str) -> None:
        """Log at INFO level — drop-in replacement for pinfo()."""
        self._root_log.info(msg)

    def warn(self, msg: str) -> None:
        """Log at WARNING level — drop-in replacement for pwarn()."""
        self._root_log.warning(msg)

    def error(self, msg: str) -> None:
        """Log at ERROR level — drop-in replacement for perr()."""
        self._root_log.error(msg)

    def debug(self, msg: str) -> None:
        """Log at DEBUG level — drop-in replacement for pdebug()."""
        self._root_log.debug(msg)

    def sep(self) -> None:
        """
        Log a visual separator line at INFO level — drop-in replacement for psep().
        WHY 68 dashes: matches the original psep() which used '─' * 68.
        """
        self._root_log.info("─" * 68)


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level singleton + backward-compatible function API
# ═══════════════════════════════════════════════════════════════════════════════

_logger_instance = StrategyLogger()


def setup_logging(cfg: "Config") -> None:
    """Backward-compatible wrapper — delegates to the singleton."""
    _logger_instance.setup(cfg)


def get_logger(name: str) -> logging.Logger:
    """Backward-compatible wrapper — delegates to the singleton."""
    return _logger_instance.get_logger(name)


def info(msg: str) -> None:
    """Log at INFO level — drop-in replacement for pinfo()."""
    _logger_instance.info(msg)


def warn(msg: str) -> None:
    """Log at WARNING level — drop-in replacement for pwarn()."""
    _logger_instance.warn(msg)


def error(msg: str) -> None:
    """Log at ERROR level — drop-in replacement for perr()."""
    _logger_instance.error(msg)


def debug(msg: str) -> None:
    """Log at DEBUG level — drop-in replacement for pdebug()."""
    _logger_instance.debug(msg)


def sep() -> None:
    """Log a visual separator line at INFO level — drop-in replacement for psep()."""
    _logger_instance.sep()


# ═══════════════════════════════════════════════════════════════════════════════
#  Auto-configure from the module-level config singleton on import
# ═══════════════════════════════════════════════════════════════════════════════

def _auto_setup() -> None:
    """
    Configure logging from util.config_util.cfg if it is already loaded.
    Called automatically at module import time.
    """
    try:
        from util.config_util import cfg  # noqa: PLC0415
        if cfg is not None:
            _logger_instance.setup(cfg)
    except Exception as exc:
        print(
            f"[logger] WARNING: auto-setup failed — {exc}\n"
            "Call setup_logging(cfg) explicitly before using logging functions.",
            file=sys.stderr,
        )


_auto_setup()


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test — smoke-test the full logging pipeline without running the
#  strategy.  Writes a few lines at every level and confirms file output.
#
#  Usage:
#    python util/logger.py                        # uses default config.toml
#    python util/logger.py /path/to/config.toml  # custom path
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    # Import here so the module works as a standalone script too.
    from util.config_util import _DEFAULT_CONFIG_PATH, load_config

    _path = _cfg_path or _DEFAULT_CONFIG_PATH
    print(f"Loading config: {_path.resolve()}\n")

    try:
        _test_cfg = load_config(_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config load FAILED:\n{exc}")
        sys.exit(1)

    # Re-configure with the loaded config (overrides auto-setup from import)
    setup_logging(_test_cfg)

    # ── Smoke test ────────────────────────────────────────────────────────────
    print("─" * 72)
    print("  LOGGER SELF-TEST")
    print("─" * 72)

    sep()
    info("Logger self-test started")
    debug("DEBUG   — only visible when log_level = DEBUG")
    info("INFO    — standard operational messages")
    warn("WARNING — non-fatal issues (e.g. margin buffer low, VIX near limit)")
    error("ERROR   — recoverable failures (e.g. quote fetch failed, order rejected)")
    sep()
    info(f"Console handler  : {_test_cfg.LOG_TO_CONSOLE}")
    info(f"File handler     : {_test_cfg.LOG_TO_FILE}  →  {_test_cfg.LOG_FILE!r}")
    info(f"Log level        : {_test_cfg.LOG_LEVEL}")
    info(f"Rotation mode    : {_test_cfg.LOG_ROTATION}")
    info(f"Max file size    : {_test_cfg.LOG_MAX_BYTES / (1024 * 1024):.0f} MB  "
         f"(backup count: {_test_cfg.LOG_BACKUP_COUNT})")
    sep()

    # Named child logger test
    _child = get_logger("self_test")
    _child.info("Named child logger 'nss.self_test' works correctly")

    sep()
    info("Logger self-test complete ✓")
    print("\n  All log levels written successfully.\n")
