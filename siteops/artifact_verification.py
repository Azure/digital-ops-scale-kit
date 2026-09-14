# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Immutable artifact-verification receipts, separate from package descriptions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Verification timestamps must include a timezone.")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ArtifactVerification:
    """Evidence from a successful verifier, not an execution authorization token."""

    sha256: str
    size: int
    policy_id: str
    policy_version: int
    policy_sha256: str
    root_sha256: str
    proof_sha256: str
    evaluated_at: datetime
    valid_until: datetime
    verifier: str
    verifier_version: str
    evidence_json: bytes = field(repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "apiVersion": "siteops/v1alpha1",
            "kind": "ArtifactVerification",
            "subject": {"algorithm": "sha256", "digest": self.sha256, "size": self.size},
            "policy": {
                "id": self.policy_id, "version": self.policy_version,
                "sha256": self.policy_sha256, "validUntil": utc_text(self.valid_until),
            },
            "evaluatedAt": utc_text(self.evaluated_at),
            "verifier": {"name": self.verifier, "version": self.verifier_version},
            "trustedRootSha256": self.root_sha256,
            "proofSha256": self.proof_sha256,
            "evidence": json.loads(self.evidence_json),
            "revocation": "not-checked",
        }

    def serialized(self) -> bytes:
        return (
            json.dumps(self.document(), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.serialized()).hexdigest()
