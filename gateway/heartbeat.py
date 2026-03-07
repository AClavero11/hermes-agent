"""
Heartbeat Pattern - General-purpose proactive scheduler for gateway tasks.

Provides a config-driven task scheduler that replaces hardcoded cron ticking,
channel directory refresh, ILS polling, and cache cleanup logic.

Tasks are registered via config.yaml heartbeat section and executed on a
tick interval (default 60 seconds).
"""

import logging
import importlib
import inspect
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatTask:
    """Configuration for a single heartbeat task."""
    name: str
    schedule_ticks: int  # Run when tick_count % schedule_ticks == 0
    handler_path: str    # Dotted import path: "module.submodule.function"
    enabled: bool = True
    handler: Optional[Callable] = field(default=None, init=False)
    last_run: Optional[datetime] = field(default=None, init=False)
    next_run_tick: Optional[int] = field(default=None, init=False)
    success_count: int = field(default=0, init=False)
    failure_count: int = field(default=0, init=False)


class HeartbeatScheduler:
    """
    Config-driven task scheduler for gateway background operations.

    Loads tasks from config.yaml heartbeat section and executes them on
    a regular tick interval. Handlers are resolved via importlib and
    called with **context kwargs (they accept what they need).
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the heartbeat scheduler.

        Args:
            config_path: Optional explicit config path (defaults to ~/.hermes/config.yaml)
        """
        self.tasks: Dict[str, HeartbeatTask] = {}
        self.config_path = config_path
        self._load_config()

    def _load_config(self) -> None:
        """Load tasks from config.yaml heartbeat section."""
        config_path = self.config_path
        if not config_path:
            from pathlib import Path
            config_path = str(Path.home() / ".hermes" / "config.yaml")

        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
            return

        heartbeat_config = config.get("heartbeat", {})
        if not heartbeat_config.get("enabled", True):
            logger.info("Heartbeat scheduler disabled in config")
            return

        tasks_config = heartbeat_config.get("tasks", [])
        for task_config in tasks_config:
            try:
                self.register_task(
                    name=task_config.get("name", ""),
                    schedule_ticks=task_config.get("schedule_ticks", 1),
                    handler_path=task_config.get("handler", ""),
                    enabled=task_config.get("enabled", True),
                )
            except Exception as e:
                logger.error("Failed to register task %s: %s", task_config.get("name"), e)

    def register_task(
        self,
        name: str,
        schedule_ticks: int,
        handler_path: str,
        enabled: bool = True,
    ) -> None:
        """
        Register a single task.

        Args:
            name: Task identifier
            schedule_ticks: Run when tick_count % schedule_ticks == 0
            handler_path: Dotted import path (e.g., "cron.scheduler.tick")
            enabled: Whether the task is enabled

        Raises:
            ValueError: If handler_path cannot be resolved
        """
        if not name or not handler_path:
            raise ValueError(f"Task requires name and handler_path")

        # Try to resolve the handler function immediately
        handler = self._resolve_handler(handler_path)
        if not handler:
            raise ValueError(f"Cannot resolve handler: {handler_path}")

        task = HeartbeatTask(
            name=name,
            schedule_ticks=schedule_ticks,
            handler_path=handler_path,
            enabled=enabled,
        )
        task.handler = handler
        task.next_run_tick = schedule_ticks  # First run after first full cycle

        self.tasks[name] = task
        logger.info("Registered heartbeat task: %s (every %d ticks)", name, schedule_ticks)

    def _resolve_handler(self, handler_path: str) -> Optional[Callable]:
        """
        Resolve a dotted handler path to a callable.

        Args:
            handler_path: e.g., "cron.scheduler.tick" or "gateway.channel_directory.build_channel_directory"

        Returns:
            The callable, or None if not found
        """
        parts = handler_path.rsplit(".", 1)
        if len(parts) != 2:
            logger.error("Invalid handler path: %s (must be 'module.function')", handler_path)
            return None

        module_path, func_name = parts
        try:
            module = importlib.import_module(module_path)
            handler = getattr(module, func_name, None)
            if not handler or not callable(handler):
                logger.error("Handler not found or not callable: %s.%s", module_path, func_name)
                return None
            return handler
        except ImportError as e:
            logger.error("Failed to import module %s: %s", module_path, e)
            return None

    def tick(self, tick_count: int, **context: Any) -> None:
        """
        Execute scheduled tasks for this tick.

        Called regularly (e.g., every 60 seconds from the cron ticker).
        For each enabled task where tick_count % schedule_ticks == 0,
        calls the handler with **context kwargs.

        Args:
            tick_count: The current tick number (starts at 0 or 1)
            **context: Additional context passed to handlers:
                - adapters: Platform adapters dict
                - main_loop: Event loop for async operations
                - any other kwargs handlers might need
        """
        for task in self.tasks.values():
            if not task.enabled:
                continue

            if tick_count % task.schedule_ticks != 0:
                continue

            self._run_task(task, tick_count, **context)

    def _run_task(self, task: HeartbeatTask, tick_count: int, **context: Any) -> None:
        """
        Execute a single task and track its status.

        Args:
            task: The task to run
            tick_count: Current tick count
            **context: Context to pass to the handler
        """
        try:
            task.last_run = datetime.now()
            task.next_run_tick = tick_count + task.schedule_ticks

            # Inspect handler signature to pass only what it accepts
            sig = inspect.signature(task.handler)
            kwargs = {}
            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )

            if has_var_keyword:
                # Handler accepts **kwargs, pass all context
                kwargs = context.copy()
            else:
                # Handler has specific params, pass only matching ones
                for param_name in sig.parameters:
                    if param_name in context:
                        kwargs[param_name] = context[param_name]

            logger.debug("Executing heartbeat task: %s (tick %d)", task.name, tick_count)
            assert task.handler is not None, f"Task {task.name} has no handler"
            task.handler(**kwargs)
            task.success_count += 1
            logger.debug("Heartbeat task completed: %s", task.name)

        except Exception as e:
            task.failure_count += 1
            logger.error("Heartbeat task failed: %s: %s", task.name, e, exc_info=True)

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """
        Get status of all registered tasks.

        Returns:
            Dict mapping task name to status dict with:
            - last_run: ISO format timestamp of last execution
            - next_run_tick: Tick at which task will run next
            - success_count: Number of successful runs
            - failure_count: Number of failed runs
            - enabled: Whether the task is currently enabled
        """
        status = {}
        for name, task in self.tasks.items():
            status[name] = {
                "enabled": task.enabled,
                "last_run": task.last_run.isoformat() if task.last_run else None,
                "next_run_tick": task.next_run_tick,
                "success_count": task.success_count,
                "failure_count": task.failure_count,
            }
        return status
