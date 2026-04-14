# vulture whitelist — framework false-positive suppression
# Vulture scans this file and treats any name defined here as "used".
# Format: assignment so vulture picks up the name as referenced.

# ---------------------------------------------------------------------------
# pytest fixtures (conftest.py + per-module fixtures)
# ---------------------------------------------------------------------------
_isolate_hermes_home = None  # unused variable
tmp_dir = None  # unused variable
mock_config = None  # unused variable

# ---------------------------------------------------------------------------
# Tool registry: exported constants from tools/terminal_tool.py, skills_tool.py
# ---------------------------------------------------------------------------
TERMINAL_TOOL_DESCRIPTION = None  # unused variable
SKILLS_TOOL_DESCRIPTION = None  # unused variable

# ---------------------------------------------------------------------------
# __all__ exports (tools/__init__.py, tools/summit_trace_flags.py)
# ---------------------------------------------------------------------------
__all__ = None  # unused variable

# ---------------------------------------------------------------------------
# Abstract method implementations (tools/file_operations.py, tools/skills_hub.py)
# These are declared @abstractmethod in ABC base classes — concrete impls
# look "unused" to vulture because calls go through the abstract interface.
# ---------------------------------------------------------------------------
read_file = None  # unused variable
write_file = None  # unused variable
patch_replace = None  # unused variable
patch_v4a = None  # unused variable
source_id = None  # unused variable

# ---------------------------------------------------------------------------
# Telegram SDA callback handlers (tools/telegram_sda_flows.py)
# Registered at runtime via python-telegram-bot; never called directly.
# ---------------------------------------------------------------------------
handle_noprice_manual_reply = None  # unused variable
handle_solicit_callback = None  # unused variable
handle_append_callback = None  # unused variable
handle_idg_warning_dismissed = None  # unused variable
dispatch_sda_callback = None  # unused variable

# ---------------------------------------------------------------------------
# Gateway / platform handler functions
# (gateway/platforms/telegram.py, discord.py, slack.py)
# Registered as callbacks; never called by name in Python.
# ---------------------------------------------------------------------------
check_telegram_requirements = None  # unused variable
on_ready = None  # unused variable
on_message = None  # unused variable
on_timeout = None  # unused variable
handle_message = None  # unused variable
handle_message_event = None  # unused variable
handle_hermes_command = None  # unused variable

# ---------------------------------------------------------------------------
# cc_remote async handlers
# ---------------------------------------------------------------------------
handle_cc_command = None  # unused variable
handle_approval = None  # unused variable

# ---------------------------------------------------------------------------
# gateway/run.py module-level functions called indirectly / via threading
# ---------------------------------------------------------------------------
start_gateway = None  # unused variable

# ---------------------------------------------------------------------------
# Pydantic v2 model config attributes
# model_config is read by Pydantic machinery, not called explicitly.
# ---------------------------------------------------------------------------
model_config = None  # unused variable
