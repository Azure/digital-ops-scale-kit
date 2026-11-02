# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Publish the advisory warnings that a deployment step returns through `reports.warnings`.

A step binds one of its template's array outputs. After the operation succeeds,
the whole array is validated before anything is shown. Each valid record logs
`warning.step-reported`. A missing or malformed value logs one
`warning.step-report-invalid` for the operation and nothing else. Neither
changes the operation's status, the run's exit code or what runs next.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from siteops.diagnostics import STEP_REPORT_INVALID, STEP_REPORTED, warn
from siteops.sanitize import is_redaction_enabled

MAX_STEP_WARNINGS = 16
MAX_WARNING_MESSAGE = 500

logger = logging.getLogger(__name__)


def _message(record: Any) -> str | None:
    if not isinstance(record, dict) or set(record) != {"message"}:
        return None
    message = record["message"]
    if (
        not isinstance(message, str) or not 1 <= len(message) <= MAX_WARNING_MESSAGE
        or not message.strip()
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in message
        )
    ):
        return None
    return message


def step_warnings(outputs: Any, output_name: str) -> tuple[str, ...] | None:
    """Return the messages in a successful operation's bound output, or None when the report is invalid.

    ARM returns each output as `{"type": ..., "value": ...}`. The value must be
    an array of at most 16 records that each hold exactly one `message`.
    """
    wrapper = outputs.get(output_name) if isinstance(outputs, dict) else None
    if not isinstance(wrapper, dict) or "value" not in wrapper:
        return None
    records = wrapper["value"]
    if not isinstance(records, list) or len(records) > MAX_STEP_WARNINGS:
        return None
    messages = []
    for record in records:
        message = _message(record)
        if message is None:
            return None
        messages.append(message)
    return tuple(messages)


def publish_step_warnings(site: str, step: str, outputs: Any, output_name: str) -> None:
    """Log the bound output's warnings for one succeeded operation."""
    try:
        messages = step_warnings(outputs, output_name)
    except Exception:  # noqa: BLE001 - a report never changes an observed success
        messages = None
    redacted = is_redaction_enabled()
    if messages is None:
        if redacted:
            warn(logger, STEP_REPORT_INVALID, "A deployment step returned an invalid warning report.")
        else:
            warn(
                logger, STEP_REPORT_INVALID,
                "Site '%s' step '%s' returned an invalid warning report in output '%s'.",
                site, step, output_name,
            )
        return
    for message in messages:
        if redacted:
            warn(logger, STEP_REPORTED, "A deployment step reported a warning.")
        else:
            warn(logger, STEP_REPORTED, "Site '%s' step '%s' reported: %s", site, step, message)
