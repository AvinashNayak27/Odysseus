"""Hash-bound human approval plans."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict

from odysseus.models import ApprovalPlan


def approval_hash(plan: ApprovalPlan) -> str:
    """Hash every action-bearing field using deterministic JSON serialization."""
    payload = json.dumps(asdict(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_approval(plan: ApprovalPlan, supplied_hash: str) -> None:
    expected = approval_hash(plan)
    if not hmac.compare_digest(expected, supplied_hash):
        raise PermissionError("approval hash does not match the exact action plan")
