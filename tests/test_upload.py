"""Uploads stop before writes on invalid context, and preserve recovery records."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from chaoxing import cli, homework
from chaoxing.common import read_json

PC_URL = "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=10&classId=20&workId=30"
PC_PAGE = (
    '<input id="userId" value="1"><input id="uploadEnc" value="PRIVATE">'
    '<input id="uploadTimeStamp" value="1700000000000">'
    '<textarea name="answer12"></textarea>'
    "<script>window.UEDITOR_CONFIG.fileUrl = ServerHost.uploadDomain + "
    '"/ueditorupload/attachment?uploadtype=work";</script>'
)
MOBILE_PAGE = (
    '<input id="uploadUid" value="1"><input id="uploadEnc" value="PRIVATE">'
    '<input id="currentTime" value="1700000000000">'
)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = self.root / "实验.zip"
        self.file.write_bytes(b"example-file")
        self.client = Mock()
        self.client.get.return_value = SimpleNamespace(url=PC_URL, text=PC_PAGE)
        self.response = {
            "state": "SUCCESS",
            "original": self.file.name,
            "size": self.file.stat().st_size,
            "fileType": ".zip",
            "url": "/read?objectId=synthetic-id&other=value",
        }
        self.client.post.return_value.json.return_value = self.response
        self.enterContext(
            patch(
                "requests.adapters.HTTPAdapter.send",
                side_effect=AssertionError("Network forbidden"),
            )
        )

    def test_pc_upload_uses_observed_form_once_and_parses_object_id(self):
        result, name, ext = homework.upload_pc_raw(self.client, PC_URL, str(self.file))
        self.assertEqual((result["objectId"], name, ext), ("synthetic-id", self.file.name, "zip"))
        self.client.post.assert_called_once()
        call = self.client.post.call_args
        self.assertIn("/ueditorupload/attachment?", call.args[0])
        self.assertEqual(set(call.kwargs["files"]), {"upfile"})
        self.assertEqual(call.kwargs["timeout"], (30, 180))

    def test_missing_empty_or_directory_attachment_never_fetches_page(self):
        empty = self.root / "empty.zip"
        empty.touch()
        for path in (self.root / "missing", empty, self.root):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                homework.upload_pc_raw(self.client, PC_URL, str(path))
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_context_and_identity_mismatch_stop_before_upload(self):
        for url, page in (
            (PC_URL.replace("workId=30", "workId=99"), PC_PAGE),
            (PC_URL, PC_PAGE.replace('id="uploadEnc"', 'id="changed"')),
            (PC_URL, PC_PAGE.replace("/ueditorupload/attachment", "/unrecognized")),
        ):
            with self.subTest(url=url, page=page):
                self.client.get.return_value = SimpleNamespace(url=url, text=page)
                with self.assertRaises(homework.UploadError) as caught:
                    homework.upload_pc_raw(self.client, PC_URL, str(self.file))
                self.assertFalse(caught.exception.upload_uncertain)
                self.assertNotIn("PRIVATE", str(caught.exception))
        self.client.post.assert_not_called()

    def test_mobile_missing_signed_timestamp_does_not_invent_one(self):
        with patch.object(
            homework, "fetch_index", return_value=MOBILE_PAGE.replace("currentTime", "missing")
        ):
            with self.assertRaises(homework.UploadError):
                homework.upload_raw(
                    self.client, "https://mooc1-api.chaoxing.com/work", str(self.file)
                )
        self.client.post.assert_not_called()

    def test_pc_file_over_official_size_limit_stops_before_page_request(self):
        with patch.object(homework.os.path, "getsize", return_value=500 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                homework.upload_pc_raw(self.client, PC_URL, str(self.file))
        self.client.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_invalid_server_responses_are_uncertain_and_never_retried(self):
        for response in (["PRIVATE"], {"state": "SUCCESS"}, {**self.response, "size": 1}):
            with self.subTest(response=response):
                self.client.post.reset_mock()
                self.client.post.return_value.json.return_value = response
                with self.assertRaises(homework.UploadError) as caught:
                    homework.upload_pc_raw(self.client, PC_URL, str(self.file))
                self.assertTrue(caught.exception.upload_uncertain)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.client.post.assert_called_once()

    def test_non_json_does_not_echo_response_body(self):
        self.client.post.return_value.text = "PRIVATE_HTML"
        self.client.post.return_value.json.side_effect = ValueError("PRIVATE_HTML")
        with self.assertRaises(homework.UploadError) as caught:
            homework.upload_pc_raw(self.client, PC_URL, str(self.file))
        self.assertTrue(caught.exception.upload_uncertain)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_success_record_can_be_reused_and_existing_record_is_never_overwritten(self):
        out = self.root / "upload.json"
        args = cli.build_parser().parse_args(
            ["upload", "--pc-work-url", PC_URL, "--file", str(self.file), "--out", str(out)]
        )
        with (
            patch.object(homework, "load_session", return_value=self.client),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            homework.cmd_upload(args)
            with self.assertRaises(ValueError):
                homework.cmd_upload(args)
        result = read_json(out)
        self.assertTrue(result["status"])
        self.assertEqual(result["objectId"], "synthetic-id")
        self.assertIn("iframe", result["html"])
        self.assertNotIn("objectId", output.getvalue())
        self.client.post.assert_called_once()

    def test_timeout_record_is_private_safe_and_stops_after_one_post(self):
        out = self.root / "failure.json"
        args = cli.build_parser().parse_args(
            ["upload", "--pc-work-url", PC_URL, "--file", str(self.file), "--out", str(out)]
        )
        self.client.post.side_effect = requests.ReadTimeout("https://example.test/?token=PRIVATE")
        with patch.object(homework, "load_session", return_value=self.client):
            with self.assertRaises(requests.ReadTimeout):
                homework.cmd_upload(args)
        result = read_json(out)
        self.assertTrue(result["uncertain"])
        self.assertIsNone(result["status"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.client.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
