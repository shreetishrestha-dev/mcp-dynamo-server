"""DynamoDB error interpretation for LLM-friendly responses.

Translates common botocore ``ClientError`` exceptions into plain-English,
structured dicts that tool handlers can return directly. This avoids surfacing
raw AWS error codes to the LLM, which tend to cause retry confusion.
"""

from __future__ import annotations

from botocore.exceptions import ClientError


def interpret_dynamo_error(exc: ClientError) -> dict:
    """Translate a boto3 ``ClientError`` into a plain-English tool response.

    Returns a dict with ``{"error": "<Code>", "message": "<plain English>"}``
    that callers should return directly from the tool handler.

    Unknown error codes fall through to a generic wrapper that still includes
    the code and HTTP status for debuggability.
    """
    error = exc.response.get("Error", {})
    code = error.get("Code", "Unknown")
    raw_message = error.get("Message", str(exc))
    http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

    match code:
        case "ConditionalCheckFailedException":
            message = (
                "The condition expression failed — the item may not exist or was modified "
                "concurrently. Check your ConditionExpression and retry."
            )
        case "ProvisionedThroughputExceededException":
            message = (
                "Read/write capacity exhausted. Wait a moment and retry, or switch the "
                "table to on-demand billing."
            )
        case "ResourceNotFoundException":
            message = "Table does not exist or is not yet active."
        case "ValidationException":
            message = (
                f"DynamoDB rejected the request: {raw_message}. "
                "Check expression syntax and attribute names."
            )
        case "TransactionConflictException":
            message = (
                "A conflicting transaction is in progress on this item. "
                "Retry after a short delay."
            )
        case "ItemCollectionSizeLimitExceededException":
            message = "The item collection has reached the 10 GB limit."
        case "RequestLimitExceeded":
            message = (
                "AWS request rate limit hit. Slow down and retry with exponential backoff."
            )
        case _:
            # Generic wrapper: always includes code + HTTP status for debuggability.
            parts = [f"DynamoDB error ({code})"]
            if http_status is not None:
                parts.append(f"HTTP {http_status}")
            parts.append(raw_message)
            message = ": ".join(parts)

    return {"error": code, "message": message}


__all__ = ["interpret_dynamo_error"]
