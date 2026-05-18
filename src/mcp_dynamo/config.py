"""Environment-driven configuration for the DynamoDB MCP server.

Single source of truth for credential and endpoint resolution. Reads env vars
once at startup and fails fast with an actionable error message if no usable
credentials are present.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when the server cannot resolve a usable AWS configuration."""


@dataclass(frozen=True)
class Config:
    """Resolved server configuration.

    Secret-bearing fields use ``field(repr=False)`` so an accidental
    ``repr(cfg)`` (e.g. via a logging call or an exception traceback) does not
    leak credentials.
    """

    region: str
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    profile: str | None = None
    endpoint_url: str | None = None
    allowed_tables: frozenset[str] | None = None
    blocked_tables: frozenset[str] | None = None

    @property
    def has_explicit_keys(self) -> bool:
        return bool(self.access_key and self.secret_key)

    @property
    def has_profile(self) -> bool:
        return bool(self.profile)

    @property
    def has_endpoint_override(self) -> bool:
        """True iff a ``DYNAMODB_ENDPOINT_URL`` was supplied."""
        return bool(self.endpoint_url)

    @property
    def is_local(self) -> bool:
        """True iff the endpoint override points at localhost / 127.0.0.1.

        Kept separately from ``has_endpoint_override`` so callers and the
        ``dynamo_whoami`` tool can distinguish "DynamoDB Local" from "custom
        VPC endpoint" — both override the URL but only one is the toy server.
        """
        if not self.endpoint_url:
            return False
        url = self.endpoint_url.lower()
        return (
            "localhost" in url
            or "127.0.0.1" in url
            or "0.0.0.0" in url
        )


def _get_env(*names: str) -> str | None:
    """Return the first defined env var (in order). Empty strings count.

    Uses ``is not None`` rather than a truthy check so that intentionally
    empty values (e.g. ``AWS_PROFILE=""`` to override an inherited profile)
    are preserved and surfaced as-is.
    """
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return None


def _parse_table_list(env_var: str) -> frozenset[str] | None:
    """Parse a comma-separated list of table names from an env var.

    Returns ``None`` if the env var is unset or empty (meaning no restriction).
    Strips whitespace from each name.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return None
    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    return names if names else None


def load_config() -> Config:
    """Build a Config from environment variables.

    Resolution order for credentials:
      1. Explicit env vars (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)
      2. AWS_PROFILE
      3. boto3 default chain (we don't pre-validate this; we let boto3 try)

    DYNAMODB_ENDPOINT_URL overrides the AWS endpoint and is the trigger for
    DynamoDB Local. Region is always required; we accept AWS_REGION or
    AWS_DEFAULT_REGION.

    DYNAMODB_ALLOWED_TABLES / DYNAMODB_BLOCKED_TABLES restrict which tables
    tools may access. If both are set, ALLOWED_TABLES takes precedence and a
    warning is emitted on stderr.
    """
    region = _get_env("AWS_REGION", "AWS_DEFAULT_REGION")
    access_key = _get_env("AWS_ACCESS_KEY_ID")
    secret_key = _get_env("AWS_SECRET_ACCESS_KEY")
    session_token = _get_env("AWS_SESSION_TOKEN")
    profile = _get_env("AWS_PROFILE")
    endpoint_url = _get_env("DYNAMODB_ENDPOINT_URL")
    allowed_tables = _parse_table_list("DYNAMODB_ALLOWED_TABLES")
    blocked_tables = _parse_table_list("DYNAMODB_BLOCKED_TABLES")

    if not region:
        raise ConfigError(
            "AWS_REGION (or AWS_DEFAULT_REGION) is required. "
            "Set it in your MCP client's `env` block, e.g. \"AWS_REGION\": \"us-east-1\"."
        )

    if allowed_tables is not None and blocked_tables is not None:
        print(
            "[mcp-dynamo] Warning: Both DYNAMODB_ALLOWED_TABLES and DYNAMODB_BLOCKED_TABLES "
            "are set; ALLOWED_TABLES takes precedence.",
            file=sys.stderr,
        )

    # We don't hard-require explicit keys: boto3's default chain may still
    # resolve credentials (IAM role, SSO, container creds). client.py validates
    # by issuing a real call at startup.
    return Config(
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        profile=profile,
        endpoint_url=endpoint_url,
        allowed_tables=allowed_tables,
        blocked_tables=blocked_tables,
    )


def credentials_error_message() -> str:
    """Return the message shown when credential resolution fails at runtime."""
    return (
        "No AWS credentials resolved. Set one of:\n"
        "  - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (and optionally AWS_SESSION_TOKEN)\n"
        "  - AWS_PROFILE pointing at a profile in ~/.aws/credentials\n"
        "  - Run inside an environment with a default boto3 credential chain "
        "(IAM role, SSO session, container creds, etc.)\n"
        "For DynamoDB Local, any non-empty values for the key pair work; also set "
        "DYNAMODB_ENDPOINT_URL=http://localhost:8000."
    )
