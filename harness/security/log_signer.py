"""
log_signer.py — LogSigner and LogVerifier

HMAC-SHA256 signs each decision log entry so tampering is detectable.
The signing key is loaded from the environment (never from a file in the repo).

Signing does not encrypt. It proves that a log entry was written by a process
that held the signing key, and that it has not been modified since.

Usage:
    # Signing (in BaseAgent.execute())
    signer = LogSigner.from_env()
    signed_entry = signer.sign(result.to_dict())
    # signed_entry now has an extra "_sig" field

    # Verification (in LogVerifier)
    verifier = LogVerifier.from_env()
    ok, reason = verifier.verify(signed_entry)

Key management:
    Set HARNESS_LOG_SIGNING_KEY to any high-entropy string (32+ chars).
    If the env var is not set, signing is skipped with a warning (not an error).
    This allows the harness to run without signing in dev/test environments.

    Generate a key:
        python -c "import secrets; print(secrets.token_hex(32))"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENV_KEY_NAME = "HARNESS_LOG_SIGNING_KEY"
SIGNATURE_FIELD = "_sig"
SIGNED_FIELDS = [
    "agent_name", "phase", "status", "confidence",
    "timestamp", "flags",
]


@dataclass
class VerificationResult:
    valid: bool
    reason: str
    entry_index: int = -1

    def __str__(self) -> str:
        icon = "✓" if self.valid else "✗"
        return f"{icon} {self.reason}"


class LogSigner:
    """
    Signs a log entry dict with HMAC-SHA256.
    Adds a `_sig` field containing the hex digest.
    The signature covers a canonical JSON serialisation of SIGNED_FIELDS.
    """

    def __init__(self, key: bytes):
        self._key = key

    @classmethod
    def from_env(cls) -> "LogSigner | None":
        """
        Load signing key from environment.
        Returns None if HARNESS_LOG_SIGNING_KEY is not set (signing disabled).
        """
        raw_key = os.environ.get(ENV_KEY_NAME, "")
        if not raw_key:
            return None
        if len(raw_key) < 16:
            print(f"  ⚠ {ENV_KEY_NAME} is too short (< 16 chars) — signing disabled")
            return None
        return cls(raw_key.encode())

    def sign(self, entry: dict) -> dict:
        """
        Add a _sig field to the entry dict.
        Returns a new dict (does not modify in place).
        """
        canonical = self._canonical(entry)
        sig = hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()
        return {**entry, SIGNATURE_FIELD: sig}

    def _canonical(self, entry: dict) -> str:
        """
        Build a stable canonical string from the fields that must be signed.
        Uses sorted keys and compact JSON to ensure determinism.
        """
        subset = {k: entry.get(k) for k in SIGNED_FIELDS if k in entry}
        return json.dumps(subset, sort_keys=True, separators=(",", ":"), default=str)


class LogVerifier:
    """
    Verifies HMAC-SHA256 signatures on decision log entries.
    Used by `python cli.py security verify-logs` and the GC agent.
    """

    def __init__(self, key: bytes):
        self._key = key
        self._signer = LogSigner(key)

    @classmethod
    def from_env(cls) -> "LogVerifier | None":
        raw_key = os.environ.get(ENV_KEY_NAME, "")
        if not raw_key:
            return None
        return cls(raw_key.encode())

    def verify(self, entry: dict, index: int = -1) -> VerificationResult:
        """Verify one log entry. Returns VerificationResult."""
        stored_sig = entry.get(SIGNATURE_FIELD)
        if stored_sig is None:
            return VerificationResult(
                valid=False,
                reason=f"Entry has no {SIGNATURE_FIELD!r} field — was it written without signing?",
                entry_index=index,
            )

        expected_sig = hmac.new(
            self._key,
            self._signer._canonical(entry).encode(),
            hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(stored_sig, expected_sig):
            return VerificationResult(
                valid=True,
                reason=f"Signature valid (agent={entry.get('agent_name')}, "
                       f"ts={entry.get('timestamp', '')[:19]})",
                entry_index=index,
            )
        return VerificationResult(
            valid=False,
            reason=f"Signature MISMATCH — entry may have been tampered with "
                   f"(agent={entry.get('agent_name')}, ts={entry.get('timestamp', '')[:19]})",
            entry_index=index,
        )

    def verify_log_file(self, log_path: Path) -> list[VerificationResult]:
        """
        Verify every entry in a JSONL decision log file.
        Returns one VerificationResult per line.
        """
        if not log_path.exists():
            return [VerificationResult(False, f"Log file not found: {log_path}")]

        results = []
        for i, line in enumerate(log_path.read_text().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                results.append(self.verify(entry, index=i))
            except json.JSONDecodeError as exc:
                results.append(VerificationResult(
                    valid=False,
                    reason=f"Line {i}: invalid JSON — {exc}",
                    entry_index=i,
                ))
        return results

    def summary(self, results: list[VerificationResult]) -> str:
        total = len(results)
        valid = sum(1 for r in results if r.valid)
        invalid = total - valid
        lines = [
            f"Log integrity: {valid}/{total} entries valid",
        ]
        if invalid:
            lines.append(f"  ✗ {invalid} TAMPERED or UNSIGNED entries:")
            for r in results:
                if not r.valid:
                    lines.append(f"    [{r.entry_index}] {r.reason}")
        else:
            lines.append("  ✓ All entries intact")
        return "\n".join(lines)
