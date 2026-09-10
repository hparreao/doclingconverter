"""FastAPI control plane for the public Lambda Function URL.

The document bytes bypass this API: a browser uploads them directly to the
private S3 bucket through a short-lived POST policy. FastAPI owns the JSON
control plane and Mangum adapts its ASGI application to Lambda.
"""

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
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from mangum import Mangum

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
    job_id_from_path,
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
UTC = timezone.utc


def _json(status_code: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
    )


def _headers(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.headers.items()}


def _request_ip(request: Request) -> str:
    """Read Lambda's source IP when present, with a safe ASGI-test fallback."""
    event = request.scope.get("aws.event")
    if isinstance(event, dict):
        source_ip = event.get("requestContext", {}).get("http", {}).get("sourceIp")
        if source_ip:
            return str(source_ip)
    return request.client.host if request.client else "unknown"


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


def _job_token(headers: dict[str, str]) -> str | None:
    token = headers.get("x-job-token", "")
    return token if 32 <= len(token) <= 200 else None


def _job_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorized_job(headers: dict[str, str], job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a job only when its opaque browser capability matches."""
    token = _job_token(headers)
    expected = str(job.get("accessTokenHash", "")) if job else ""
    if not token or not expected or not hmac.compare_digest(_job_token_hash(token), expected):
        return None
    return job


def _consume_api_request_quota(request: Request) -> bool:
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
                    f"quota#api-ip-minute#{minute_key}#{_request_ip(request)}",
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


def _create_job(payload: dict[str, Any]) -> JSONResponse:
    filename = safe_filename(str(payload.get("filename", "")))
    size = payload.get("size")
    formats = payload.get("toFormats", ["md"])
    if not filename or not isinstance(size, int) or not 1 <= size <= MAX_FILE_BYTES:
        return _json(422, {"detail": "The file is not allowed."})
    if (
        not isinstance(formats, list)
        or not formats
        or not all(isinstance(value, str) for value in formats)
        or not set(formats).issubset(ALLOWED_FORMATS)
    ):
        return _json(422, {"detail": "Choose one or more supported output formats."})

    now = int(time.time())
    job_id = str(uuid.uuid4())
    input_key = f"uploads/{job_id}/{filename}"
    expires_at = now + RESULT_TTL_SECONDS
    content_type = str(payload.get("contentType") or "application/octet-stream")[:120]
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
        # A job whose upload policy could not be issued must not remain usable.
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
    return _json(201, {"jobId": job_id, "jobToken": access_token, "upload": upload, "expiresAt": expires_at})


def _submit_transaction(job: dict[str, Any], source_ip: str) -> None:
    """Serialize a job and debit conversion quota only once after an upload."""
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


def _submit_job(request: Request, job_id: str) -> JSONResponse:
    job = _authorized_job(_headers(request), TABLE.get_item(Key={"jobId": job_id}).get("Item"))
    if not job:
        return _json(404, {"detail": "Conversion not found."})
    if job.get("status") != "created":
        return _json(409, {"detail": "This upload cannot be submitted."})
    try:
        s3.head_object(Bucket=BUCKET, Key=job["inputKey"])
    except ClientError:
        return _json(422, {"detail": "Upload the selected file before submitting it."})
    try:
        _submit_transaction(job, _request_ip(request))
    except ClientError as error:
        if _is_conditional_failure(error):
            reasons = error.response.get("CancellationReasons", [])
            lock_index = 0 if job.get("quotaConsumedAt") else 2
            if len(reasons) > lock_index and reasons[lock_index].get("Code") == "ConditionalCheckFailed":
                return _json(429, {"detail": "Another conversion is running. Retry this upload shortly."})
            if job.get("quotaConsumedAt"):
                return _json(409, {"detail": "This upload was already submitted."})
            return _json(429, {"detail": _job_quota_detail(error)})
        raise
    try:
        lambda_client.invoke(
            FunctionName=WORKER_FUNCTION,
            InvocationType="Event",
            Payload=f'{{"jobId":"{job_id}"}}'.encode(),
        )
    except ClientError:
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :created",
            ConditionExpression="#status = :submitted",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":created": "created", ":submitted": "submitted"},
        )
        _release_worker_lock(job_id)
        return _json(503, {"detail": "The converter is temporarily unavailable. Retry shortly."})
    return _json(202, {"jobId": job_id, "status": "submitted"})


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


def _read_job(request: Request, job_id: str) -> JSONResponse:
    job = _authorized_job(_headers(request), TABLE.get_item(Key={"jobId": job_id}).get("Item"))
    if not job or int(job.get("expiresAt", 0)) <= int(time.time()):
        return _json(404, {"detail": "Conversion not found."})
    payload: dict[str, Any] = {"jobId": job_id, "status": job.get("status", "unknown")}
    if job.get("status") == "completed" and job.get("resultKey"):
        payload["resultUrl"] = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": job["resultKey"]}, ExpiresIn=600
        )
    if job.get("status") == "failed":
        payload["detail"] = "Conversion failed. Check the supported formats and limits."
    return _json(200, payload)


def _valid_job_id(job_id: str) -> bool:
    return job_id_from_path(f"/jobs/{job_id}") is not None


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def public_boundary(request: Request, call_next: Any) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"cache-control": "no-store"})
        if _headers(request).get("origin") != SITE_ORIGIN:
            return _json(403, {"detail": "This API is available through the converter site only."})
        if not _consume_api_request_quota(request):
            return _json(429, {"detail": "The public request rate limit has been reached. Try again later."})
        return await call_next(request)

    @app.post("/jobs")
    async def create_job(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return _json(400, {"detail": "Malformed request."})
        if not isinstance(payload, dict):
            return _json(400, {"detail": "Malformed request."})
        return _create_job(payload)

    @app.post("/jobs/{job_id}/submit")
    async def submit_job(request: Request, job_id: str) -> JSONResponse:
        if not _valid_job_id(job_id):
            return _json(404, {"detail": "Route not found."})
        return _submit_job(request, job_id)

    @app.get("/jobs/{job_id}")
    async def read_job(request: Request, job_id: str) -> JSONResponse:
        if not _valid_job_id(job_id):
            return _json(404, {"detail": "Route not found."})
        return _read_job(request, job_id)

    @app.get("/capabilities")
    async def capabilities() -> JSONResponse:
        return _json(200, {"maxFileBytes": MAX_FILE_BYTES, "maxFiles": 1, "outputFormats": sorted(ALLOWED_FORMATS)})

    return app


app = create_app()
handler = Mangum(app, lifespan="off")
