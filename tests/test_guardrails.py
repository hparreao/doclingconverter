import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).parents[1] / "app" / "main.py"


class GuardrailContractTests(unittest.TestCase):
    def setUp(self):
        self.source = SOURCE.read_text(encoding="utf-8")

    def test_module_is_valid_python(self):
        ast.parse(self.source)

    def test_public_contract_is_upload_only(self):
        self.assertIn('UPLOAD_PATH = "/v1/convert/file/async"', self.source)
        self.assertIn('path.startswith("/v1/convert/source")', self.source)
        self.assertIn('"/source" not in path', self.source)

    def test_limits_and_no_store_are_enforced(self):
        self.assertIn('MAX_FILES = 5', self.source)
        self.assertIn('MAX_FILE_BYTES = 25 * 1024 * 1024', self.source)
        self.assertIn('b"cache-control", b"no-store"', self.source)

    def test_path_traversal_and_magic_bytes_are_checked(self):
        self.assertIn('PurePosixPath(normalized).name == filename', self.source)
        self.assertIn('def _signature_matches', self.source)
