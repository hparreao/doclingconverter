"""Data-plane Lambda: a single temporary Docling conversion per accepted job."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from docling.document_converter import DocumentConverter

from lambda_app.common import ALLOWED_FORMATS, MAX_FILE_BYTES, WORKER_LOCK_KEY, safe_filename, signature_matches

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
TABLE = dynamodb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["ARTIFACTS_BUCKET"]


def _begin(job_id: str) -> dict[str, Any] | None:
    try:
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :running, startedAt = :started",
            ConditionExpression="#status = :submitted",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":running": "running", ":submitted": "submitted", ":started": int(time.time())},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return TABLE.get_item(Key={"jobId": job_id}).get("Item")


def _fail(job_id: str) -> None:
    TABLE.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET #status = :failed, finishedAt = :finished",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":failed": "failed", ":finished": int(time.time())},
    )


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


def _convert(input_path: Path, filename: str, formats: list[str]) -> dict[str, Any]:
    extension = input_path.suffix.lower()
    if not safe_filename(filename):
        raise ValueError("invalid filename")
    if input_path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file too large")
    with input_path.open("rb") as source:
        if not signature_matches(extension, source.read(16)):
            raise ValueError("signature mismatch")
    converter = DocumentConverter()
    document = converter.convert(str(input_path)).document
    outputs: dict[str, Any] = {}
    if "md" in formats:
        outputs["md"] = document.export_to_markdown()
    if "json" in formats:
        outputs["json"] = document.export_to_dict()
    return {"documents": [{"filename": filename, "outputs": outputs}]}


def handler(event: dict[str, Any], _context: Any) -> None:
    """Consume one submitted job; failures never expose document details to clients."""
    job_id = str(event.get("jobId", ""))
    job = _begin(job_id)
    if not job:
        return
    input_path = Path("/tmp") / f"{job_id}-{job['filename']}"
    try:
        metadata = s3.head_object(Bucket=BUCKET, Key=job["inputKey"])
        if int(metadata.get("ContentLength", 0)) > MAX_FILE_BYTES:
            raise ValueError("file too large")
        s3.download_file(BUCKET, job["inputKey"], str(input_path))
        formats = [value for value in job.get("outputFormats", []) if value in ALLOWED_FORMATS]
        payload = _convert(input_path, job["filename"], formats)
        result_key = f"results/{job_id}/result.json"
        s3.put_object(
            Bucket=BUCKET,
            Key=result_key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        TABLE.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #status = :completed, resultKey = :result, finishedAt = :finished",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":completed": "completed", ":result": result_key, ":finished": int(time.time())},
        )
    except Exception:
        _fail(job_id)
    finally:
        input_path.unlink(missing_ok=True)
        _release_worker_lock(job_id)
        try:
            s3.delete_object(Bucket=BUCKET, Key=job["inputKey"])
        except ClientError:
            pass
