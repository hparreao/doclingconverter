"""Control-plane Lambda: signed upload, bounded queue admission, and polling."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from lambda_app.common import (
    ALLOWED_FORMATS,
    MAX_FILE_BYTES,
    MAX_JOBS_PER_IP_PER_DAY,
    MAX_JOBS_PER_MONTH,
    RESULT_TTL_SECONDS,
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
BUCKET = os.environ["ARTIFACTS_BUCKET"]
WORKER_FUNCTION = os.environ["WORKER_FUNCTION"]
SITE_ORIGIN = os.environ["SITE_ORIGIN"]


def _request_ip(event: dict[str, Any]) -> str:
    return str(event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown"))


def _consume_quota(key: str, limit: int, expiry: int) -> bool:
    try:
        TABLE.update_item(
            Key={"jobId": key},
            UpdateExpression="SET expiresAt = :expiry ADD requestCount :one",
            ConditionExpression="attribute_not_exists(requestCount) OR requestCount < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":expiry": expiry},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
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
    if not _consume_quota(f"quota#month#{month_key}", MAX_JOBS_PER_MONTH, now + RESULT_TTL_SECONDS * 32):
        return response(429, {"detail": "The public monthly conversion quota is exhausted."}, SITE_ORIGIN)
    if not _consume_quota(f"quota#ip#{day_key}#{_request_ip(event)}", MAX_JOBS_PER_IP_PER_DAY, now + RESULT_TTL_SECONDS * 2):
        return response(429, {"detail": "The daily conversion quota for this network is exhausted."}, SITE_ORIGIN)

    job_id = str(uuid.uuid4())
    input_key = f"uploads/{job_id}/{filename}"
    expires_at = now + RESULT_TTL_SECONDS
    content_type = str(request.get("contentType") or "application/octet-stream")[:120]
    TABLE.put_item(
        Item={
            "jobId": job_id,
            "status": "created",
            "filename": filename,
            "inputKey": input_key,
            "outputFormats": formats,
            "createdAt": now,
            "expiresAt": expires_at,
        },
        ConditionExpression="attribute_not_exists(jobId)",
    )
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
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :submitted",
            ConditionExpression="#status = :created",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":submitted": "submitted", ":created": "created"},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return response(409, {"detail": "This upload was already submitted."}, SITE_ORIGIN)
        raise
    lambda_client.invoke(FunctionName=WORKER_FUNCTION, InvocationType="Event", Payload=f'{{"jobId":"{job_id}"}}'.encode())
    return response(202, {"jobId": job_id, "status": "submitted"}, SITE_ORIGIN)


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
