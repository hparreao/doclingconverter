"""Control-plane Lambda: signed upload, bounded queue admission, and polling."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from lambda_app.common import (
    ALLOWED_FORMATS,
    MAX_API_REQUESTS_PER_DAY,
    MAX_API_REQUESTS_PER_IP_PER_MINUTE,
    MAX_API_REQUESTS_PER_MONTH,
    MAX_FILE_BYTES,
    MAX_JOBS_PER_IP_PER_DAY,
    MAX_JOBS_PER_MONTH,
    RESULT_TTL_SECONDS,
    WORKER_LOCK_KEY,
    WORKER_LOCK_TTL_SECONDS,
    is_site_origin,
    job_id_from_path,
    parse_json_body,
    response,
    safe_filename,
)

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")
TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])
DYNAMODB_CLIENT = boto3.client("dynamodb")
BUCKET = os.environ["ARTIFACTS_BUCKET"]
WORKER_FUNCTION = os.environ["WORKER_FUNCTION"]
SITE_ORIGIN = os.environ["SITE_ORIGIN"]
SERIALIZER = TypeSerializer()


def _request_ip(event: dict[str, Any]) -> str:
    return str(event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown"))


def _quota_update(key: str, limit: int, expiry: int) -> dict[str, Any]:
    """Create one bounded DynamoDB counter mutation for a transaction."""
    return {
        "Update": {
            "TableName": TABLE.name,
            "Key": {"jobId": {"S": key}},
            "UpdateExpression": "SET expiresAt = :expiry ADD requestCount :one",
            "ConditionExpression": "attribute_not_exists(requestCount) OR requestCount < :limit",
            "ExpressionAttributeValues": {
                ":one": {"N": "1"},
                ":limit": {"N": str(limit)},
                ":expiry": {"N": str(expiry)},
            },
        }
    }


def _is_conditional_failure(error: ClientError) -> bool:
    return error.response["Error"]["Code"] in {"ConditionalCheckFailedException", "TransactionCanceledException"}


def _consume_api_request_quota(event: dict[str, Any]) -> bool:
    """Bound all public Function URL traffic, including polling and preflight."""
    now = int(time.time())
    current = datetime.now(UTC)
    minute_key = current.strftime("%Y-%m-%dT%H:%M")
    day_key = current.strftime("%Y-%m-%d")
    month_key = current.strftime("%Y-%m")
    try:
        DYNAMODB_CLIENT.transact_write_items(
            TransactItems=[
                _quota_update(
                    f"quota#api-ip-minute#{minute_key}#{_request_ip(event)}",
                    MAX_API_REQUESTS_PER_IP_PER_MINUTE,
                    now + 2 * 60,
                ),
                _quota_update(f"quota#api-day#{day_key}", MAX_API_REQUESTS_PER_DAY, now + 2 * RESULT_TTL_SECONDS),
                _quota_update(f"quota#api-month#{month_key}", MAX_API_REQUESTS_PER_MONTH, now + 32 * RESULT_TTL_SECONDS),
            ]
        )
        return True
    except ClientError as error:
        if _is_conditional_failure(error):
            return False
        raise


def _create_job(event: dict[str, Any]) -> dict[str, Any]:
    request = parse_json_body(event)
    if request is None:
        return response(400, {"detail": "Malformed request."}, SITE_ORIGIN)
    filename = safe_filename(str(request.get("filename", "")))
    size = request.get("size")
    formats = request.get("toFormats", ["md"])
    if not filename or not isinstance(size, int) or not 1 <= size <= MAX_FILE_BYTES:
        return response(422, {"detail": "The file is not allowed."}, SITE_ORIGIN)
    if (
        not isinstance(formats, list)
        or not formats
        or not all(isinstance(value, str) for value in formats)
        or not set(formats).issubset(ALLOWED_FORMATS)
    ):
        return response(422, {"detail": "Choose one or more supported output formats."}, SITE_ORIGIN)

    now = int(time.time())
    month_key = datetime.now(UTC).strftime("%Y-%m")
    day_key = datetime.now(UTC).strftime("%Y-%m-%d")
    job_id = str(uuid.uuid4())
    input_key = f"uploads/{job_id}/{filename}"
    expires_at = now + RESULT_TTL_SECONDS
    content_type = str(request.get("contentType") or "application/octet-stream")[:120]
    job = {
        "jobId": job_id,
        "status": "created",
        "filename": filename,
        "inputKey": input_key,
        "outputFormats": formats,
        "createdAt": now,
        "expiresAt": expires_at,
    }
    try:
        DYNAMODB_CLIENT.transact_write_items(
            TransactItems=[
                _quota_update(f"quota#month#{month_key}", MAX_JOBS_PER_MONTH, now + RESULT_TTL_SECONDS * 32),
                _quota_update(
                    f"quota#ip#{day_key}#{_request_ip(event)}",
                    MAX_JOBS_PER_IP_PER_DAY,
                    now + RESULT_TTL_SECONDS * 2,
                ),
                {
                    "Put": {
                        "TableName": TABLE.name,
                        "Item": {key: SERIALIZER.serialize(value) for key, value in job.items()},
                        "ConditionExpression": "attribute_not_exists(jobId)",
                    }
                },
            ]
        )
    except ClientError as error:
        if _is_conditional_failure(error):
            return response(429, {"detail": "The public conversion quota is exhausted. Try again later."}, SITE_ORIGIN)
        raise
    upload = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=input_key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_FILE_BYTES]],
        ExpiresIn=300,
    )
    return response(201, {"jobId": job_id, "upload": upload, "expiresAt": expires_at}, SITE_ORIGIN)


def _submit_job(event: dict[str, Any], job_id: str) -> dict[str, Any]:
    job = TABLE.get_item(Key={"jobId": job_id}).get("Item")
    if not job or job.get("status") != "created":
        return response(409, {"detail": "This upload cannot be submitted."}, SITE_ORIGIN)
    try:
        s3.head_object(Bucket=BUCKET, Key=job["inputKey"])
    except ClientError:
        return response(422, {"detail": "Upload the selected file before submitting it."}, SITE_ORIGIN)
    try:
        TABLE.put_item(
            Item={
                "jobId": WORKER_LOCK_KEY,
                "activeJobId": job_id,
                "expiresAt": int(time.time()) + WORKER_LOCK_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(jobId) OR expiresAt < :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(429, {"detail": "Another conversion is running. Retry this upload shortly."}, SITE_ORIGIN)
        raise
    try:
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :submitted",
            ConditionExpression="#status = :created",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":submitted": "submitted", ":created": "created"},
        )
    except ClientError as error:
        _release_worker_lock(job_id)
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(409, {"detail": "This upload was already submitted."}, SITE_ORIGIN)
        raise
    try:
        lambda_client.invoke(FunctionName=WORKER_FUNCTION, InvocationType="Event", Payload=f'{{"jobId":"{job_id}"}}'.encode())
    except ClientError:
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :created",
            ConditionExpression="#status = :submitted",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":created": "created", ":submitted": "submitted"},
        )
        _release_worker_lock(job_id)
        return response(503, {"detail": "The converter is temporarily unavailable. Retry shortly."}, SITE_ORIGIN)
    return response(202, {"jobId": job_id, "status": "submitted"}, SITE_ORIGIN)


def _release_worker_lock(job_id: str) -> None:
    try:
        TABLE.delete_item(
            Key={"jobId": WORKER_LOCK_KEY},
            ConditionExpression="activeJobId = :job_id",
            ExpressionAttributeValues={":job_id": job_id},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _read_job(job_id: str) -> dict[str, Any]:
    job = TABLE.get_item(Key={"jobId": job_id}).get("Item")
    if not job or int(job.get("expiresAt", 0)) <= int(time.time()):
        return response(404, {"detail": "Conversion not found."}, SITE_ORIGIN)
    payload: dict[str, Any] = {"jobId": job_id, "status": job.get("status", "unknown")}
    if job.get("status") == "completed" and job.get("resultKey"):
        payload["resultUrl"] = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": job["resultKey"]}, ExpiresIn=600
        )
    if job.get("status") == "failed":
        payload["detail"] = "Conversion failed. Check the supported formats and limits."
    return response(200, payload, SITE_ORIGIN)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Route a strict, browser-only Function URL API."""
    if not is_site_origin(event, SITE_ORIGIN):
        return response(403, {"detail": "This API is available through the converter site only."})
    if not _consume_api_request_quota(event):
        return response(429, {"detail": "The public request rate limit has been reached. Try again later."}, SITE_ORIGIN)
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "access-control-allow-origin": SITE_ORIGIN,
                "access-control-allow-methods": "GET,POST,OPTIONS",
                "access-control-allow-headers": "content-type",
                "vary": "Origin",
            },
            "body": "",
        }
    if method == "POST" and path == "/jobs":
        return _create_job(event)
    job_id = job_id_from_path(path)
    if method == "POST" and job_id and path.endswith("/submit"):
        return _submit_job(event, job_id)
    if method == "GET" and job_id:
        return _read_job(job_id)
    if method == "GET" and path == "/capabilities":
        return response(200, {"maxFileBytes": MAX_FILE_BYTES, "maxFiles": 1, "outputFormats": sorted(ALLOWED_FORMATS)}, SITE_ORIGIN)
    return response(404, {"detail": "Route not found."}, SITE_ORIGIN)
