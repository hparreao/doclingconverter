"""Control-plane Lambda: signed upload, bounded queue admission, and polling."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
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
    UPLOAD_URL_TTL_SECONDS,
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
UTC = timezone.utc


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


def _job_quota_detail(error: ClientError) -> str:
    """Shape admission-limit failures without exposing counter implementation."""
    reasons = error.response.get("CancellationReasons", [])
    if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
        return "This network has reached its daily conversion quota. Try again tomorrow."
    return "The public conversion quota is exhausted. Try again later."


def _job_token(event: dict[str, Any]) -> str | None:
    headers = {str(key).lower(): str(value) for key, value in event.get("headers", {}).items()}
    token = headers.get("x-job-token", "")
    return token if 32 <= len(token) <= 200 else None


def _job_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorized_job(event: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a job only when its opaque browser capability matches."""
    token = _job_token(event)
    expected = str(job.get("accessTokenHash", "")) if job else ""
    if not token or not expected or not hmac.compare_digest(_job_token_hash(token), expected):
        return None
    return job


def _consume_api_request_quota(event: dict[str, Any]) -> bool:
    """Bound public API routes, including polling and job creation."""
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
    job_id = str(uuid.uuid4())
    input_key = f"uploads/{job_id}/{filename}"
    expires_at = now + RESULT_TTL_SECONDS
    content_type = str(request.get("contentType") or "application/octet-stream")[:120]
    access_token = secrets.token_urlsafe(32)
    job = {
        "jobId": job_id,
        "status": "created",
        "filename": filename,
        "inputKey": input_key,
        "outputFormats": formats,
        "createdAt": now,
        "expiresAt": expires_at,
        "accessTokenHash": _job_token_hash(access_token),
    }
    TABLE.put_item(Item=job, ConditionExpression="attribute_not_exists(jobId)")
    try:
        upload = s3.generate_presigned_post(
            Bucket=BUCKET,
            Key=input_key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_FILE_BYTES]],
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        )
    except ClientError:
        # Do not leave a usable job if issuing its upload policy failed.
        try:
            TABLE.delete_item(
                Key={"jobId": job_id},
                ConditionExpression="#status = :created",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":created": "created"},
            )
        except ClientError:
            pass
        raise
    return response(201, {"jobId": job_id, "jobToken": access_token, "upload": upload, "expiresAt": expires_at}, SITE_ORIGIN)


def _submit_transaction(job: dict[str, Any], source_ip: str) -> None:
    """Reserve work and debit conversion quota only after S3 has the upload."""
    now = int(time.time())
    lock = {
        "Put": {
            "TableName": TABLE.name,
            "Item": {
                "jobId": {"S": WORKER_LOCK_KEY},
                "activeJobId": {"S": job["jobId"]},
                "expiresAt": {"N": str(now + WORKER_LOCK_TTL_SECONDS)},
            },
            "ConditionExpression": "attribute_not_exists(jobId) OR expiresAt < :now",
            "ExpressionAttributeValues": {":now": {"N": str(now)}},
        }
    }
    status = {
        "Update": {
            "TableName": TABLE.name,
            "Key": {"jobId": {"S": job["jobId"]}},
            "UpdateExpression": "SET #status = :submitted",
            "ConditionExpression": "#status = :created",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":submitted": {"S": "submitted"}, ":created": {"S": "created"}},
        }
    }
    if job.get("quotaConsumedAt"):
        DYNAMODB_CLIENT.transact_write_items(TransactItems=[lock, status])
        return

    month_key = datetime.now(UTC).strftime("%Y-%m")
    day_key = datetime.now(UTC).strftime("%Y-%m-%d")
    status["Update"]["UpdateExpression"] = "SET #status = :submitted, quotaConsumedAt = :now"
    status["Update"]["ExpressionAttributeValues"][":now"] = {"N": str(now)}
    DYNAMODB_CLIENT.transact_write_items(
        TransactItems=[
            _quota_update(f"quota#month#{month_key}", MAX_JOBS_PER_MONTH, now + RESULT_TTL_SECONDS * 32),
            _quota_update(
                f"quota#ip#{day_key}#{source_ip}", MAX_JOBS_PER_IP_PER_DAY, now + RESULT_TTL_SECONDS * 2
            ),
            lock,
            status,
        ]
    )


def _submit_job(event: dict[str, Any], job_id: str) -> dict[str, Any]:
    job = _authorized_job(event, TABLE.get_item(Key={"jobId": job_id}).get("Item"))
    if not job:
        return response(404, {"detail": "Conversion not found."}, SITE_ORIGIN)
    if job.get("status") != "created":
        return response(409, {"detail": "This upload cannot be submitted."}, SITE_ORIGIN)
    try:
        s3.head_object(Bucket=BUCKET, Key=job["inputKey"])
    except ClientError:
        return response(422, {"detail": "Upload the selected file before submitting it."}, SITE_ORIGIN)
    try:
        _submit_transaction(job, _request_ip(event))
    except ClientError as error:
        if _is_conditional_failure(error):
            reasons = error.response.get("CancellationReasons", [])
            lock_index = 0 if job.get("quotaConsumedAt") else 2
            if len(reasons) > lock_index and reasons[lock_index].get("Code") == "ConditionalCheckFailed":
                return response(429, {"detail": "Another conversion is running. Retry this upload shortly."}, SITE_ORIGIN)
            if job.get("quotaConsumedAt"):
                return response(409, {"detail": "This upload was already submitted."}, SITE_ORIGIN)
            return response(429, {"detail": _job_quota_detail(error)}, SITE_ORIGIN)
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


def _read_job(event: dict[str, Any], job_id: str) -> dict[str, Any]:
    job = _authorized_job(event, TABLE.get_item(Key={"jobId": job_id}).get("Item"))
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
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": {"cache-control": "no-store"}, "body": ""}
    if not _consume_api_request_quota(event):
        return response(429, {"detail": "The public request rate limit has been reached. Try again later."}, SITE_ORIGIN)
    path = event.get("rawPath", "")
    if method == "POST" and path == "/jobs":
        return _create_job(event)
    job_id = job_id_from_path(path)
    if method == "POST" and job_id and path.endswith("/submit"):
        return _submit_job(event, job_id)
    if method == "GET" and job_id:
        return _read_job(event, job_id)
    if method == "GET" and path == "/capabilities":
        return response(200, {"maxFileBytes": MAX_FILE_BYTES, "maxFiles": 1, "outputFormats": sorted(ALLOWED_FORMATS)}, SITE_ORIGIN)
    return response(404, {"detail": "Route not found."}, SITE_ORIGIN)
