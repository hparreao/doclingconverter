import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


class LambdaWorkerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s3 = MagicMock()
        cls.table = MagicMock()
        resource = MagicMock()
        resource.Table.return_value = cls.table

        cls.environment = patch.dict(
            os.environ,
            {"JOBS_TABLE": "test-jobs", "ARTIFACTS_BUCKET": "test-artifacts"},
            clear=False,
        )
        cls.boto_client = patch("boto3.client", return_value=cls.s3)
        cls.boto_resource = patch("boto3.resource", return_value=resource)
        cls.environment.start()
        cls.boto_client.start()
        cls.boto_resource.start()
        sys.modules.pop("lambda_app.worker", None)
        cls.worker = importlib.import_module("lambda_app.worker")

    @classmethod
    def tearDownClass(cls):
        cls.boto_resource.stop()
        cls.boto_client.stop()
        cls.environment.stop()
        sys.modules.pop("lambda_app.worker", None)

    def setUp(self):
        self.s3.reset_mock()
        self.table.reset_mock()
        self.table.update_item.side_effect = None
        self.table.get_item.return_value = {
            "Item": {
                "jobId": "11111111-1111-1111-1111-111111111111",
                "filename": "sample.pdf",
                "inputKey": "uploads/job/sample.pdf",
                "outputFormats": ["md"],
            }
        }
        self.s3.head_object.return_value = {"ContentLength": 42}

    def test_worker_stores_result_then_removes_source_and_lock(self):
        with patch.object(self.worker, "_convert", return_value={"documents": []}) as convert:
            self.worker.handler({"jobId": "11111111-1111-1111-1111-111111111111"}, None)

        convert.assert_called_once()
        self.s3.put_object.assert_called_once()
        self.s3.delete_object.assert_called_once_with(Bucket="test-artifacts", Key="uploads/job/sample.pdf")
        self.table.delete_item.assert_called_once()
        updates = [call.kwargs["UpdateExpression"] for call in self.table.update_item.call_args_list]
        self.assertIn("SET #status = :running, startedAt = :started", updates)
        self.assertIn("SET #status = :completed, resultKey = :result, finishedAt = :finished", updates)

    def test_worker_marks_a_conversion_failure_without_serializing_exception_text(self):
        with patch.object(self.worker, "_convert", side_effect=ValueError("secret document detail")):
            self.worker.handler({"jobId": "11111111-1111-1111-1111-111111111111"}, None)

        updates = [call.kwargs for call in self.table.update_item.call_args_list]
        failure = next(call for call in updates if call["ExpressionAttributeValues"].get(":failed") == "failed")
        self.assertEqual("convert", failure["ExpressionAttributeValues"][":stage"])
        self.assertNotIn("secret document detail", repr(failure))

    def test_worker_ignores_an_event_that_was_not_submitted(self):
        self.table.update_item.side_effect = self.worker.ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )

        self.worker.handler({"jobId": "11111111-1111-1111-1111-111111111111"}, None)

        self.table.get_item.assert_not_called()
        self.s3.head_object.assert_not_called()
