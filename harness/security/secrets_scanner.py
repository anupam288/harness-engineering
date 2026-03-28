"""
secrets_scanner.py — SecretsScanner

Scans Python source files in harness/agents/ (and optionally the full repo)
for hardcoded API keys, tokens, and passwords.

Integrated into StructuralLinter.lint() so it runs as part of CI and pre-commit.
Also available standalone via `python cli.py security scan-secrets`.

What it looks for:
  - Anthropic / OpenAI / AWS / GCP / Azure key patterns
  - Generic "secret", "password", "token", "api_key" assignments
  - High-entropy strings (base64-like, hex) assigned to suspicious variable names
  - Private key PEM headers

What it ignores:
  - Strings that look like placeholders (${...}, <...>, "your_key_here")
  - Test files (configurable)
  - Strings shorter than MIN_SECRET_LENGTH
  - Comment lines
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path


MIN_SECRET_LENGTH = 20
HIGH_ENTROPY_THRESHOLD = 4.0   # Shannon entropy bits per char


# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, str]] = [
    # Anthropic
    (r"sk-ant-[a-zA-Z0-9\-_]{20,}", "anthropic_api_key"),
    # OpenAI
    (r"sk-[a-zA-Z0-9]{20,}", "openai_api_key"),
    # AWS
    (r"AKIA[0-9A-Z]{16}", "aws_access_key_id"),
    (r"(?i)(aws_secret_access_key|aws_secret)\s*=\s*['\"][a-zA-Z0-9/+]{40}['\"]", "aws_secret"),
    # GCP
    (r'"type":\s*"service_account"', "gcp_service_account"),
    # Azure
    (r"DefaultEndpointsProtocol=https;AccountName=", "azure_storage_connection"),
    # GitHub
    (r"ghp_[a-zA-Z0-9]{36}", "github_personal_token"),
    (r"ghs_[a-zA-Z0-9]{36}", "github_actions_token"),
    # Generic patterns
    (r"(?i)(api_key|apikey|api-key)\s*=\s*['\"][a-zA-Z0-9\-_]{20,}['\"]", "generic_api_key"),
    (r"(?i)(secret|password|passwd|pwd)\s*=\s*['\"][^\s'\"]{8,}['\"]", "generic_secret"),
    (r"(?i)(token|access_token|auth_token)\s*=\s*['\"][a-zA-Z0-9\-_\.]{20,}['\"]", "generic_token"),
    # PEM private keys
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private_key_pem"),
    # Bearer tokens in hardcoded headers
    (r"[Bb]earer\s+[a-zA-Z0-9\-_\.]{30,}", "bearer_token"),
    # Database connection strings with passwords
    (r"(postgres|mysql|mongodb)://[^:]+:[^@]{8,}@", "db_connection_string"),
]

_PLACEHOLDER_PATTERNS = [
    re.compile(r"\$\{[A-Z_]+\}"),          # ${ENV_VAR}
    re.compile(r"<[a-z_]+>"),               # <placeholder>
    re.compile(r"your[_-]?(api[_-]?)?key", re.IGNORECASE),
    re.compile(r"test[_-]?key", re.IGNORECASE),
    re.compile(r"xxx+"),
    re.compile(r"\.\.\."),
    re.compile(r"example"),
]

_COMPILED_SECRET_PATTERNS = [
    (re.compile(pattern), label)
    for pattern, label in _SECRET_PATTERNS
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SecretFinding:
    file_path: str
    line_number: int
    pattern_label: str
    excerpt: str     # the matched text, partially redacted
    severity: str    # "critical" | "warn"


@dataclass
class ScanResult:
    scanned_files: int = 0
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.critical_findings) == 0

    @property
    def critical_findings(self) -> list[SecretFinding]:
        return [f for f in self.findings if f.severity == "critical"]

    def report(self) -> str:
        lines = [
            f"Secrets scan: {self.scanned_files} files scanned, "
            f"{len(self.findings)} finding(s)"
        ]
        if not self.findings:
            lines.append("  ✓ No secrets found")
        for finding in self.findings:
            icon = "✗" if finding.severity == "critical" else "⚠"
            lines.append(
                f"  {icon} [{finding.severity.upper()}] "
                f"{finding.file_path}:{finding.line_number} "
                f"({finding.pattern_label}): {finding.excerpt}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SecretsScanner
# ---------------------------------------------------------------------------

class SecretsScanner:
    """
    Scans Python source files for hardcoded secrets.

    Used by StructuralLinter (auto) and `cli.py security scan-secrets` (manual).
    """

    def __init__(
        self,
        skip_test_files: bool = True,
        skip_patterns: list[str] = None,
        entropy_scan: bool = True,
    ):
        self.skip_test_files = skip_test_files
        self.skip_patterns = [
            re.compile(p) for p in (skip_patterns or [])
        ]
        self.entropy_scan = entropy_scan

    def scan_directory(self, directory: Path) -> ScanResult:
        """Scan all Python files in a directory recursively."""
        result = ScanResult()
        for py_file in sorted(directory.rglob("*.py")):
            if self._should_skip(py_file):
                continue
            file_result = self.scan_file(py_file)
            result.scanned_files += 1
            result.findings.extend(file_result.findings)
        return result

    def scan_file(self, file_path: Path) -> ScanResult:
        """Scan a single Python file."""
        result = ScanResult(scanned_files=1)
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return result

        for line_num, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            # Skip blank lines, comments, imports
            if not stripped or stripped.startswith("#") or stripped.startswith("import"):
                continue

            findings = self._check_line(str(file_path), line_num, line)
            result.findings.extend(findings)

        return result

    def _check_line(self, file_path: str, line_num: int, line: str) -> list[SecretFinding]:
        findings = []

        # Skip lines that look like placeholders or env var references
        if any(p.search(line) for p in _PLACEHOLDER_PATTERNS):
            return []

        # Pattern matching
        for pattern, label in _COMPILED_SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                matched_text = match.group(0)
                # Double-check it's not a placeholder
                if any(p.search(matched_text) for p in _PLACEHOLDER_PATTERNS):
                    continue
                findings.append(SecretFinding(
                    file_path=file_path,
                    line_number=line_num,
                    pattern_label=label,
                    excerpt=self._redact(matched_text),
                    severity="critical",
                ))

        # High-entropy string scan
        if self.entropy_scan:
            entropy_findings = self._check_entropy(file_path, line_num, line)
            findings.extend(entropy_findings)

        return findings

    def _check_entropy(
        self, file_path: str, line_num: int, line: str
    ) -> list[SecretFinding]:
        """Find high-entropy string literals assigned to suspicious variable names."""
        findings = []
        # Look for: suspicious_var = "high_entropy_string"
        assign_pattern = re.compile(
            r'(?i)(key|secret|token|password|passwd|credential|auth|api)\w*\s*=\s*'
            r'["\']([a-zA-Z0-9+/=\-_\.]{' + str(MIN_SECRET_LENGTH) + r',})["\']'
        )
        for match in assign_pattern.finditer(line):
            candidate = match.group(2)
            if _shannon_entropy(candidate) >= HIGH_ENTROPY_THRESHOLD:
                if not any(p.search(candidate) for p in _PLACEHOLDER_PATTERNS):
                    findings.append(SecretFinding(
                        file_path=file_path,
                        line_number=line_num,
                        pattern_label="high_entropy_assignment",
                        excerpt=self._redact(candidate),
                        severity="critical",
                    ))
        return findings

    def _should_skip(self, path: Path) -> bool:
        name = path.name
        if self.skip_test_files and (name.startswith("test_") or "test" in path.parts):
            return True
        if name.startswith("."):
            return True
        for pattern in self.skip_patterns:
            if pattern.search(str(path)):
                return True
        return False

    @staticmethod
    def _redact(text: str, keep: int = 6) -> str:
        """Partially redact a secret for safe display in reports."""
        if len(text) <= keep * 2:
            return "***"
        return text[:keep] + "..." + text[-keep:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy in bits per character."""
    if not s:
        return 0.0
    from collections import Counter
    counts = Counter(s)
    length = len(s)
    entropy = 0.0
    for count in counts.values():
        prob = count / length
        if prob > 0:
            entropy -= prob * math.log2(prob)
    return entropy
