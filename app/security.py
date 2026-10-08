import re
from pathlib import PurePosixPath

from app.config import settings

# A credential value is a literal. A value that starts a function call, a subscript or
# brace template, or an attribute chain — os.environ.get("X"), config["X"], f"{VAR}",
# settings.API_TOKEN — is code that reads a secret from configuration and must be
# neither flagged nor mangled in reports. The lookaheads reject those value shapes.
_NOT_CODE_VALUE = (
    r"(?![\w.]+\()"
    r"(?![\w.]*(?:\[|\{))"
    r"(?![A-Za-z_]\w*\.[A-Za-z_])"
)

SECRET_PATTERNS = [
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\bglpat-[A-Za-z0-9_-]{20,}\b",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",
    r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"\bdop_v1_[a-f0-9]{64}\b",
    r"\b(?:eyJ[A-Za-z0-9_-]{10,}\.){2}[A-Za-z0-9_-]{10,}\b",  # JWT
    r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b",
    # URL authority credentials — a user:password pair before the host (connection strings).
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@",
    # key=value pairs for any credential-bearing setting name. The value must look like a
    # literal credential: no whitespace, at least 8 characters, and not a code lookup.
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b\s*[=:]\s*"
    + _NOT_CODE_VALUE
    + r"[\"']?\S{8,}",
    r"(?i)\bauthorization\b\s*:\s*(?:bearer|basic|token)\s+" + _NOT_CODE_VALUE + r"\S{8,}",
    # PEM/OpenSSH public keys are not secret but do not belong in a PR body or memory.
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PUBLIC KEY-----",
]


def redact(value: str) -> str:
    for name in (
        "openrouter_api_key",
        "telegram_bot_token",
        "github_token",
        "sandbox_token",
        "api_token",
        "coolify_token",
        "database_url",
        "database_password",
    ):
        secret = getattr(settings, name, "")
        if secret and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        value = re.sub(pattern, "[REDACTED]", value)
    return value


def secret_present(value: str) -> bool:
    return redact(value) != value


def validate_path(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if (
        not path
        or p.is_absolute()
        or any(x in {"..", ".git"} for x in p.parts)
        or "\\" in path
        or "\x00" in path
    ):
        raise ValueError("Unsafe repository path")
    if not p.parts or p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
        raise ValueError("Secrets and Git internals are inaccessible")
    if p.suffix in {".pem", ".key", ".p12"} or p.name in {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
    }:
        raise ValueError("Credential files are inaccessible")
    return p
