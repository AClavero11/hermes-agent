"""
Lobster Deterministic YAML Workflows — Deterministic execution for known sequences.

Provides a YAML-driven workflow engine that bypasses LLM reasoning for known task
sequences. Workflows define steps that execute sequentially, with variable
interpolation and error handling, enabling faster and more reliable execution
of recurring patterns.

Design:
- Load YAML workflow definitions from ~/.hermes/workflows/
- Parse steps with action, input template, output variable, and error handling
- Execute steps sequentially, passing outputs between steps via context
- Log execution with step timings and success/failure tracking
- Match user messages against workflow triggers (regex or keywords)
"""

import logging
import importlib
import inspect
import re
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """Single step in a workflow execution."""
    action: str              # Tool/function name or dotted path to call
    input_template: str      # String template with {{ var }} placeholders
    output: str              # Variable name to store result
    on_error: str = "abort"  # "skip", "abort", or "retry"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "input": self.input_template,
            "output": self.output,
            "on_error": self.on_error,
        }


@dataclass
class StepExecution:
    """Result of executing a single step."""
    step: WorkflowStep
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0
    success: bool = False
    output: Any = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.step.action,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class Workflow:
    """Parsed YAML workflow definition."""
    name: str
    description: str
    trigger: str              # Regex pattern or keyword to match
    steps: List[WorkflowStep]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class WorkflowExecution:
    """Result of executing a complete workflow."""
    workflow: Workflow
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0
    success: bool = False
    context: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepExecution] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_name": self.workflow.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
        }


