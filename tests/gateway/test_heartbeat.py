"""Tests for gateway/heartbeat.py — HeartbeatScheduler configuration and execution."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, call
from datetime import datetime

import pytest

from gateway.heartbeat import HeartbeatScheduler, HeartbeatTask


class TestHeartbeatTaskCreation:
    """Test HeartbeatTask initialization."""

    def test_basic_creation(self):
        task = HeartbeatTask(
            name="test_task",
            schedule_ticks=5,
            handler_path="module.function",
        )
        assert task.name == "test_task"
        assert task.schedule_ticks == 5
        assert task.handler_path == "module.function"
        assert task.enabled is True
        assert task.handler is None
        assert task.last_run is None
        assert task.success_count == 0
        assert task.failure_count == 0

    def test_disabled_task(self):
        task = HeartbeatTask(
            name="disabled",
            schedule_ticks=1,
            handler_path="module.func",
            enabled=False,
        )
        assert task.enabled is False


class TestHeartbeatSchedulerHandlerResolution:
    """Test handler resolution via importlib."""

    def test_resolve_valid_handler(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        handler = scheduler._resolve_handler("cron.scheduler.tick")
        assert handler is not None
        assert callable(handler)

    def test_resolve_invalid_module(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        handler = scheduler._resolve_handler("nonexistent_module.function")
        assert handler is None

    def test_resolve_invalid_function(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        handler = scheduler._resolve_handler("pathlib.nonexistent_func")
        assert handler is None

    def test_resolve_invalid_path_format(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        handler = scheduler._resolve_handler("invalid_path_no_dot")
        assert handler is None


class TestHeartbeatSchedulerRegistration:
    """Test task registration."""

    def test_register_valid_task(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        scheduler.tasks = {}
        scheduler.register_task(
            name="test",
            schedule_ticks=5,
            handler_path="pathlib.Path",
        )
        assert "test" in scheduler.tasks
        assert scheduler.tasks["test"].name == "test"
        assert scheduler.tasks["test"].schedule_ticks == 5

    def test_register_invalid_handler(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        scheduler.tasks = {}
        with pytest.raises(ValueError, match="Cannot resolve handler"):
            scheduler.register_task(
                name="test",
                schedule_ticks=5,
                handler_path="nonexistent.function",
            )

    def test_register_missing_name(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        scheduler.tasks = {}
        with pytest.raises(ValueError):
            scheduler.register_task(
                name="",
                schedule_ticks=5,
                handler_path="pathlib.Path",
            )

    def test_register_missing_handler_path(self):
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        scheduler.tasks = {}
        with pytest.raises(ValueError):
            scheduler.register_task(
                name="test",
                schedule_ticks=5,
                handler_path="",
            )


class TestHeartbeatSchedulerTicking:
    """Test task execution on ticks."""

    def test_tick_executes_due_task(self):
        """Task with schedule_ticks=1 fires every tick."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="every_tick",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"every_tick": task}

        # Tick 0
        scheduler.tick(tick_count=0)
        assert mock_handler.call_count == 1

        # Tick 1
        scheduler.tick(tick_count=1)
        assert mock_handler.call_count == 2

    def test_tick_skips_disabled_task(self):
        """Disabled tasks never fire."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="disabled",
            schedule_ticks=1,
            handler_path="pathlib.Path",
            enabled=False,
        )
        task.handler = mock_handler
        scheduler.tasks = {"disabled": task}

        scheduler.tick(tick_count=0)
        assert mock_handler.call_count == 0

    def test_tick_respects_schedule_ticks(self):
        """Task with schedule_ticks=5 fires only at tick 0, 5, 10, etc."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="every_5",
            schedule_ticks=5,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"every_5": task}

        # Ticks 0-4: no execution
        for i in range(5):
            scheduler.tick(tick_count=i)
        assert mock_handler.call_count == 1  # Fired at tick 0

        # Tick 5: fires
        scheduler.tick(tick_count=5)
        assert mock_handler.call_count == 2

        # Tick 10: fires
        scheduler.tick(tick_count=10)
        assert mock_handler.call_count == 3

    def test_tick_passes_context_kwargs(self):
        """Context kwargs are passed to handlers that accept them."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)

        def mock_handler(adapters=None, main_loop=None):
            pass

        task = HeartbeatTask(
            name="test",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"test": task}

        # Wrap in Mock to track calls
        with patch("gateway.heartbeat.HeartbeatTask.handler", mock_handler):
            scheduler.tick(
                tick_count=0,
                adapters={"mock": "adapter"},
                main_loop="loop",
            )

        # Successfully executed without error
        assert task.success_count == 1

    def test_tick_handler_exception_tracked(self):
        """Handler exceptions are caught and failure count updated."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock(side_effect=ValueError("test error"))
        task = HeartbeatTask(
            name="failing",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"failing": task}

        scheduler.tick(tick_count=0)
        assert task.failure_count == 1
        assert task.success_count == 0

    def test_tick_handler_success_tracked(self):
        """Successful handler calls increment success count."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="success",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"success": task}

        scheduler.tick(tick_count=0)
        assert task.success_count == 1
        assert task.failure_count == 0

    def test_tick_updates_last_run_timestamp(self):
        """Task last_run is updated after execution."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="test",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"test": task}

        assert task.last_run is None
        before = datetime.now()
        scheduler.tick(tick_count=0)
        after = datetime.now()

        assert task.last_run is not None
        assert before <= task.last_run <= after

    def test_tick_multiple_tasks(self):
        """Multiple tasks can coexist and run independently."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler_1 = Mock()
        mock_handler_5 = Mock()

        task1 = HeartbeatTask(
            name="every_1",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task1.handler = mock_handler_1

        task5 = HeartbeatTask(
            name="every_5",
            schedule_ticks=5,
            handler_path="pathlib.Path",
        )
        task5.handler = mock_handler_5

        scheduler.tasks = {"every_1": task1, "every_5": task5}

        # Tick 0: both fire
        scheduler.tick(tick_count=0)
        assert mock_handler_1.call_count == 1
        assert mock_handler_5.call_count == 1

        # Tick 1-4: only every_1 fires
        for i in range(1, 5):
            scheduler.tick(tick_count=i)
        assert mock_handler_1.call_count == 5
        assert mock_handler_5.call_count == 1

        # Tick 5: both fire again
        scheduler.tick(tick_count=5)
        assert mock_handler_1.call_count == 6
        assert mock_handler_5.call_count == 2


class TestHeartbeatSchedulerStatus:
    """Test status reporting."""

    def test_get_status_empty(self):
        """Status of empty scheduler."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        scheduler.tasks = {}
        status = scheduler.get_status()
        assert status == {}

    def test_get_status_single_task(self):
        """Status includes task metrics."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        mock_handler = Mock()
        task = HeartbeatTask(
            name="test",
            schedule_ticks=5,
            handler_path="pathlib.Path",
        )
        task.handler = mock_handler
        scheduler.tasks = {"test": task}

        # Before any execution
        status = scheduler.get_status()
        assert status["test"]["last_run"] is None
        assert status["test"]["success_count"] == 0
        assert status["test"]["failure_count"] == 0
        assert status["test"]["enabled"] is True

        # After execution
        scheduler.tick(tick_count=0)
        status = scheduler.get_status()
        assert status["test"]["last_run"] is not None
        assert status["test"]["success_count"] == 1
        assert status["test"]["failure_count"] == 0
        assert status["test"]["next_run_tick"] == 5

    def test_get_status_multiple_tasks(self):
        """Status includes all tasks."""
        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        task1 = HeartbeatTask(
            name="task1",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task1.handler = Mock()
        task2 = HeartbeatTask(
            name="task2",
            schedule_ticks=5,
            handler_path="pathlib.Path",
            enabled=False,
        )
        task2.handler = Mock()
        scheduler.tasks = {"task1": task1, "task2": task2}

        status = scheduler.get_status()
        assert "task1" in status
        assert "task2" in status
        assert status["task2"]["enabled"] is False


class TestHeartbeatSchedulerConfigLoading:
    """Test config loading from config.yaml."""

    def test_load_config_missing_file(self):
        """Missing config file is handled gracefully."""
        scheduler = HeartbeatScheduler(config_path="/nonexistent/config.yaml")
        # Should not raise, tasks will be empty
        assert scheduler.tasks == {}

    def test_load_config_invalid_yaml(self):
        """Invalid YAML is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text("{invalid: yaml: content")
            scheduler = HeartbeatScheduler(config_path=str(config_file))
            assert scheduler.tasks == {}

    def test_load_config_heartbeat_disabled(self):
        """When heartbeat.enabled=false, no tasks are loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text("""
