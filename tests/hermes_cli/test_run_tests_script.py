from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TESTS_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"


def test_run_tests_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(RUN_TESTS_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_run_tests_script_skips_unusable_venv_and_uses_uv_fallback():
    content = RUN_TESTS_SCRIPT.read_text(encoding="utf-8")

    assert "Skipping unusable virtualenv" in content
    assert "UV_PROJECT_ENVIRONMENT" in content
    assert "uv run" in content
    assert "--extra dev" in content
    assert "pytest-split" in content
    assert "HERMES_TEST_PYTHON" in content
