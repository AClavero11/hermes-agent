"""
Batch Queue for Anthropic Message Batches API.

Manages submission, polling, and result delivery for batch processing jobs.
Batches provide 50% cost reduction with 24-hour turnaround, ideal for
non-interactive workflows like nightly reports, bulk analysis, market scans.

API: https://api.anthropic.com/v1/messages/batches
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional
import uuid

import httpx

from hermes_state import COST_PER_MODEL

logger = logging.getLogger(__name__)


class BatchQueue:
    """
    Queue for Anthropic Message Batches API (50% cost reduction).

    Manages batch request lifecycle:
    1. submit() — queue messages for batch processing
    2. poll() — check batch status, collect results
    3. deliver() — send completed results via Telegram

    Cost tracking: Batch API requests are automatically charged at 50% of standard rates.
    Costs are logged to hermes_state.cost_log with workflow="batch".
    """

    def __init__(self, db_path: Optional[Path] = None, config: Optional[Dict] = None):
        """
        Initialize BatchQueue.

        Args:
            db_path: Path to state.db file. Defaults to ~/.hermes/state.db
            config: Configuration dict with batch settings
        """
        if db_path is None:
            hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
            db_path = hermes_home / "state.db"

        self.db_path = db_path
        self.config = config or {}

        # API settings
        self.api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        self.api_base = "https://api.anthropic.com/v1"
        self.default_model = self.config.get("default_model", "claude-sonnet-4-6-20250514")

        # Polling settings
        self.poll_interval_ticks = self.config.get("poll_interval_ticks", 15)
        self.max_pending = self.config.get("max_pending", 10)

        # Telegram settings for delivery
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN_CZAR")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID_AC")

        if not self.api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set. Batch API will fail. "
                "Set via ANTHROPIC_API_KEY env var."
            )

    def submit(
        self,
        messages: List[Dict],
        workflow_name: str,
        model: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> str:
        """
        Submit a batch request to Anthropic Batches API.

        Args:
            messages: List of message dicts with role, content, etc.
            workflow_name: Name of the workflow for tracking
            model: Model to use. Defaults to config default_model
            metadata: Optional metadata dict to store with batch

        Returns:
            batch_id from Anthropic API

        Raises:
            RuntimeError: If API key not configured
            httpx.HTTPError: If API call fails
        """
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        model = model or self.default_model

        # Build batch request format
        custom_id = f"{workflow_name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

        batch_request = {
            "requests": [
                {
                    "custom_id": custom_id,
                    "params": {
                        "model": model,
                        "max_tokens": 4096,
                        "messages": messages,
                    }
                }
            ]
        }

        # Submit to Anthropic API
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        url = f"{self.api_base}/messages/batches"

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=batch_request, headers=headers)
            response.raise_for_status()
            batch_data = response.json()

        batch_id = batch_data.get("id")
        if not batch_id:
            raise RuntimeError(f"No batch_id in response: {batch_data}")

        logger.info(f"Submitted batch {batch_id} for workflow {workflow_name}")

        # Store in database
        self._store_batch(
            batch_id=batch_id,
            workflow_name=workflow_name,
            model=model,
            status="pending",
            metadata=metadata,
        )

        return batch_id

    def poll(self) -> List[Dict]:
        """
        Poll all pending batches for completion.

        Returns:
            List of completed batch results (empty if none complete)
        """
        pending_batches = self._get_pending_batches()

        if not pending_batches:
            logger.debug("No pending batches to poll")
            return []

        logger.info(f"Polling {len(pending_batches)} pending batches")

        completed_results = []

        for batch in pending_batches:
            batch_id = batch["batch_id"]

            try:
                status, result_summary = self._check_batch_status(batch_id)

                if status == "ended":
                    # Fetch results
                    results = self._fetch_batch_results(batch_id)

                    # Mark as completed
                    self._update_batch_status(
                        batch_id=batch_id,
                        status="completed",
                        result_summary=result_summary,
                    )

                    completed_results.append({
                        "batch_id": batch_id,
                        "workflow_name": batch["workflow_name"],
                        "model": batch["model"],
                        "results": results,
                        "completed_at": time.time(),
                    })

                    logger.info(f"Batch {batch_id} completed: {result_summary}")

                elif status == "failed":
                    # Mark as failed
                    self._update_batch_status(
                        batch_id=batch_id,
                        status="failed",
                        result_summary=result_summary,
                    )
                    logger.error(f"Batch {batch_id} failed: {result_summary}")

            except Exception as e:
                logger.error(f"Error polling batch {batch_id}: {e}", exc_info=True)

        return completed_results

    def deliver_results(self, results: List[Dict], **_context) -> None:
        """
        Deliver completed batch results via Telegram.

        Args:
            results: List of completed batch result dicts
            **context: Additional context (for logging)
        """
        if not self.telegram_token or not self.telegram_chat_id:
            logger.warning(
                "Telegram credentials not configured. "
                "Skipping batch result delivery."
            )
            return

        for batch_result in results:
            batch_id = batch_result["batch_id"]
            workflow = batch_result["workflow_name"]
            completed_at = batch_result["completed_at"]

            message = (
                f"Batch completed: {workflow}\n"
                f"Batch ID: {batch_id}\n"
                f"Completed at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(completed_at))}\n"
                f"Results count: {len(batch_result.get('results', []))}"
            )

            self._send_telegram_message(message)
            logger.info(f"Delivered batch {batch_id} results via Telegram")

    def get_pending(self) -> List[Dict]:
        """
        Get list of all pending batches with current status.

        Returns:
            List of pending batch dicts with batch_id, workflow_name, status, submitted_at
        """
        return self._get_pending_batches()

    def cancel(self, batch_id: str) -> bool:
        """
        Cancel a pending batch.

        Args:
            batch_id: Batch ID to cancel

        Returns:
            True if cancellation succeeded, False otherwise
        """
        if not self.api_key:
            logger.error("ANTHROPIC_API_KEY not configured")
            return False

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        url = f"{self.api_base}/messages/batches/{batch_id}/cancel"

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, headers=headers)
                response.raise_for_status()

            # Update database
            self._update_batch_status(batch_id, status="cancelled")
            logger.info(f"Cancelled batch {batch_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to cancel batch {batch_id}: {e}")
            return False

    def estimate_batch_cost(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """
        Estimate cost of a batch request at 50% discount.

        Anthropic Batches API charges 50% of standard rates.

        Args:
            model: Model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Cost in USD (at 50% of standard rates)
        """
        # Find matching model in COST_PER_MODEL
        model_rates = None
        for key, rates in COST_PER_MODEL.items():
            if key in model or model in key:
                model_rates = rates
                break

        if model_rates is None:
            # Default to claude-haiku rates if model not found
            model_rates = COST_PER_MODEL["claude-haiku-4-5"]

        input_rate = model_rates.get("input", 0.80) / 1_000_000
        output_rate = model_rates.get("output", 4.00) / 1_000_000

        # Regular token cost
        input_cost = input_tokens * input_rate
        output_cost = output_tokens * output_rate

        # Apply 50% batch discount
        total_cost = (input_cost + output_cost) * 0.5
        return round(total_cost, 6)

    # =========================================================================
    # Private methods (database + API)
    # =========================================================================

    def _store_batch(
        self,
        batch_id: str,
        workflow_name: str,
        model: str,
        status: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Store batch in database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            metadata_json = json.dumps(metadata) if metadata else None

            cursor.execute(
                """INSERT INTO batch_queue
                   (batch_id, workflow_name, model, status, submitted_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (batch_id, workflow_name, model, status, time.time(), metadata_json),
            )

            conn.commit()
            conn.close()

        except sqlite3.IntegrityError:
            logger.warning(f"Batch {batch_id} already exists in database")
        except Exception as e:
            logger.error(f"Failed to store batch in database: {e}")

    def _get_pending_batches(self) -> List[Dict]:
        """Fetch all pending batches from database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM batch_queue WHERE status IN ('pending', 'processing') "
                "ORDER BY submitted_at DESC"
            )

            batches = [dict(row) for row in cursor.fetchall()]
            conn.close()

            return batches

        except sqlite3.OperationalError:
            logger.error("batch_queue table does not exist. Run migration first.")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch pending batches: {e}")
            return []

    def _update_batch_status(
        self,
        batch_id: str,
        status: str,
        result_summary: Optional[str] = None,
    ) -> None:
        """Update batch status in database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            if result_summary is not None:
                cursor.execute(
                    "UPDATE batch_queue SET status = ?, result_summary = ?, completed_at = ? "
                    "WHERE batch_id = ?",
                    (status, result_summary, time.time(), batch_id),
                )
            else:
                cursor.execute(
                    "UPDATE batch_queue SET status = ? WHERE batch_id = ?",
                    (status, batch_id),
                )

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to update batch status: {e}")

    def _check_batch_status(self, batch_id: str) -> tuple:
        """
        Check batch status from Anthropic API.

        Returns:
            Tuple of (status, result_summary_str)
            Status is one of: pending, processing, ended, failed
        """
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        url = f"{self.api_base}/messages/batches/{batch_id}"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            batch_data = response.json()

        status = batch_data.get("processing_status", "unknown")

        # Build summary string
        summary_parts = [f"status={status}"]
        if "request_counts" in batch_data:
            counts = batch_data["request_counts"]
            summary_parts.append(
                f"processed={counts.get('processed', 0)}, "
                f"succeeded={counts.get('succeeded', 0)}, "
                f"errored={counts.get('errored', 0)}"
            )

        result_summary = " | ".join(summary_parts)

        return status, result_summary

    def _fetch_batch_results(self, batch_id: str) -> List[Dict]:
        """
        Fetch results from a completed batch.

        Returns:
            List of result dicts from the batch API
        """
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        url = f"{self.api_base}/messages/batches/{batch_id}/results"

        all_results = []

        with httpx.Client(timeout=60.0) as client:
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()

                for line in response.iter_lines():
                    if line.strip():
                        try:
                            result = json.loads(line)
                            all_results.append(result)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse result line: {line}")

        return all_results

    def _send_telegram_message(self, message: str) -> bool:
        """
        Send a message via Telegram.

        Args:
            message: Message text

        Returns:
            True if successful, False otherwise
        """
        if not self.telegram_token or not self.telegram_chat_id:
            logger.warning("Telegram credentials not configured")
            return False

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"

        payload = {
            "chat_id": self.telegram_chat_id,
            "text": message,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False


def poll_batch_handler(**_context) -> None:
    """
    Heartbeat handler that polls batch results.

    Intended to be called by the agent's heartbeat system periodically.
    """
    logger.info("Running batch queue poll handler")

    queue = BatchQueue()

    # Poll for completed batches
    results = queue.poll()

    if results:
        logger.info(f"Found {len(results)} completed batches")
        queue.deliver_results(results)
    else:
        logger.debug("No batches completed in this poll cycle")
