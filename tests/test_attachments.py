"""Synthetic homework attachment flow and failure handling, with no real account."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from chaoxing import homework, materials
from chaoxing.common import read_json, write_json
from chaoxing.local import RequestStopped


class AttachmentTests(unittest.TestCase):
    def test_multiple_attachments_require_an_explicit_selection_before_network(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work.json"
            write_json(
                work,
                {
                    "questions": [
                        {
                            "questionId": "12",
                            "attachments": [
                                {"objectId": "one", "name": "a.docx"},
                                {"objectId": "two", "name": "b.docx"},
                            ],
                        }
                    ]
                },
            )
            args = SimpleNamespace(
                work_json=str(work),
                question_id="",
                object_id="",
                out=d,
                result_json="",
                session="unused",
                all_files=False,
            )
            with (
                patch.object(homework, "load_session") as load,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                homework.cmd_download_attachments(args)
            load.assert_not_called()

    def test_download_link_comes_from_matching_official_preview_anchor(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(
            url="https://mooc1.chaoxing.com/mooc-ans/ueditorupload/read",
            text='<a href="https://d0.cldisk.com/download/object?token=synthetic&amp;v=1">下载</a>'
            '<a href="https://d0.cldisk.com/download/other">其他</a>',
        )
        actual = homework.attachment_download_url(
            client, {"objectId": "object", "name": "实验.docx"}
        )
        self.assertEqual(actual, "https://d0.cldisk.com/download/object?token=synthetic&v=1")
        query = client.get.call_args.args[0]
        self.assertIn("/mooc-ans/ueditorupload/read?", query)
        self.assertNotIn("/ananas/status/", query)

    def test_no_download_link_does_not_probe_alternative_endpoints(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(
            url="https://mooc1.chaoxing.com/mooc-ans/ueditorupload/read", text="不允许下载"
        )
        with self.assertRaises(ValueError):
            homework.attachment_download_url(client, {"objectId": "obj", "name": "a.docx"})
        self.assertEqual(client.get.call_count, 1)

    def test_external_download_anchor_is_rejected(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(
            url="https://mooc1.chaoxing.com/mooc-ans/ueditorupload/read",
            text='<a href="https://evil.invalid/download/obj">下载</a>',
        )
        with self.assertRaises(RequestStopped):
            homework.attachment_download_url(client, {"objectId": "obj", "name": "a.docx"})

    def test_atomic_download_rejects_html_empty_and_truncated_bodies(self):
        for headers, chunks in [
            ({"Content-Type": "text/html"}, [b"error"]),
            ({}, []),
            ({"Content-Length": "100"}, [b"partial"]),
        ]:
            with self.subTest(headers=headers, chunks=chunks), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "prior.docx"
                path.write_bytes(b"prior")
                response = Mock(status_code=200, headers=headers)
                response.iter_content.return_value = chunks
                client = Mock()
                client.get.return_value = response
                with self.assertRaises(RuntimeError):
                    materials.download_file(client, "https://d0.cldisk.com/download/obj", path)
                self.assertEqual(path.read_bytes(), b"prior")
                self.assertEqual(list(Path(d).iterdir()), [path])
                response.close.assert_called_once()

    def test_successful_atomic_download_and_referer(self):
        with tempfile.TemporaryDirectory() as d:
            response = Mock(status_code=200, headers={"Content-Length": "7"})
            response.iter_content.return_value = [b"content"]
            client = Mock()
            client.get.return_value = response
            path = Path(d) / "new.docx"
            self.assertEqual(materials.download_file(client, "https://d0.cldisk.com/file", path), 7)
            self.assertEqual(path.read_bytes(), b"content")
            self.assertEqual(
                client.get.call_args.kwargs["headers"]["Referer"], "https://pan-yz.chaoxing.com/"
            )

    def test_explicit_json_attachment_is_not_mistaken_for_an_error_response(self):
        with tempfile.TemporaryDirectory() as d:
            response = Mock(
                status_code=200,
                headers={
                    "Content-Type": "application/json",
                    "Content-Disposition": 'attachment; filename="data.json"',
                },
            )
            response.iter_content.return_value = [b'{"data": [1]}']
            client = Mock()
            client.get.return_value = response
            path = Path(d) / "data.json"
            materials.download_file(client, "https://d0.cldisk.com/file", path)
            self.assertEqual(read_json(path), {"data": [1]})

    def test_attachment_manifest_preserves_partial_failures_without_signed_urls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            work = root / "work.json"
            manifest = root / "result.json"
            write_json(
                work,
                {
                    "questions": [
                        {
                            "questionId": "12",
                            "attachments": [
                                {"objectId": "one", "name": "../first.docx"},
                                {"objectId": "two", "name": "second.docx"},
                            ],
                        }
                    ]
                },
            )
            args = SimpleNamespace(
                work_json=str(work),
                all_files=True,
                question_id="",
                object_id="",
                out=str(root / "files"),
                result_json=str(manifest),
                session="unused",
            )
            with (
                patch.object(homework, "load_session"),
                patch.object(homework, "attachment_download_url", return_value="unused"),
                patch.object(
                    homework,
                    "download_file",
                    side_effect=[
                        7,
                        requests.Timeout("https://d0.cldisk.com/download/two?token=PRIVATE"),
                    ],
                ) as download,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                homework.cmd_download_attachments(args)
            data = read_json(manifest)
            self.assertEqual((data["success"], data["failed"], data["total"]), (1, 1, 2))
            self.assertEqual(Path(download.call_args_list[0].args[2]).parent, root / "files")
            self.assertNotIn("PRIVATE", manifest.read_text(encoding="utf-8") + output.getvalue())

    def test_access_stop_saves_manifest_and_does_not_fetch_next_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work.json"
            manifest = Path(d) / "result.json"
            write_json(
                work,
                {
                    "questions": [
                        {
                            "questionId": "12",
                            "attachments": [
                                {"objectId": "one", "name": "a.docx"},
                                {"objectId": "two", "name": "b.docx"},
                            ],
                        }
                    ]
                },
            )
            args = SimpleNamespace(
                work_json=str(work),
                all_files=True,
                question_id="",
                object_id="",
                out=d,
                result_json=str(manifest),
                session="unused",
            )
            with (
                patch.object(homework, "load_session"),
                patch.object(
                    homework, "attachment_download_url", side_effect=RequestStopped("verify")
                ) as resolve,
            ):
                with self.assertRaises(RequestStopped):
                    homework.cmd_download_attachments(args)
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(read_json(manifest)["total"], 2)
            self.assertEqual(read_json(manifest)["skipped"], 1)
            self.assertFalse(read_json(manifest)["files"][0]["success"])


if __name__ == "__main__":
    unittest.main()
