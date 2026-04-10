"""
Gateway runtime status helpers.

Provides PID-file based detection of whether the gateway daemon is running,
used by send_message's check_fn to gate availability in the CLI.

The PID file lives at ``{HERMES_HOME}/gateway.pid``.  HERMES_HOME defaults to
``~/.hermes`` but can be overridden via the environment variable.  This means
separate HERMES_HOME directories naturally get separate PID files — a property
that will be useful when we add named profiles (multiple agents running
concurrently under distinct configurations).
"""

import os
import subprocess
from pathlib import Path
from typing import Optional


def _get_pid_path() -> Path:
    """Return the path to the gateway PID file, respecting HERMES_HOME."""
    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home / "gateway.pid"


def write_pid_file() -> None:
    """Write the current process PID to the gateway PID file."""
    pid_path = _get_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove the gateway PID file if it exists."""
    try:
        _get_pid_path().unlink(missing_ok=True)
    except Exception:
        pass


def get_running_pid() -> Optional[int]:
    """Return the PID of a running gateway instance, or ``None``.

    Checks the PID file and verifies the process is actually alive and is a gateway.
    Cleans up stale PID files automatically (handles PID reuse).
    """
    pid_path = _get_pid_path()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent

        # Verify it's actually a gateway process (not a reused PID from another process)
        # Check if the process command contains gateway indicators
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                cmd = result.stdout.strip()
                # Verify the command looks like a gateway process
                # Matches: hermes_cli.main gateway, gateway/run.py, -m gateway.run
                if "hermes_cli.main gateway" in cmd or "gateway/run.py" in cmd or "gateway.run" in cmd:
                    return pid
        except Exception:
            # If we can't verify the command, trust the PID existence check
            # This is safe because the PID file is gateway-specific
            return pid

        # Process exists but is not a gateway — clean up stale PID file
        remove_pid_file()
        return None
    except (ValueError, ProcessLookupError, PermissionError):
        # Stale PID file — process is gone
        remove_pid_file()
        return None


def is_gateway_running() -> bool:
    """Check if the gateway daemon is currently running."""
    return get_running_pid() is not None
