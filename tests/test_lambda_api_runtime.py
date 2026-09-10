import importlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class RequestStub:
    def __init__(self, headers=None, source_ip="198.51.100.8"):
        self.headers = headers or {"origin": "https://converter.example"}
        self.client = SimpleNamespace(host=source_ip)
        self.scope = {"aws.event": {"requestContext": {"http": {"sourceIp": source_ip}}}}


class LambdaApiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s3 = MagicMock()
        cls.lambda_client = MagicMock()
        cls.dynamodb_client = MagicMock()
        cls.table = MagicMock()
        resource = MagicMock()
        resource.Table.return_value = cls.table

        def client_for(service_name, *args, **kwargs):
            return {"s3": cls.s3, "lambda": cls.lambda_client, "dynamodb": cls.dynamodb_client}[service_name]

        cls.environment = patch.dict(
            os.environ,
            {
                "JOBS_TABLE": "test-jobs",
                "ARTIFACTS_BUCKET": "test-artifacts",
                "WORKER_FUNCTION": "test-worker",
                "SITE_ORIGIN": "https://converter.example",
            },
            clear=False,
        )
        cls.boto_client = patch("boto3.client", side_effect=client_for)
        cls.boto_resource = patch("boto3.resource", return_value=resource)
        cls.environment.start()
        cls.boto_client.start()
        cls.boto_resource.start()
        sys.modules.pop("lambda_app.api", None)
        cls.api = importlib.import_module("lambda_app.api")

    @classmethod
    def tearDownClass(cls):
        cls.boto_resource.stop()
        cls.boto_client.stop()
        cls.environment.stop()
        sys.modules.pop("lambda_app.api", None)

    def setUp(self):
        self.s3.reset_mock()
        self.lambda_client.reset_mock()
        self.dynamodb_client.reset_mock()
        self.table.reset_mock()
        self.s3.generate_presigned_post.return_value = {"url": "https://upload.example", "fields": {}}

    def test_mangum_adapts_a_function_url_create_request(self):
        self.dynamodb_client.transact_write_items.return_value = {}
        event = {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": "/jobs",
            "rawQueryString": "",
            "headers": {"origin": "https://converter.example", "content-type": "application/json"},
            "requestContext": {"http": {"method": "POST", "path": "/jobs", "sourceIp": "198.51.100.9"}},
            "body": json.dumps({"filename": "sample.pdf", "size": 42, "toFormats": ["md"]}),
            "isBase64Encoded": False,
        }

        response = self.api.handler(event, None)

        self.assertEqual(201, response["statusCode"])
        self.assertIn("jobToken", json.loads(response["body"]))
        self.table.put_item.assert_called_once()
        self.s3.generate_presigned_post.assert_called_once()

    def test_wrong_origin_is_rejected_before_any_aws_mutation(self):
        event = {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": "/capabilities",
            "rawQueryString": "",
            "headers": {"origin": "https://attacker.example"},
            "requestContext": {"http": {"method": "GET", "path": "/capabilities", "sourceIp": "198.51.100.9"}},
            "isBase64Encoded": False,
        }

        response = self.api.handler(event, None)

        self.assertEqual(403, response["statusCode"])
        self.dynamodb_client.transact_write_items.assert_not_called()

    def test_create_only_records_pending_job_and_does_not_debit_conversion_quota(self):
        response = self.api._create_job({"filename": "sample.pdf", "size": 42, "toFormats": ["md"]})

        self.assertEqual(201, response.status_code)
        self.table.put_item.assert_called_once()
        self.dynamodb_client.transact_write_items.assert_not_called()

    def test_submit_debits_quota_and_acquires_lock_in_one_transaction(self):
        job = {"jobId": "11111111-1111-1111-1111-111111111111"}

        self.api._submit_transaction(job, "198.51.100.8")

        request = self.dynamodb_client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(4, len(request))
        self.assertIn("quota#month#", request[0]["Update"]["Key"]["jobId"]["S"])
        self.assertIn("quota#ip#", request[1]["Update"]["Key"]["jobId"]["S"])
        self.assertEqual("control#worker-lock", request[2]["Put"]["Item"]["jobId"]["S"])
        self.assertIn("quotaConsumedAt", request[3]["Update"]["UpdateExpression"])

    def test_retry_after_worker_dispatch_failure_reuses_existing_quota_admission(self):
        job = {"jobId": "11111111-1111-1111-1111-111111111111", "quotaConsumedAt": 1}

        self.api._submit_transaction(job, "198.51.100.8")

        request = self.dynamodb_client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(2, len(request))
        self.assertEqual("control#worker-lock", request[0]["Put"]["Item"]["jobId"]["S"])
        self.assertNotIn("quotaConsumedAt", request[1]["Update"]["UpdateExpression"])

    def test_job_capability_is_required(self):
        token = "a" * 48
        job = {"accessTokenHash": self.api._job_token_hash(token)}

        self.assertIsNone(self.api._authorized_job({}, job))
        self.assertIsNone(self.api._authorized_job({"x-job-token": "b" * 48}, job))
        self.assertEqual(job, self.api._authorized_job({"x-job-token": token}, job))