class WorkflowEngine:
    """
    YAML-driven deterministic workflow executor.

    Loads workflow definitions from YAML files, matches user messages against
    workflow triggers, and executes workflows step-by-step with variable
    interpolation and error handling.
    """

    def __init__(self, workflows_dir: Optional[str] = None):
        """
        Initialize the workflow engine.

        Args:
            workflows_dir: Directory containing YAML workflow files.
                          Defaults to ~/.hermes/workflows/
        """
        if workflows_dir is None:
            hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
            workflows_dir = str(hermes_home / "workflows")

        self.workflows_dir = Path(workflows_dir)
        self.workflows: Dict[str, Workflow] = {}
        self.executions: List[WorkflowExecution] = []

    def load(self, path: str) -> Optional[Workflow]:
        """
        Parse a single YAML workflow file.

        Args:
            path: Path to YAML workflow file.

        Returns:
            Workflow object, or None if parse failed.
        """
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("Failed to load workflow from %s: %s", path, e)
            return None

        try:
            workflow = self._parse_workflow(data)
            if workflow:
                self.workflows[workflow.name] = workflow
                logger.info("Loaded workflow: %s", workflow.name)
            return workflow
        except Exception as e:
            logger.error("Failed to parse workflow from %s: %s", path, e)
            return None

    def load_all(self) -> List[Workflow]:
        """
        Load all YAML workflow files from workflows_dir.

        Returns:
            List of loaded Workflow objects.
        """
        if not self.workflows_dir.exists():
            logger.warning("Workflows directory does not exist: %s", self.workflows_dir)
            return []

        workflows = []
        for yaml_file in self.workflows_dir.glob("*.yaml"):
            workflow = self.load(str(yaml_file))
            if workflow:
                workflows.append(workflow)

        logger.info("Loaded %d workflows from %s", len(workflows), self.workflows_dir)
        return workflows

    def _parse_workflow(self, data: Dict[str, Any]) -> Optional[Workflow]:
        """
        Parse workflow YAML data into a Workflow object.

        Args:
            data: Parsed YAML data.

        Returns:
            Workflow object, or None if required fields are missing.

        Raises:
            ValueError: If workflow structure is invalid.
        """
        name = data.get("name")
        if not name:
            raise ValueError("Workflow missing required field: name")

        description = data.get("description", "")
        trigger = data.get("trigger")
        if not trigger:
            raise ValueError(f"Workflow '{name}' missing required field: trigger")

        steps_data = data.get("steps", [])
        if not steps_data:
            raise ValueError(f"Workflow '{name}' missing steps")

        steps = []
        for i, step_data in enumerate(steps_data):
            try:
                step = self._parse_step(step_data, i, name)
                steps.append(step)
            except ValueError as e:
                raise ValueError(f"Workflow '{name}' step {i}: {e}")

        return Workflow(
            name=name,
            description=description,
            trigger=trigger,
            steps=steps,
        )

    def _parse_step(self, data: Dict[str, Any], _index: int, _workflow_name: str) -> WorkflowStep:
        """
        Parse a single workflow step.

        Args:
            data: Step data from YAML.
            index: Step index (for error reporting).
            workflow_name: Workflow name (for error reporting).

        Returns:
            WorkflowStep object.

        Raises:
            ValueError: If required fields are missing.
        """
        action = data.get("action")
        if not action:
            raise ValueError("Missing required field: action")

        input_template = data.get("input", "")
        output = data.get("output")
        if not output:
            raise ValueError("Missing required field: output")

        on_error = data.get("on_error", "abort")
        if on_error not in ("skip", "abort", "retry"):
            raise ValueError(f"on_error must be 'skip', 'abort', or 'retry', got '{on_error}'")

        return WorkflowStep(
            action=action,
            input_template=input_template,
            output=output,
            on_error=on_error,
        )

    def match(self, user_message: str) -> Optional[Workflow]:
        """
        Match a user message against workflow triggers.

        Tries to match message against each workflow's trigger pattern
        (as a regex). Returns the first matching workflow.

        Args:
            user_message: User input message.

        Returns:
            Matching Workflow, or None if no match.
        """
        for workflow in self.workflows.values():
            try:
                if re.search(workflow.trigger, user_message, re.IGNORECASE):
                    logger.debug("Matched workflow '%s' for message: %s", workflow.name, user_message[:50])
                    return workflow
            except re.error as e:
                logger.error("Invalid regex in workflow trigger '%s': %s", workflow.name, e)

        return None

    def execute(self, workflow: Workflow, context: Dict[str, Any]) -> WorkflowExecution:
        """
        Execute a workflow step-by-step.

        Runs each step sequentially, interpolating variables from context,
        and storing outputs back into context for use by later steps.

        Args:
            workflow: Workflow to execute.
            context: Initial context dict (variables available to steps).

        Returns:
            WorkflowExecution with results and timing info.
        """
        execution = WorkflowExecution(
            workflow=workflow,
            start_time=datetime.now(),
            context=context.copy(),
        )

        logger.info("Executing workflow: %s", workflow.name)

        for step in workflow.steps:
            step_exec = self._execute_step(step, execution.context)
            execution.steps.append(step_exec)

            # Store output in context for later steps
            if step_exec.success:
                execution.context[step.output] = step_exec.output
                logger.debug("Step '%s' output stored in '%s'", step.action, step.output)
            else:
                # Handle error
                if step.on_error == "abort":
                    execution.error = f"Step '{step.action}' failed: {step_exec.error}"
                    logger.error("Workflow aborted: %s", execution.error)
                    break
                elif step.on_error == "skip":
                    logger.warning("Step '%s' skipped due to error: %s", step.action, step_exec.error)
                    continue
                elif step.on_error == "retry":
                    logger.info("Retrying step '%s'", step.action)
                    step_exec = self._execute_step(step, execution.context)
                    execution.steps[-1] = step_exec
                    if not step_exec.success:
                        execution.error = f"Step '{step.action}' failed after retry: {step_exec.error}"
                        logger.error("Workflow aborted: %s", execution.error)
                        break
                    execution.context[step.output] = step_exec.output

        execution.end_time = datetime.now()
        execution.duration_ms = (execution.end_time - execution.start_time).total_seconds() * 1000
        execution.success = execution.error is None

        self.executions.append(execution)

        if execution.success:
            logger.info("Workflow '%s' completed successfully in %.1f ms", workflow.name, execution.duration_ms)
        else:
            logger.error("Workflow '%s' failed: %s", workflow.name, execution.error)

        return execution

    def _execute_step(self, step: WorkflowStep, context: Dict[str, Any]) -> StepExecution:
        """
        Execute a single workflow step.

        Args:
            step: Step to execute.
            context: Current context (for variable interpolation).

        Returns:
            StepExecution with result and timing.
        """
        step_exec = StepExecution(
            step=step,
            start_time=datetime.now(),
        )

        try:
            # Interpolate variables in input template
            interpolated_input = self._interpolate(step.input_template, context)
            logger.debug("Step '%s' input: %s", step.action, interpolated_input[:100] if isinstance(interpolated_input, str) else interpolated_input)

            # Resolve and call the handler
            handler = self._resolve_handler(step.action)
            if not handler:
                step_exec.error = f"Cannot resolve handler: {step.action}"
                step_exec.end_time = datetime.now()
                step_exec.duration_ms = (step_exec.end_time - step_exec.start_time).total_seconds() * 1000
                logger.error("Step '%s' error: %s", step.action, step_exec.error)
                return step_exec

            # Call handler with input (support both positional and keyword args)
            sig = inspect.signature(handler)
            if len(sig.parameters) > 0:
                step_exec.output = handler(interpolated_input)
            else:
                step_exec.output = handler()

            step_exec.success = True
            step_exec.end_time = datetime.now()
            step_exec.duration_ms = (step_exec.end_time - step_exec.start_time).total_seconds() * 1000

            logger.debug("Step '%s' completed in %.1f ms", step.action, step_exec.duration_ms)

        except Exception as e:
            step_exec.error = str(e)
            step_exec.end_time = datetime.now()
            step_exec.duration_ms = (step_exec.end_time - step_exec.start_time).total_seconds() * 1000
            logger.error("Step '%s' raised exception: %s", step.action, e, exc_info=True)

        return step_exec

    def _interpolate(self, template: str, context: Dict[str, Any]) -> str:
        """
        Interpolate {{ var_name }} placeholders in a template string.

        Args:
            template: String with {{ var }} placeholders.
            context: Dict of variable values.

        Returns:
            Interpolated string.
        """
        def replace_var(match):
            var_name = match.group(1).strip()
            value = context.get(var_name, f"{{{{ {var_name} }}}}")
            return str(value)

        return re.sub(r'\{\{\s*(\w+)\s*\}\}', replace_var, template)

    def _resolve_handler(self, handler_path: str) -> Optional[Callable]:
        """
        Resolve a handler path to a callable.

        Supports:
        - Built-in actions: "log", "format", "shell"
        - Dotted imports: "module.submodule.function"

        Args:
            handler_path: Handler identifier or dotted path.

        Returns:
            Callable handler, or None if not found.
        """
        # Built-in handlers
        if handler_path == "log":
            return self._builtin_log
        elif handler_path == "format":
            return self._builtin_format
        elif handler_path == "shell":
            return self._builtin_shell

        # Custom dotted path
        parts = handler_path.rsplit(".", 1)
        if len(parts) != 2:
            logger.error("Invalid handler path: %s (must be 'module.function' or a built-in)", handler_path)
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

    @staticmethod
    def _builtin_log(input_text: str) -> str:
        """Built-in log action — logs input and returns it."""
        logger.info("Workflow: %s", input_text)
        return input_text

    @staticmethod
    def _builtin_format(input_text: str) -> str:
        """Built-in format action — returns input as-is (simple passthrough)."""
        return input_text

    @staticmethod
    def _builtin_shell(input_text: str) -> str:
        """Built-in shell action — executes command and returns output."""
        import subprocess
        try:
            result = subprocess.run(
                input_text,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout + (result.stderr if result.returncode != 0 else "")
        except subprocess.TimeoutExpired:
            return "Command timed out"
        except Exception as e:
            return f"Command failed: {e}"

    def get_status(self) -> Dict[str, Any]:
        """
        Get engine status and execution history.

        Returns:
            Dict with loaded workflows count and recent executions.
        """
        return {
            "workflows_loaded": len(self.workflows),
            "workflows_dir": str(self.workflows_dir),
            "total_executions": len(self.executions),
            "recent_executions": [
                {
                    "workflow": e.workflow.name,
                    "success": e.success,
                    "duration_ms": e.duration_ms,
                    "timestamp": e.start_time.isoformat(),
                }
                for e in self.executions[-10:]  # Last 10
            ],
        }
