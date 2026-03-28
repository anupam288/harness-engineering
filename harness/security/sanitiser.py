"""
sanitiser.py — InputSanitiser

Detects prompt injection attempts and sanitises string fields before
they reach any LLM call. Runs as part of SchemaValidator — deterministic,
zero token cost, runs before the agent's LLM call.

What it catches:
  - Classic prompt injection patterns ("ignore previous instructions", "system:")
  - Role-switching attempts ("you are now", "act as", "jailbreak")
  - Context escape attempts (excessive newlines, null bytes, Unicode tricks)
  - Oversized fields that could blow out context windows
  - Control characters that could corrupt log entries

What it does NOT do:
  - Block legitimate inputs that happen to mention AI concepts
  - Parse or understand the semantics of the input
  - Replace the content — it flags and reports, the caller decides

Design: pattern-first (regex), then heuristics. No LLM involved.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Injection patterns
# ---------------------------------------------------------------------------

# Patterns that are almost never legitimate in structured agent inputs
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # Classic instruction override
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|context)",
     "instruction_override"),
    (r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|context)",
     "instruction_override"),
    (r"forget\s+(all\s+)?(previous|prior)\s+(instructions?|context)",
     "instruction_override"),

    # Role / identity manipulation
    (r"\byou\s+are\s+now\b",                      "role_switch"),
    (r"\bact\s+as\b.{0,30}\b(AI|assistant|bot)\b","role_switch"),
    (r"\bpretend\s+(you\s+are|to\s+be)\b",        "role_switch"),
    (r"\bjailbreak\b",                              "jailbreak"),
    (r"\bdan\s+mode\b",                            "jailbreak"),
    (r"\bgrandma\s+trick\b",                       "jailbreak"),

    # System / meta context injection
    (r"<\s*system\s*>",                            "system_tag"),
    (r"\[INST\]",                                  "instruction_tag"),
    (r"###\s*(Human|Assistant|System)\s*:",        "role_separator"),
    (r"<\|im_start\|>",                            "special_token"),
    (r"<\|endoftext\|>",                           "special_token"),

    # Tool / function call injection
    (r"```\s*(python|bash|javascript|sql)",        "code_block_injection"),
    (r"<tool_call>",                               "tool_call_injection"),
    (r"\bos\.system\s*\(",                         "code_execution"),
    (r"\bsubprocess\.",                            "code_execution"),
    (r"\beval\s*\(",                               "code_execution"),

    # Data exfiltration
    (r"https?://\S+\.(ngrok|requestbin|pipedream)", "exfiltration_url"),
    (r"send\s+(this|the)\s+(to|via)\s+http",       "exfiltration_instruction"),

    # Excessive repetition (often used to overflow context)
    (r"(.)\1{50,}",                                "repetition_attack"),
]

_COMPILED_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE | re.DOTALL), label)
    for pattern, label in _INJECTION_PATTERNS
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SanitisationIssue:
    field_name: str
    issue_type: str        # "injection" | "oversized" | "control_chars" | "unicode_trick"
    pattern_label: str
    excerpt: str           # the suspicious substring (truncated)
    severity: str          # "block" | "warn"


@dataclass
class SanitisationResult:
    passed: bool
    issues: list[SanitisationIssue] = field(default_factory=list)
    sanitised: dict = field(default_factory=dict)  # cleaned version of input

    def has_blocks(self) -> bool:
        return any(i.severity == "block" for i in self.issues)

    def report(self) -> str:
        if self.passed:
            return "✓ Input sanitisation passed"
        lines = ["✗ Input sanitisation FAILED"]
        for issue in self.issues:
            lines.append(
                f"  [{issue.severity.upper()}] field='{issue.field_name}' "
                f"type={issue.issue_type} pattern={issue.pattern_label} "
                f"excerpt={issue.excerpt!r}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# InputSanitiser
# ---------------------------------------------------------------------------

class InputSanitiser:
    """
    Scans string fields in agent input for prompt injection and other
    malicious content. Called by SchemaValidator before any LLM call.

    Configuration from security_config.yaml:
      max_field_length:  int   (default 10_000 chars per field)
      block_on_injection: bool (default True — treat injections as hard blocks)
      allow_patterns:    list  (regex patterns to whitelist — for advanced use)
    """

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.max_field_length = cfg.get("max_field_length", 10_000)
        self.block_on_injection = cfg.get("block_on_injection", True)
        self.allow_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in cfg.get("allow_patterns", [])
        ]

    def sanitise(self, input_data: dict) -> SanitisationResult:
        """
        Scan all string fields in input_data.
        Returns SanitisationResult with issues and a cleaned copy.
        """
        issues: list[SanitisationIssue] = []
        sanitised = {}

        for field_name, value in input_data.items():
            if isinstance(value, str):
                field_issues, clean_value = self._check_string(field_name, value)
                issues.extend(field_issues)
                sanitised[field_name] = clean_value
            elif isinstance(value, dict):
                sub_result = self.sanitise(value)
                issues.extend(sub_result.issues)
                sanitised[field_name] = sub_result.sanitised
            elif isinstance(value, list):
                clean_list = []
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        item_issues, clean_item = self._check_string(
                            f"{field_name}[{i}]", item
                        )
                        issues.extend(item_issues)
                        clean_list.append(clean_item)
                    else:
                        clean_list.append(item)
                sanitised[field_name] = clean_list
            else:
                sanitised[field_name] = value

        blocking_issues = [i for i in issues if i.severity == "block"]
        return SanitisationResult(
            passed=len(blocking_issues) == 0,
            issues=issues,
            sanitised=sanitised,
        )

    def _check_string(
        self, field_name: str, value: str
    ) -> tuple[list[SanitisationIssue], str]:
        issues = []
        clean = value

        # 1. Strip null bytes and non-printable control characters
        clean_chars = []
        for ch in clean:
            cat = unicodedata.category(ch)
            if ch == "\n" or ch == "\t":
                clean_chars.append(ch)   # keep newlines and tabs
            elif cat.startswith("C"):    # control / format / surrogate
                issues.append(SanitisationIssue(
                    field_name=field_name,
                    issue_type="control_chars",
                    pattern_label="control_character",
                    excerpt=repr(ch),
                    severity="warn",
                ))
                # Strip the character
            else:
                clean_chars.append(ch)
        clean = "".join(clean_chars)

        # 2. Unicode homograph / invisible character tricks
        invisible = [ch for ch in clean if unicodedata.category(ch) in ("Cf", "Mn")]
        if len(invisible) > 5:
            issues.append(SanitisationIssue(
                field_name=field_name,
                issue_type="unicode_trick",
                pattern_label="invisible_characters",
                excerpt=f"{len(invisible)} invisible chars",
                severity="warn",
            ))

        # 3. Oversized field
        if len(clean) > self.max_field_length:
            issues.append(SanitisationIssue(
                field_name=field_name,
                issue_type="oversized",
                pattern_label="field_too_long",
                excerpt=f"{len(clean)} chars (max {self.max_field_length})",
                severity="block",
            ))
            clean = clean[: self.max_field_length]

        # 4. Injection pattern matching
        for pattern, label in _COMPILED_PATTERNS:
            # Check against allow list first
            if any(allow.search(clean) for allow in self.allow_patterns):
                continue
            match = pattern.search(clean)
            if match:
                severity = "block" if self.block_on_injection else "warn"
                excerpt = match.group(0)[:80]
                issues.append(SanitisationIssue(
                    field_name=field_name,
                    issue_type="injection",
                    pattern_label=label,
                    excerpt=excerpt,
                    severity=severity,
                ))

        return issues, clean
