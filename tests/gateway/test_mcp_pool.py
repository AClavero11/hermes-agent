"""Tests for MCPPool connection pooling and health checks."""

import json
import threading
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from gateway.mcp_pool import MCPPool, get_mcp_pool, health_check_handler


class MockMCPServerTask:
    """Mock MCPServerTask for testing."""

    def __init__(self, name: str, session=None, tool_timeout=60):
        self.name = name
        self.session = session
        self.tool_timeout = tool_timeout
        self._tools = []


class TestMCPPoolInit:
    """Test MCPPool initialization."""

    def test_init_creates_instance(self):
        """MCPPool should initialize successfully."""
        pool = MCPPool()
        assert pool is not None
        assert pool._connections == {}
        assert pool._health_check_times == {}

    def test_init_with_custom_config_path(self):
        """MCPPool should accept custom config path."""
        pool = MCPPool(config_path="/tmp/custom-config.yaml")
        assert pool._config_path == "/tmp/custom-config.yaml"

    def test_default_config_path_from_env(self, monkeypatch):
        """MCPPool should use HERMES_HOME from environment."""
        monkeypatch.setenv("HERMES_HOME", "/tmp/test-hermes")
        pool = MCPPool()
        assert pool._config_path == "/tmp/test-hermes/config.yaml"


class TestGetConnection:
    """Test get_connection method."""

    def test_get_connection_no_mcp_tool(self):
        """Should gracefully handle missing mcp_tool."""
        pool = MCPPool()
        pool._mcp_tool = None

        result = pool.get_connection("alexandria")
        assert result["name"] == "alexandria"
        assert result["healthy"] is False
        assert result["connected"] is False
        assert "error" in result

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_connection_not_in_cache(self, mock_load):
        """Should fetch from mcp_tool._servers if not cached."""
        pool = MCPPool()

        # Mock mcp_tool with a connected server
        mock_mcp_tool = MagicMock()
        mock_session = MagicMock()
        mock_server = MockMCPServerTask(
            "alexandria", session=mock_session, tool_timeout=60
        )
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool

        result = pool.get_connection("alexandria")

        assert result["name"] == "alexandria"
        assert result["healthy"] is True
        assert result["connected"] is True
        assert result["session"] is mock_session
        assert "last_used" in result
        assert "uptime_s" in result

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_connection_caches_result(self, mock_load):
        """Should cache connection info on subsequent calls."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_session = MagicMock()
        mock_server = MockMCPServerTask("v11", session=mock_session)
        mock_mcp_tool._servers = {"v11": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool

        # First call
        result1 = pool.get_connection("v11")
        assert result1["name"] == "v11"

        # Mock server connection drops
        mock_mcp_tool._servers = {"v11": MockMCPServerTask("v11", session=None)}

        # Second call should return cached version
        result2 = pool.get_connection("v11")
        assert result2["connected"] is True  # Still cached
        assert result2["last_used"] == result1["last_used"]

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_connection_disconnected_server(self, mock_load):
        """Should return disconnected status if server.session is None."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=None)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool

        result = pool.get_connection("alexandria")
        assert result["healthy"] is False
        assert result["connected"] is False


