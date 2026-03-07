"""Tests for gateway/workflow_engine.py — WorkflowEngine YAML parsing and execution."""

import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

from gateway.workflow_engine import (
    WorkflowStep,
    Workflow,
    WorkflowEngine,
    StepExecution,
    WorkflowExecution,
)


class TestWorkflowStepCreation:
    """Test WorkflowStep initialization."""

    def test_basic_creation(self):
        step = WorkflowStep(
            action="test_action",
            input_template="test input",
            output="result",
        )
        assert step.action == "test_action"
        assert step.input_template == "test input"
        assert step.output == "result"
        assert step.on_error == "abort"

    def test_with_on_error(self):
        step = WorkflowStep(
            action="test_action",
            input_template="input",
            output="result",
            on_error="skip",
        )
        assert step.on_error == "skip"

    def test_to_dict(self):
        step = WorkflowStep(
            action="test",
            input_template="{{ var }}",
            output="out",
            on_error="retry",
        )
        data = step.to_dict()
        assert data["action"] == "test"
        assert data["input"] == "{{ var }}"
        assert data["output"] == "out"
        assert data["on_error"] == "retry"


class TestWorkflowCreation:
    """Test Workflow initialization."""

    def test_basic_creation(self):
        steps = [
            WorkflowStep("action1", "input1", "out1"),
            WorkflowStep("action2", "input2", "out2"),
        ]
        workflow = Workflow(
            name="test_workflow",
            description="Test workflow",
            trigger="test.*pattern",
            steps=steps,
        )
        assert workflow.name == "test_workflow"
        assert workflow.description == "Test workflow"
        assert workflow.trigger == "test.*pattern"
        assert len(workflow.steps) == 2

    def test_to_dict(self):
        steps = [WorkflowStep("action", "input", "out")]
        workflow = Workflow(
            name="test",
            description="desc",
            trigger="trigger",
            steps=steps,
        )
        data = workflow.to_dict()
        assert data["name"] == "test"
        assert data["description"] == "desc"
        assert data["trigger"] == "trigger"
        assert len(data["steps"]) == 1


class TestWorkflowEngineParsing:
    """Test WorkflowEngine YAML parsing."""

    def test_parse_valid_workflow(self):
        yaml_data = {
            "name": "test",
            "description": "Test workflow",
            "trigger": "test.*",
            "steps": [
                {
                    "action": "log",
                    "input": "test input",
                    "output": "result",
                },
            ],
        }
        engine = WorkflowEngine()
        workflow = engine._parse_workflow(yaml_data)
        assert workflow is not None
        assert workflow.name == "test"
        assert len(workflow.steps) == 1
        assert workflow.steps[0].action == "log"

    def test_parse_missing_name(self):
        yaml_data = {
            "description": "Test",
            "trigger": "test",
            "steps": [],
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="name"):
            engine._parse_workflow(yaml_data)

    def test_parse_missing_trigger(self):
        yaml_data = {
            "name": "test",
            "description": "Test",
            "steps": [],
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="trigger"):
            engine._parse_workflow(yaml_data)

    def test_parse_missing_steps(self):
        yaml_data = {
            "name": "test",
            "description": "Test",
            "trigger": "test",
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="steps"):
            engine._parse_workflow(yaml_data)

    def test_parse_step_missing_action(self):
        yaml_data = {
            "name": "test",
            "trigger": "test",
            "steps": [
                {
                    "input": "test",
                    "output": "result",
                },
            ],
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="action"):
            engine._parse_workflow(yaml_data)

    def test_parse_step_missing_output(self):
        yaml_data = {
            "name": "test",
            "trigger": "test",
            "steps": [
                {
                    "action": "log",
                    "input": "test",
                },
            ],
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="output"):
            engine._parse_workflow(yaml_data)

    def test_parse_step_invalid_on_error(self):
        yaml_data = {
            "name": "test",
            "trigger": "test",
            "steps": [
                {
                    "action": "log",
                    "input": "test",
                    "output": "result",
                    "on_error": "invalid",
                },
            ],
        }
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="on_error"):
            engine._parse_workflow(yaml_data)

    def test_parse_multiple_steps_with_defaults(self):
        yaml_data = {
            "name": "test",
            "trigger": "test",
            "steps": [
                {"action": "log", "input": "a", "output": "r1"},
                {"action": "format", "input": "b", "output": "r2", "on_error": "skip"},
                {"action": "shell", "input": "c", "output": "r3", "on_error": "retry"},
            ],
        }
        engine = WorkflowEngine()
        workflow = engine._parse_workflow(yaml_data)
        assert len(workflow.steps) == 3
        assert workflow.steps[0].on_error == "abort"
        assert workflow.steps[1].on_error == "skip"
        assert workflow.steps[2].on_error == "retry"


