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
        template = FOUNDATION.read_text(encoding="utf-8")
        self.assertIn("BlockPublicPolicy: true", template)
        self.assertIn("ExpirationInDays: 1", template)
        self.assertIn("TimeToLiveSpecification", template)

    def test_worker_removes_input_and_never_returns_document_errors(self):
        source = WORKER.read_text(encoding="utf-8")
        self.assertIn("s3.delete_object", source)
        self.assertIn("input_path.unlink", source)
        self.assertIn("except Exception:", source)
        self.assertNotIn("str(error)", source)

    def test_service_has_bounded_x86_worker(self):
        template = SERVICE.read_text(encoding="utf-8")
        self.assertIn("- x86_64", template)
        self.assertIn("ReservedConcurrentExecutions: 1", template)
        self.assertIn("Timeout: 900", template)
        self.assertIn("MemorySize: 6144", template)

    def test_public_source_does_not_embed_aws_credentials(self):
        prohibited = ("AKIA", "ASIA", "aws_secret_access_key", "aws_access_key_id")
        files = [
            path
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts and "tests" not in path.parts
        ]
        corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files)
        self.assertFalse(any(marker.lower() in corpus.lower() for marker in prohibited))