class TestHealthCheck:
    """Test health_check method."""

    def test_health_check_no_mcp_tool(self):
        """Should return empty dict if mcp_tool unavailable."""
        pool = MCPPool()
        pool._mcp_tool = None

        results = pool.health_check()
        assert results == {}

    def test_health_check_no_config(self):
        """Should return empty dict if no servers configured."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_mcp_tool._load_mcp_config = MagicMock(return_value={})
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(return_value={})

        results = pool.health_check()
        assert results == {}

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_health_check_disconnected_server(self, mock_load):
        """Should report unhealthy if server.session is None."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=None)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        results = pool.health_check()

        assert "alexandria" in results
        assert results["alexandria"]["healthy"] is False
        assert "last_checked" in results["alexandria"]

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_health_check_clears_dead_connections(self, mock_load):
        """Should clear dead connections from cache."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=None)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        # Pre-populate cache
        pool._connections["alexandria"] = {
            "name": "alexandria",
            "healthy": True,
            "connected": True,
        }

        # Run health check
        pool.health_check()

        # Cache should be cleared
        assert "alexandria" not in pool._connections

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_health_check_successful_ping(self, mock_load):
        """Should report healthy if list_tools succeeds."""
        pool = MCPPool()

        # Mock successful list_tools
        mock_session = AsyncMock()
        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=mock_session)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()

        # Mock _run_on_mcp_loop to return True (successful ping)
        mock_mcp_tool._run_on_mcp_loop = MagicMock(return_value=True)

        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        results = pool.health_check()

        assert "alexandria" in results
        assert results["alexandria"]["healthy"] is True
        assert "latency_ms" in results["alexandria"]
        assert results["alexandria"]["latency_ms"] is not None

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_health_check_failed_ping(self, mock_load):
        """Should report unhealthy if list_tools fails."""
        pool = MCPPool()

        mock_session = AsyncMock()
        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=mock_session)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()

        # Mock _run_on_mcp_loop to return False (failed ping)
        mock_mcp_tool._run_on_mcp_loop = MagicMock(return_value=False)

        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        results = pool.health_check()

        assert "alexandria" in results
        assert results["alexandria"]["healthy"] is False

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_health_check_exception_handling(self, mock_load):
        """Should handle exceptions during health check."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=MagicMock())
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()

        # Mock _run_on_mcp_loop to raise exception
        mock_mcp_tool._run_on_mcp_loop = MagicMock(
            side_effect=RuntimeError("Connection lost")
        )

        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        results = pool.health_check()

        assert "alexandria" in results
        assert results["alexandria"]["healthy"] is False
        assert "Connection lost" in results["alexandria"]["reason"]


class TestGetStats:
    """Test get_stats method."""

    def test_get_stats_empty_config(self):
        """Should return empty dict if no servers configured."""
        pool = MCPPool()
        pool._get_server_config = MagicMock(return_value={})

        stats = pool.get_stats()
        assert stats == {}

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_stats_connected_server(self, mock_load):
        """Should report stats for connected server."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_session = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=mock_session)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        # Pre-populate connection cache
        pool._connections["alexandria"] = {
            "last_used": "2026-03-07T10:00:00",
        }

        stats = pool.get_stats()

        assert "alexandria" in stats
        assert stats["alexandria"]["connections"] == 1
        assert stats["alexandria"]["healthy"] is True
        assert stats["alexandria"]["last_used"] == "2026-03-07T10:00:00"
        assert "uptime_s" in stats["alexandria"]

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_stats_disconnected_server(self, mock_load):
        """Should report stats for disconnected server."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=None)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        stats = pool.get_stats()

        assert "alexandria" in stats
        assert stats["alexandria"]["connections"] == 0
        assert stats["alexandria"]["healthy"] is False

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_get_stats_multiple_servers(self, mock_load):
        """Should report stats for multiple servers."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_session_1 = MagicMock()
        mock_session_2 = MagicMock()
        mock_mcp_tool._servers = {
            "alexandria": MockMCPServerTask("alexandria", session=mock_session_1),
            "v11": MockMCPServerTask("v11", session=mock_session_2),
        }
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}, "v11": {}}
        )

        stats = pool.get_stats()

        assert len(stats) == 2
        assert "alexandria" in stats
        assert "v11" in stats
        assert stats["alexandria"]["healthy"] is True
        assert stats["v11"]["healthy"] is True


class TestShutdown:
    """Test shutdown method."""

    def test_shutdown_no_mcp_tool(self):
        """Should gracefully handle missing mcp_tool."""
        pool = MCPPool()
        pool._mcp_tool = None

        # Should not raise
        pool.shutdown()

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_shutdown_calls_mcp_tool(self, mock_load):
        """Should call mcp_tool.shutdown_mcp_servers."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.shutdown_mcp_servers = MagicMock()
        pool._mcp_tool = mock_mcp_tool

        pool.shutdown()

        mock_mcp_tool.shutdown_mcp_servers.assert_called_once()

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_shutdown_clears_cache(self, mock_load):
        """Should clear internal caches on shutdown."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        pool._mcp_tool = mock_mcp_tool

        # Pre-populate caches
        pool._connections["test"] = {"name": "test"}
        pool._health_check_times["test"] = time.time()

        pool.shutdown()

        assert pool._connections == {}
        assert pool._health_check_times == {}

    @patch("gateway.mcp_pool.MCPPool._load_mcp_tool")
    def test_shutdown_handles_exceptions(self, mock_load):
        """Should handle exceptions during shutdown."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.shutdown_mcp_servers = MagicMock(
            side_effect=RuntimeError("Shutdown failed")
        )
        pool._mcp_tool = mock_mcp_tool

        # Should not raise
        pool.shutdown()


