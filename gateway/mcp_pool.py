"""MCP Connection Pooling with Health Checks

Provides MCPPool class that wraps the existing mcp_tool.py connection management,
adding:
- Connection caching with last_used timestamps
- Periodic health checks (ping via list_tools)
- Auto-respawn of dead connections
- Pool statistics for monitoring

The MCPPool builds on top of the long-lived connection management in tools/mcp_tool.py,
which already handles:
- Dedicated background event loop (_mcp_loop) in a daemon thread
- Long-lived asyncio Tasks per MCP server keeping transport alive
- Automatic reconnection with exponential backoff
- Thread-safe architecture with _lock
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPPool:
    """Persistent MCP server connection pool with health monitoring.

    Wraps the existing mcp_tool.py connection management, adding:
    - Connection caching with last_used timestamps
    - Periodic health checks (ping via list_tools)
    - Auto-respawn of dead connections
    - Pool statistics for monitoring

    Thread-safe: all access to internal state is protected by _lock.
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the MCP pool.

        Args:
            config_path: Path to config.yaml. If None, uses HERMES_HOME default.
        """
        self._lock = threading.Lock()
        self._connections: Dict[str, Dict[str, Any]] = {}
        self._health_check_times: Dict[str, float] = {}
        self._config_path = config_path or self._get_default_config_path()
        self._mcp_tool = None
        self._startup_time = time.time()
        self._load_mcp_tool()

    def _get_default_config_path(self) -> str:
        """Get default config path from HERMES_HOME."""
        hermes_home = os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes"))
        return os.path.join(hermes_home, "config.yaml")

    def _load_mcp_tool(self):
        """Lazy-load the mcp_tool module to avoid circular imports."""
        try:
            from tools import mcp_tool
            self._mcp_tool = mcp_tool
            logger.debug("MCPPool: mcp_tool module loaded")
        except ImportError as e:
            logger.warning("MCPPool: Failed to load mcp_tool: %s", e)

    def _get_server_config(self) -> Dict[str, dict]:
        """Load MCP server config from config.yaml.

        Returns dict of {server_name: server_config}.
        """
        if not self._mcp_tool:
            return {}

        try:
            return self._mcp_tool._load_mcp_config()
        except Exception as e:
            logger.error("MCPPool: Failed to load MCP config: %s", e)
            return {}

    def get_connection(self, server_name: str) -> Dict[str, Any]:
        """Returns cached connection info or spawns new one.

        Updates last_used timestamp. Returns dict with keys:
        - name: server name
        - healthy: bool
        - session: MCP ClientSession or None
        - connected: bool
        - last_used: ISO timestamp
        - uptime_s: uptime in seconds

        Thread-safe.
        """
        if not self._mcp_tool:
            return {
                "name": server_name,
                "healthy": False,
                "error": "MCP tool not available",
                "connected": False,
                "last_used": datetime.now(timezone.utc).isoformat(),
                "uptime_s": time.time() - self._startup_time,
            }

        with self._lock:
            # Check if already cached
            if server_name in self._connections:
                self._connections[server_name]["last_used"] = datetime.now(timezone.utc).isoformat()
                return self._connections[server_name]

        # Not cached, fetch from mcp_tool._servers
        try:
            with self._mcp_tool._lock:
                server = self._mcp_tool._servers.get(server_name)

            if server and server.session:
                connection_info = {
                    "name": server_name,
                    "healthy": True,
                    "session": server.session,
                    "connected": True,
                    "last_used": datetime.now(timezone.utc).isoformat(),
                    "uptime_s": time.time() - self._startup_time,
                    "tool_timeout": server.tool_timeout,
                }
            else:
                connection_info = {
                    "name": server_name,
                    "healthy": False,
                    "session": None,
                    "connected": False,
                    "last_used": datetime.now(timezone.utc).isoformat(),
                    "uptime_s": time.time() - self._startup_time,
                }

            with self._lock:
                self._connections[server_name] = connection_info

            return connection_info
        except Exception as e:
            logger.error("MCPPool: Error getting connection for '%s': %s", server_name, e)
            return {
                "name": server_name,
                "healthy": False,
                "error": str(e),
                "connected": False,
                "last_used": datetime.now(timezone.utc).isoformat(),
                "uptime_s": time.time() - self._startup_time,
            }

    def health_check(self) -> Dict[str, Dict[str, Any]]:
        """Ping all cached connections via list_tools().

        Returns dict of {server_name: {healthy: bool, latency_ms: float, last_checked: str}}.
        Respawns dead connections by clearing them from cache.

        Thread-safe. Runs checks in parallel on the MCP event loop.
        """
        if not self._mcp_tool:
            return {}

        config = self._get_server_config()
        if not config:
            return {}

        results: Dict[str, Dict[str, Any]] = {}

        # Fetch all connections from mcp_tool._servers
        with self._mcp_tool._lock:
            active_servers = dict(self._mcp_tool._servers)

        for server_name in config.keys():
            server = active_servers.get(server_name)
            if not server or not server.session:
                results[server_name] = {
                    "healthy": False,
                    "latency_ms": None,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "reason": "not connected",
                }
                # Clear from cache to force respawn on next access
                with self._lock:
                    self._connections.pop(server_name, None)
                continue

            # Ping via list_tools
            start_time = time.time()

            async def _ping():
                try:
                    await asyncio.wait_for(
                        server.session.list_tools(),
                        timeout=server.tool_timeout or 30,
                    )
                    return True
                except Exception:
                    return False

            try:
                result = self._mcp_tool._run_on_mcp_loop(_ping(), timeout=35)
                latency_ms = (time.time() - start_time) * 1000
                healthy = result is True

                results[server_name] = {
                    "healthy": healthy,
                    "latency_ms": round(latency_ms, 2),
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                }

                with self._lock:
                    self._health_check_times[server_name] = time.time()

                if not healthy:
                    # Clear from cache to force respawn on next access
                    with self._lock:
                        self._connections.pop(server_name, None)
                    logger.warning(
                        "MCPPool: health check failed for '%s', will respawn",
                        server_name,
                    )

            except Exception as e:
                results[server_name] = {
                    "healthy": False,
                    "latency_ms": None,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                    "reason": str(e),
                }
                # Clear from cache to force respawn
                with self._lock:
                    self._connections.pop(server_name, None)
                logger.error(
                    "MCPPool: health check error for '%s': %s", server_name, e
                )

        return results

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return pool statistics.

        Returns dict of {server_name: {connections: int, healthy: bool, last_used: str, uptime_s: float}}.

        Thread-safe.
        """
        config = self._get_server_config()
        stats: Dict[str, Dict[str, Any]] = {}

        if not self._mcp_tool:
            return stats

        with self._mcp_tool._lock:
            active_servers = dict(self._mcp_tool._servers)

        for server_name in config.keys():
            server = active_servers.get(server_name)
            healthy = server and server.session is not None

            with self._lock:
                conn_info = self._connections.get(server_name, {})
                last_used = conn_info.get("last_used")
                last_check = self._health_check_times.get(server_name, 0)

            uptime_s = time.time() - self._startup_time

            # Count connections: 1 if healthy, 0 otherwise
            num_connections = 1 if healthy else 0

            stats[server_name] = {
                "connections": num_connections,
                "healthy": healthy,
                "last_used": last_used or "never",
                "last_health_check": (
                    datetime.fromtimestamp(last_check).isoformat()
                    if last_check
                    else "never"
                ),
                "uptime_s": round(uptime_s, 2),
            }

        return stats

    def shutdown(self):
        """Gracefully close all pooled connections.

        This delegates to mcp_tool.shutdown_mcp_servers().
        """
        if not self._mcp_tool:
            return

        try:
            self._mcp_tool.shutdown_mcp_servers()
            logger.info("MCPPool: all connections shut down")
        except Exception as e:
            logger.error("MCPPool: error during shutdown: %s", e)

        with self._lock:
            self._connections.clear()
            self._health_check_times.clear()


# Module-level singleton instance
_pool_instance: Optional[MCPPool] = None
_pool_lock = threading.Lock()


def get_mcp_pool() -> MCPPool:
    """Get or create the module-level MCPPool singleton.

    Thread-safe.
    """
    global _pool_instance
    if _pool_instance is None:
        with _pool_lock:
            if _pool_instance is None:
                _pool_instance = MCPPool()
    return _pool_instance


def health_check_handler(**context) -> str:
    """Heartbeat task handler for health checks.

    Called by the heartbeat scheduler (US-005). Returns JSON summary.

    Signature conforms to gateway heartbeat task interface.
    """
    pool = get_mcp_pool()

    try:
        results = pool.health_check()
        healthy_count = sum(1 for r in results.values() if r.get("healthy"))
        total_count = len(results)

        summary = {
            "status": "ok" if healthy_count == total_count else "degraded",
            "healthy": healthy_count,
            "total": total_count,
            "servers": results,
        }
        return json.dumps(summary)
    except Exception as e:
        logger.error("health_check_handler failed: %s", e)
        return json.dumps({
            "status": "error",
            "error": str(e),
        })
