"""
Model routing system for Hermes Agent.

Implements tiered model selection based on task complexity:
- Haiku: Fast, cheap tasks (classification, extraction, summarization)
- Sonnet: Balanced tasks (reasoning, code generation, chat)
- Opus: Complex tasks (planning, debugging, multi-step reasoning)

Routes based on task classification from conversation context.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
import yaml

logger = logging.getLogger(__name__)

# Default task-to-model mapping
DEFAULT_TASK_MODEL_MAP = {
    'classify': 'anthropic/claude-haiku-4-5-20251001',      # Fast classification
    'extract': 'anthropic/claude-haiku-4-5-20251001',        # Data extraction
    'summarize': 'anthropic/claude-haiku-4-5-20251001',      # Summarization
    'reason': 'anthropic/claude-sonnet-4-6',                 # Complex reasoning
    'code': 'anthropic/claude-sonnet-4-6',                   # Code generation/analysis
    'chat': 'anthropic/claude-sonnet-4-6',                   # General conversation
    'plan': 'anthropic/claude-opus-4-6',                     # Strategic planning
    'complex_debug': 'anthropic/claude-opus-4-6',            # Deep debugging
}

# Task classification keywords - map common patterns to task types
TASK_CLASSIFICATION_KEYWORDS = {
    'classify': [
        'classify', 'categorize', 'which', 'what type', 'detect', 'identify type',
        'sentiment', 'classify as', 'is this a'
    ],
    'extract': [
        'extract', 'pull out', 'get the', 'find all', 'list all', 'parse',
        'gather', 'collect', 'scrape'
    ],
    'summarize': [
        'summarize', 'summary', 'tl;dr', 'brief', 'condense', 'recap',
        'overview', 'digest'
    ],
    'plan': [
        'plan', 'strategy', 'roadmap', 'architecture', 'design', 'approach',
        'build', 'implement from scratch', 'how should i', 'what\'s the best way'
    ],
    'complex_debug': [
        'debug', 'why is', 'broken', 'error', 'not working', 'investigate',
        'root cause', 'what\'s wrong', 'fix this', 'problem'
    ],
    'code': [
        'write', 'code', 'function', 'script', 'implement', 'create',
        'generate', 'refactor', 'optimize', 'convert', 'translate'
    ],
    'reason': [
        'why', 'how does', 'explain', 'understand', 'analyze', 'interpret',
        'evaluate', 'compare', 'pros and cons'
    ],
}


class ModelRouter:
    """Routes tasks to appropriate models based on complexity classification."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the router with configuration.

        Args:
            config_path: Path to config.yaml. If None, uses ~/.hermes/config.yaml
        """
        self.config_path = config_path
        self.task_model_map = DEFAULT_TASK_MODEL_MAP.copy()
        self._load_config()

    def _load_config(self) -> None:
        """Load model routing config from config.yaml if present."""
        if self.config_path is None:
            hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
            self.config_path = hermes_home / "config.yaml"

        if not Path(self.config_path).exists():
            logger.debug("Config file not found at %s, using defaults", self.config_path)
            return

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            # Load model_routing section if present
            if 'model_routing' in config:
                model_routing = config['model_routing']
                if isinstance(model_routing, dict) and 'task_model_map' in model_routing:
                    custom_map = model_routing['task_model_map']
                    if isinstance(custom_map, dict):
                        self.task_model_map.update(custom_map)
                        logger.debug("Loaded custom model routing from config")

        except Exception as e:
            logger.warning("Failed to load model routing config: %s", e)

    def classify_task(self, messages: List[Dict[str, Any]]) -> str:
        """
        Classify task type from conversation messages.

        Analyzes the latest user message to determine task complexity.
        Falls back to 'chat' if classification is ambiguous.

        Args:
            messages: List of message dicts with 'role' and 'content'

        Returns:
            Task type string (e.g., 'classify', 'code', 'plan')
        """
        if not messages:
            return 'chat'

        # Find the last user message
        user_message = None
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                content = msg.get('content')
                if content is not None:
                    user_message = str(content).lower()
                break

        if not user_message:
            return 'chat'

        # Score each task type based on keyword matches
        scores = {}
        for task_type, keywords in TASK_CLASSIFICATION_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in user_message)
            if score > 0:
                scores[task_type] = score

        # Return highest-scoring task, or default to 'chat'
        if scores:
            best_task = max(scores, key=scores.get)
            logger.debug("Classified task as '%s' (score: %d)", best_task, scores[best_task])
            return best_task

        return 'chat'

    def route_model(self, task_type: str) -> str:
        """
        Route task to appropriate model.

        Args:
            task_type: Task type string (e.g., 'classify', 'code', 'plan')

        Returns:
            Model name string (e.g., 'anthropic/claude-haiku-4-5-20251001')
        """
        # Default to sonnet if task type is unknown
        model = self.task_model_map.get(task_type, self.task_model_map.get('chat'))
        logger.debug("Routed task type '%s' to model '%s'", task_type, model)
        return model

    def route_messages(self, messages: List[Dict[str, Any]]) -> str:
        """
        Convenience method: classify then route in one call.

        Args:
            messages: List of message dicts

        Returns:
            Selected model name
        """
        task_type = self.classify_task(messages)
        return self.route_model(task_type)
