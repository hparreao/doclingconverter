import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
API = ROOT / "lambda_app" / "api.py"
WORKER = ROOT / "lambda_app" / "worker.py"
FOUNDATION = ROOT / "infrastructure" / "foundation.yaml"
SERVICE = ROOT / "infrastructure" / "service.yaml"


class LambdaGuardrailTests(unittest.TestCase):
    def test_handlers_are_valid_python(self):
        ast.parse(API.read_text(encoding="utf-8"))
        ast.parse(WORKER.read_text(encoding="utf-8"))

    def test_uploads_use_s3_and_results_are_expiring(self):
        source = API.read_text(encoding="utf-8")
        self.assertIn("generate_presigned_post", source)
        self.assertIn("content-length-range", source)
        self.assertIn("MAX_JOBS_PER_MONTH", source)
        self.assertIn("is_site_origin", source)
        self.assertIn("transact_write_items", source)
        self.assertIn("_consume_api_request_quota", source)
        self.assertIn("UPLOAD_URL_TTL_SECONDS", source)
        self.assertIn("accessTokenHash", source)
        self.assertIn("hmac.compare_digest", source)
        common = (ROOT / "lambda_app" / "common.py").read_text(encoding="utf-8")
        self.assertNotIn('headers["access-control-allow-origin"]', common)
        template = FOUNDATION.read_text(encoding="utf-8")
        self.assertIn("BlockPublicPolicy: true", template)
        self.assertIn("ExpirationInDays: 1", template)
        self.assertIn("TimeToLiveSpecification", template)
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("x-job-token", service)

    def test_worker_removes_input_and_never_returns_document_errors(self):
        source = WORKER.read_text(encoding="utf-8")
        self.assertIn("s3.delete_object", source)
        self.assertIn("input_path.unlink", source)
        self.assertIn("except Exception as error:", source)
        self.assertIn("conversion_failed job_id=%s stage=%s error_type=%s", source)
        self.assertNotIn("str(error)", source)

    def test_service_has_serialized_x86_worker(self):
        template = SERVICE.read_text(encoding="utf-8")
        self.assertIn("- x86_64", template)
        self.assertIn("Timeout: 600", template)
        self.assertIn("MemorySize: 3008", template)
        self.assertIn("MaximumRetryAttempts: 0", template)
        self.assertIn("InvokedViaFunctionUrl: true", template)
        api = (ROOT / "lambda_app" / "api.py").read_text(encoding="utf-8")
        worker = (ROOT / "lambda_app" / "worker.py").read_text(encoding="utf-8")
        self.assertIn("WORKER_LOCK_KEY", api)
        self.assertIn("ConditionExpression=\"attribute_not_exists(jobId) OR expiresAt < :now\"", api)
        self.assertIn("_release_worker_lock(job_id)", worker)

    def test_deployment_scans_the_published_lambda_image(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy-aws-lambda.yml").read_text(encoding="utf-8")
        foundation = FOUNDATION.read_text(encoding="utf-8")
        self.assertIn("aquasecurity/trivy-action@v0.36.0", workflow)
        self.assertIn("severity: HIGH,CRITICAL", workflow)
        self.assertIn("exit-code: '1'", workflow)
        self.assertIn("ecr:GetDownloadUrlForLayer", foundation)

    def test_lambda_image_excludes_the_unused_server_runtime(self):
        dockerfile = (ROOT / "Dockerfile.lambda").read_text(encoding="utf-8")
        self.assertIn("python:3.12-slim@sha256:", dockerfile)
        self.assertIn("docling-slim[format-iwork]==2.124.0", dockerfile)
        self.assertNotIn("docling-serve-cpu", dockerfile)
        self.assertNotIn("\n      ray", dockerfile.lower())

    def test_public_source_does_not_embed_aws_credentials(self):
        prohibited = ("AKIA", "ASIA", "aws_secret_access_key", "aws_access_key_id")
        files = [
            path
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts and "tests" not in path.parts
        ]
        corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files)
        self.assertFalse(any(marker.lower() in corpus.lower() for marker in prohibited))