heartbeat:
  enabled: false
  tasks:
    - name: test
      schedule_ticks: 1
      handler: "pathlib.Path"
""")
            scheduler = HeartbeatScheduler(config_path=str(config_file))
            assert scheduler.tasks == {}

    def test_load_config_valid_tasks(self):
        """Valid tasks are loaded from config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text("""
heartbeat:
  enabled: true
  tasks:
    - name: task1
      schedule_ticks: 1
      handler: "pathlib.Path"
      enabled: true
    - name: task2
      schedule_ticks: 5
      handler: "pathlib.Path"
      enabled: false
""")
            scheduler = HeartbeatScheduler(config_path=str(config_file))
            assert "task1" in scheduler.tasks
            assert scheduler.tasks["task1"].enabled is True
            assert "task2" in scheduler.tasks
            assert scheduler.tasks["task2"].enabled is False

    def test_load_config_invalid_task_handler(self):
        """Task with invalid handler path is skipped with warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text("""
heartbeat:
  enabled: true
  tasks:
    - name: invalid
      schedule_ticks: 1
      handler: "nonexistent.handler"
    - name: valid
      schedule_ticks: 1
      handler: "pathlib.Path"
""")
            scheduler = HeartbeatScheduler(config_path=str(config_file))
            # Invalid task should not be registered
            assert "invalid" not in scheduler.tasks
            # Valid task should be registered
            assert "valid" in scheduler.tasks


class TestHeartbeatSchedulerHandlerSignatures:
    """Test handlers with different signatures."""

    def test_handler_no_params(self):
        """Handler with no parameters works."""
        def handler_no_params():
            pass

        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        task = HeartbeatTask(
            name="test",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = handler_no_params
        scheduler.tasks = {"test": task}

        # Should not raise
        scheduler.tick(tick_count=0, adapters={}, main_loop=None)
        assert task.success_count == 1

    def test_handler_selective_params(self):
        """Handler accepts only the params it needs."""
        params_received = {}

        def handler_selective(adapters=None, **kwargs):
            params_received["adapters"] = adapters

        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        task = HeartbeatTask(
            name="test",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = handler_selective
        scheduler.tasks = {"test": task}

        scheduler.tick(
            tick_count=0,
            adapters={"test": "adapter"},
            main_loop="loop",
            extra_param="extra",
        )

        assert params_received["adapters"] == {"test": "adapter"}
        assert task.success_count == 1

    def test_handler_with_kwargs(self):
        """Handler accepting **kwargs receives all context."""
        params_received = {}

        def handler_with_kwargs(**kwargs):
            params_received.update(kwargs)

        scheduler = HeartbeatScheduler.__new__(HeartbeatScheduler)
        task = HeartbeatTask(
            name="test",
            schedule_ticks=1,
            handler_path="pathlib.Path",
        )
        task.handler = handler_with_kwargs
        scheduler.tasks = {"test": task}

        scheduler.tick(
            tick_count=0,
            adapters={"test": "adapter"},
            main_loop="loop",
        )

        assert params_received.get("adapters") == {"test": "adapter"}
        assert params_received.get("main_loop") == "loop"
