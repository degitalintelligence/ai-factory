import re
from pathlib import PurePosixPath

from app.config import settings

SECRET_PATTERNS = [
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}\b",
    r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b",
    # scheme://user:password@host — credentials embedded in connection strings.
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@",
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