class TestGetMCPPoolSingleton:
    """Test module-level singleton."""

    def test_get_mcp_pool_returns_singleton(self):
        """get_mcp_pool should return the same instance."""
        pool1 = get_mcp_pool()
        pool2 = get_mcp_pool()

        assert pool1 is pool2

    def test_get_mcp_pool_thread_safe(self):
        """Singleton creation should be thread-safe."""
        # Import fresh module state
        import gateway.mcp_pool as mcp_pool_module

        # Clear the instance
        mcp_pool_module._pool_instance = None

        results = []

        def create_pool():
            pool = mcp_pool_module.get_mcp_pool()
            results.append(pool)

        threads = [threading.Thread(target=create_pool) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should have the same instance
        assert len(set(id(r) for r in results)) == 1


class TestHealthCheckHandler:
    """Test health_check_handler function."""

    def test_health_check_handler_success(self):
        """Should return JSON with health check results."""
        with patch("gateway.mcp_pool.get_mcp_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.health_check = MagicMock(
                return_value={
                    "alexandria": {
                        "healthy": True,
                        "latency_ms": 2.5,
                        "last_checked": "2026-03-07T10:00:00",
                    }
                }
            )
            mock_get_pool.return_value = mock_pool

            result = health_check_handler()

            assert isinstance(result, str)
            data = json.loads(result)
            assert "status" in data
            assert "healthy" in data
            assert "total" in data
            assert "servers" in data

    def test_health_check_handler_degraded(self):
        """Should report degraded if not all servers healthy."""
        with patch("gateway.mcp_pool.get_mcp_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.health_check = MagicMock(
                return_value={
                    "alexandria": {"healthy": True, "latency_ms": 2.5},
                    "v11": {"healthy": False, "latency_ms": None},
                }
            )
            mock_get_pool.return_value = mock_pool

            result = health_check_handler()

            data = json.loads(result)
            assert data["status"] == "degraded"
            assert data["healthy"] == 1
            assert data["total"] == 2

    def test_health_check_handler_all_healthy(self):
        """Should report ok if all servers healthy."""
        with patch("gateway.mcp_pool.get_mcp_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.health_check = MagicMock(
                return_value={
                    "alexandria": {"healthy": True, "latency_ms": 1.2},
                    "v11": {"healthy": True, "latency_ms": 2.3},
                }
            )
            mock_get_pool.return_value = mock_pool

            result = health_check_handler()

            data = json.loads(result)
            assert data["status"] == "ok"
            assert data["healthy"] == 2
            assert data["total"] == 2

    def test_health_check_handler_exception(self):
        """Should return error JSON on exception."""
        with patch("gateway.mcp_pool.get_mcp_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_pool.health_check = MagicMock(
                side_effect=RuntimeError("Pool error")
            )
            mock_get_pool.return_value = mock_pool

            result = health_check_handler()

            data = json.loads(result)
            assert data["status"] == "error"
            assert "error" in data


class TestConcurrency:
    """Test thread safety of MCPPool."""

    def test_concurrent_get_connection(self):
        """Multiple threads should safely access get_connection."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_session = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=mock_session)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        pool._mcp_tool = mock_mcp_tool

        results = []

        def get_conn():
            result = pool.get_connection("alexandria")
            results.append(result)

        threads = [threading.Thread(target=get_conn) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results should be valid
        assert len(results) == 10
        assert all(r["name"] == "alexandria" for r in results)

    def test_concurrent_health_check_and_get_connection(self):
        """Health check and get_connection should not deadlock."""
        pool = MCPPool()

        mock_mcp_tool = MagicMock()
        mock_session = MagicMock()
        mock_server = MockMCPServerTask("alexandria", session=mock_session)
        mock_mcp_tool._servers = {"alexandria": mock_server}
        mock_mcp_tool._lock = threading.Lock()
        mock_mcp_tool._run_on_mcp_loop = MagicMock(return_value=True)
        pool._mcp_tool = mock_mcp_tool
        pool._get_server_config = MagicMock(
            return_value={"alexandria": {}}
        )

        def get_conn():
            for _ in range(5):
                pool.get_connection("alexandria")

        def health_check():
            for _ in range(5):
                pool.health_check()

        threads = [
            threading.Thread(target=get_conn),
            threading.Thread(target=health_check),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Both threads should complete without timeout
        assert all(not t.is_alive() for t in threads)