class TestWorkflowEngineVariableInterpolation:
    """Test variable interpolation in templates."""

    def test_simple_interpolation(self):
        engine = WorkflowEngine()
        result = engine._interpolate("Hello {{ name }}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_variables(self):
        engine = WorkflowEngine()
        result = engine._interpolate(
            "{{ greeting }} {{ name }}",
            {"greeting": "Hello", "name": "Alice"},
        )
        assert result == "Hello Alice"

    def test_missing_variable(self):
        engine = WorkflowEngine()
        result = engine._interpolate("Hello {{ name }}", {})
        assert result == "Hello {{ name }}"

    def test_whitespace_handling(self):
        engine = WorkflowEngine()
        result = engine._interpolate("Test {{ variable }}", {"variable": "value"})
        assert result == "Test value"

    def test_no_interpolation_needed(self):
        engine = WorkflowEngine()
        result = engine._interpolate("Plain text", {})
        assert result == "Plain text"

    def test_numeric_variable(self):
        engine = WorkflowEngine()
        result = engine._interpolate("Count: {{ count }}", {"count": 42})
        assert result == "Count: 42"


class TestWorkflowEngineTriggerMatching:
    """Test workflow trigger pattern matching."""

    def test_match_regex_pattern(self):
        engine = WorkflowEngine()
        workflow = Workflow(
            name="test",
            description="",
            trigger="daily.*report",
            steps=[],
        )
        engine.workflows["test"] = workflow

        assert engine.match("daily summary report") is not None
        assert engine.match("daily inventory report") is not None
        assert engine.match("weekly report") is None

    def test_match_case_insensitive(self):
        engine = WorkflowEngine()
        workflow = Workflow(
            name="test",
            description="",
            trigger="DAILY SUMMARY",
            steps=[],
        )
        engine.workflows["test"] = workflow

        assert engine.match("daily summary") is not None
        assert engine.match("Daily Summary") is not None
        assert engine.match("DAILY SUMMARY") is not None

    def test_match_no_match(self):
        engine = WorkflowEngine()
        workflow = Workflow(
            name="test",
            description="",
            trigger="specific pattern",
            steps=[],
        )
        engine.workflows["test"] = workflow

        assert engine.match("something else") is None

    def test_match_multiple_workflows_first_wins(self):
        engine = WorkflowEngine()
        w1 = Workflow("w1", "", "test.*", [])
        w2 = Workflow("w2", "", "test.*", [])
        engine.workflows["w1"] = w1
        engine.workflows["w2"] = w2

        match = engine.match("test message")
        assert match is not None

    def test_match_invalid_regex(self):
        engine = WorkflowEngine()
        workflow = Workflow(
            name="test",
            description="",
            trigger="[invalid(regex",
            steps=[],
        )
        engine.workflows["test"] = workflow

        # Should return None for invalid regex, not crash
        assert engine.match("test") is None


class TestWorkflowEngineHandlerResolution:
    """Test handler resolution."""

    def test_resolve_builtin_log(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("log")
        assert handler is not None
        assert callable(handler)

    def test_resolve_builtin_format(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("format")
        assert handler is not None
        assert callable(handler)

    def test_resolve_builtin_shell(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("shell")
        assert handler is not None
        assert callable(handler)

    def test_resolve_valid_import_path(self):
        engine = WorkflowEngine()
        # pathlib.Path exists
        handler = engine._resolve_handler("pathlib.Path")
        assert handler is not None
        assert callable(handler)

    def test_resolve_invalid_module(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("nonexistent_module_xyz.function")
        assert handler is None

    def test_resolve_invalid_function(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("pathlib.nonexistent_function_xyz")
        assert handler is None

    def test_resolve_invalid_format(self):
        engine = WorkflowEngine()
        handler = engine._resolve_handler("invalid_format_no_dot")
        assert handler is None

    def test_builtin_log_output(self):
        handler = WorkflowEngine._builtin_log
        result = handler("test message")
        assert result == "test message"

    def test_builtin_format_output(self):
        handler = WorkflowEngine._builtin_format
        result = handler("test text")
        assert result == "test text"

    def test_builtin_shell_simple_command(self):
        handler = WorkflowEngine._builtin_shell
        result = handler("echo hello")
        assert "hello" in result


class TestWorkflowEngineExecution:
    """Test step and workflow execution."""

    def test_execute_single_step_success(self):
        engine = WorkflowEngine()
        step = WorkflowStep("log", "test input", "result")
        step_exec = engine._execute_step(step, {})

        assert step_exec.success is True
        assert step_exec.output == "test input"
        assert step_exec.error is None
        assert step_exec.duration_ms >= 0

    def test_execute_step_with_interpolation(self):
        engine = WorkflowEngine()
        step = WorkflowStep("log", "Hello {{ name }}", "result")
        step_exec = engine._execute_step(step, {"name": "Alice"})

        assert step_exec.success is True
        assert step_exec.output == "Hello Alice"

    def test_execute_step_with_invalid_handler(self):
        engine = WorkflowEngine()
        step = WorkflowStep("invalid_handler_xyz", "input", "result")
        step_exec = engine._execute_step(step, {})

        assert step_exec.success is False
        assert step_exec.error is not None
        assert "Cannot resolve handler" in step_exec.error

    def test_execute_step_handler_exception(self):
        engine = WorkflowEngine()
        # shell handler with invalid command will raise
        step = WorkflowStep("shell", "exit 1", "result")
        step_exec = engine._execute_step(step, {})

        # Shell handler catches exceptions, should succeed or handle gracefully
        assert step_exec.end_time is not None
        assert step_exec.duration_ms >= 0

    def test_execute_workflow_success(self):
        engine = WorkflowEngine()
        steps = [
            WorkflowStep("format", "Hello {{ name }}", "greeting"),
            WorkflowStep("log", "{{ greeting }}", "final"),
        ]
        workflow = Workflow("test", "Test", "test", steps)

        execution = engine.execute(workflow, {"name": "World"})

        assert execution.success is True
        assert len(execution.steps) == 2
        assert execution.steps[0].success is True
        assert execution.steps[1].success is True
        assert execution.context["greeting"] == "Hello World"
        assert execution.context["final"] == "Hello World"

    def test_execute_workflow_abort_on_error(self):
        engine = WorkflowEngine()
        steps = [
            WorkflowStep("invalid_action", "input", "result1", on_error="abort"),
            WorkflowStep("log", "should not run", "result2"),
        ]
        workflow = Workflow("test", "Test", "test", steps)

        execution = engine.execute(workflow, {})

        assert execution.success is False
        assert len(execution.steps) == 1  # Only first step ran
        assert execution.error is not None

    def test_execute_workflow_skip_on_error(self):
        engine = WorkflowEngine()
        steps = [
            WorkflowStep("invalid_action", "input", "result1", on_error="skip"),
            WorkflowStep("log", "runs after skip", "result2"),
        ]
        workflow = Workflow("test", "Test", "test", steps)

        execution = engine.execute(workflow, {})

        assert execution.success is True
        assert len(execution.steps) == 2
        assert execution.steps[0].success is False
        assert execution.steps[1].success is True

    def test_execute_workflow_retry_on_error(self):
        engine = WorkflowEngine()

        # Create a mock handler that fails first, then succeeds
        call_count = [0]

        def failing_then_succeeding(input_text):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("First call fails")
            return f"Success on attempt {call_count[0]}"

        with patch.object(engine, '_resolve_handler') as mock_resolve:
            steps = [
                WorkflowStep("test_handler", "input", "result", on_error="retry"),
            ]
            workflow = Workflow("test", "Test", "test", steps)

            # Patch resolve to return our failing handler
            mock_resolve.return_value = failing_then_succeeding

            execution = engine.execute(workflow, {})

            # After retry, should succeed
            assert len(execution.steps) == 1
            # The step is replaced after retry, so check the final result
            assert execution.steps[0].success is True

    def test_execute_workflow_stores_results(self):
        engine = WorkflowEngine()
        execution = WorkflowExecution(
            workflow=Workflow("test", "", "test", []),
            start_time=datetime.now(),
            context={"initial": "value"},
        )

        # Manually add a step execution
        step = WorkflowStep("log", "input", "output")
        step_exec = engine._execute_step(step, execution.context)
        execution.steps.append(step_exec)

        assert len(execution.steps) == 1
        assert execution.steps[0].success is True

    def test_execution_timing(self):
        engine = WorkflowEngine()
        steps = [WorkflowStep("format", "test", "result")]
        workflow = Workflow("test", "Test", "test", steps)

        execution = engine.execute(workflow, {})

        assert execution.start_time is not None
        assert execution.end_time is not None
        assert execution.duration_ms > 0
        assert execution.steps[0].duration_ms > 0


class TestWorkflowEngineLoadAndLoadAll:
    """Test loading workflows from files."""

    def test_load_valid_yaml_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_file = Path(tmpdir) / "test.yaml"
            yaml_file.write_text(
                """
name: test_workflow
description: Test workflow
trigger: test.*
steps:
  - action: log
    input: test
    output: result
"""
            )

            engine = WorkflowEngine(workflows_dir=tmpdir)
            workflow = engine.load(str(yaml_file))

            assert workflow is not None
            assert workflow.name == "test_workflow"
            assert workflow in engine.workflows.values()

    def test_load_invalid_yaml_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_file = Path(tmpdir) / "bad.yaml"
            yaml_file.write_text("{ invalid yaml [")

            engine = WorkflowEngine(workflows_dir=tmpdir)
            workflow = engine.load(str(yaml_file))

            assert workflow is None

    def test_load_all_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create multiple workflow files
            (tmpdir / "workflow1.yaml").write_text(
                """
name: workflow1
trigger: test1
steps:
  - action: log
    input: test
    output: result
"""
            )

            (tmpdir / "workflow2.yaml").write_text(
                """
name: workflow2
trigger: test2
steps:
  - action: format
    input: test
    output: result
"""
            )

            engine = WorkflowEngine(workflows_dir=str(tmpdir))
            workflows = engine.load_all()

            assert len(workflows) == 2
            assert len(engine.workflows) == 2

    def test_load_all_nonexistent_directory(self):
        engine = WorkflowEngine(workflows_dir="/nonexistent/path")
        workflows = engine.load_all()

        assert workflows == []

    def test_load_all_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = WorkflowEngine(workflows_dir=tmpdir)
            workflows = engine.load_all()

            assert workflows == []

    def test_load_uses_default_workflows_dir(self):
        engine = WorkflowEngine()
        # Should not crash when using default dir
        assert engine.workflows_dir is not None


class TestWorkflowEngineStatus:
    """Test status reporting."""

    def test_get_status(self):
        engine = WorkflowEngine()
        workflow = Workflow("test", "Test", "test", [WorkflowStep("log", "input", "result")])
        engine.workflows["test"] = workflow

        status = engine.get_status()

        assert "workflows_loaded" in status
        assert status["workflows_loaded"] == 1
        assert "workflows_dir" in status
        assert "total_executions" in status
        assert "recent_executions" in status

    def test_get_status_with_executions(self):
        engine = WorkflowEngine()
        workflow = Workflow("test", "Test", "test", [WorkflowStep("log", "input", "result")])

        execution = engine.execute(workflow, {})

        status = engine.get_status()

        assert status["total_executions"] == 1
        assert len(status["recent_executions"]) == 1
        assert status["recent_executions"][0]["workflow"] == "test"
        assert status["recent_executions"][0]["success"] is True


class TestStepExecutionSerialization:
    """Test serialization of execution results."""

    def test_step_execution_to_dict(self):
        step = WorkflowStep("log", "input", "output")
        step_exec = StepExecution(
            step=step,
            start_time=datetime(2025, 1, 1, 12, 0, 0),
            end_time=datetime(2025, 1, 1, 12, 0, 1),
            duration_ms=1000,
            success=True,
            output="result",
        )

        data = step_exec.to_dict()

        assert data["action"] == "log"
        assert data["success"] is True
        assert data["duration_ms"] == 1000
        assert data["error"] is None

    def test_workflow_execution_to_dict(self):
        workflow = Workflow("test", "Test", "test", [WorkflowStep("log", "input", "result")])
        execution = WorkflowExecution(
            workflow=workflow,
            start_time=datetime(2025, 1, 1, 12, 0, 0),
            end_time=datetime(2025, 1, 1, 12, 0, 1),
            duration_ms=1000,
            success=True,
            steps=[],
        )

        data = execution.to_dict()

        assert data["workflow_name"] == "test"
        assert data["success"] is True
        assert data["duration_ms"] == 1000
