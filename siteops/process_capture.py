# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded pipe capture for supervised native processes."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import BinaryIO


@dataclass
class BoundedCapture:
    limit: int
    content: bytearray
    exceeded: threading.Event
    failed: threading.Event

    @classmethod
    def create(cls, limit: int) -> BoundedCapture:
        return cls(limit, bytearray(), threading.Event(), threading.Event())

    def read(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    self.failed.set()
                    return
                remaining = self.limit - len(self.content)
                if len(chunk) > remaining:
                    if remaining > 0:
                        self.content.extend(chunk[:remaining])
                    self.exceeded.set()
                else:
                    self.content.extend(chunk)
        except (OSError, ValueError):
            self.failed.set()
