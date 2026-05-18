"""boto3 client factory.

Builds a `dynamodb` client from a resolved `Config`, honoring explicit keys,
profile, and the optional `DYNAMODB_ENDPOINT_URL` override.

Caches each client per-process via ``functools.lru_cache``. This is safe for
the MCP stdio server because it is a single long-lived process — boto3
clients are not strictly thread-safe but are fine for this single-threaded
event-loop access pattern. If we ever fork or spawn worker processes, each
worker will build its own cache.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from mcp_dynamo.config import Config, ConfigError, credentials_error_message, load_config

if TYPE_CHECKING:
    # boto3 stubs aren't a hard dep; fall back to Any at runtime.
    try:
        from mypy_boto3_dynamodb import DynamoDBClient, DynamoDBServiceResource
        from mypy_boto3_sts import STSClient
    except ImportError:  # pragma: no cover - stub-only fallback
        DynamoDBClient = Any  # type: ignore[assignment, misc]
        DynamoDBServiceResource = Any  # type: ignore[assignment, misc]
        STSClient = Any  # type: ignore[assignment, misc]


def _build_session(cfg: Config) -> boto3.Session:
    """Construct a boto3 Session honoring explicit creds, then profile, then default chain."""
    if cfg.has_explicit_keys:
        return boto3.Session(
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            aws_session_token=cfg.session_token,
            region_name=cfg.region,
        )
    if cfg.has_profile:
        return boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
    return boto3.Session(region_name=cfg.region)


def _client_kwargs(cfg: Config) -> dict[str, Any]:
    return {"endpoint_url": cfg.endpoint_url} if cfg.endpoint_url else {}


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


@lru_cache(maxsize=1)
def get_dynamodb_client() -> DynamoDBClient:
    """Return a cached low-level DynamoDB client."""
    cfg = get_config()
    session = _build_session(cfg)
    return session.client("dynamodb", **_client_kwargs(cfg))


@lru_cache(maxsize=1)
def get_dynamodb_resource() -> DynamoDBServiceResource:
    """Return a cached high-level DynamoDB Resource (for friendlier dict <-> AttributeValue)."""
    cfg = get_config()
    session = _build_session(cfg)
    return session.resource("dynamodb", **_client_kwargs(cfg))


@lru_cache(maxsize=1)
def get_sts_client() -> STSClient:
    """Return a cached STS client. Used by ``dynamo_whoami`` to read caller identity."""
    cfg = get_config()
    session = _build_session(cfg)
    # STS has no regional endpoint override concept in our config.
    return session.client("sts")


def verify_credentials() -> None:
    """Issue a lightweight call to surface credential failures at startup.

    Tries ``list_tables`` (with a tiny limit) since it works against both AWS
    and DynamoDB Local. Catches every recognised boto3/botocore exception and
    wraps it in a ``ConfigError`` carrying an actionable message — raw boto3
    tracebacks must never reach stderr (where MCP hosts capture them).
    """
    try:
        client = get_dynamodb_client()
        client.list_tables(Limit=1)
    except NoCredentialsError as exc:
        raise ConfigError(credentials_error_message()) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if code in {
            "UnrecognizedClientException",
            "InvalidSignatureException",
            "InvalidClientTokenId",
            "AuthFailure",
            "AccessDenied",
            "AccessDeniedException",
            "ExpiredTokenException",
        }:
            raise ConfigError(
                f"AWS rejected the supplied credentials ({code}). "
                f"{credentials_error_message()}"
            ) from exc
        raise ConfigError(
            f"DynamoDB call failed during startup check ({code}): {message}"
        ) from exc
    except BotoCoreError as exc:
        raise ConfigError(
            f"Failed to reach DynamoDB endpoint: {exc}. "
            "If using DynamoDB Local, confirm the container is running and "
            "DYNAMODB_ENDPOINT_URL is correct."
        ) from exc


def reset_clients() -> None:
    """Clear cached clients. Used by tests."""
    get_config.cache_clear()
    get_dynamodb_client.cache_clear()
    get_dynamodb_resource.cache_clear()
    get_sts_client.cache_clear()
