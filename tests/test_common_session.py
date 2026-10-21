"""Offline coverage for parsing, persistence, and HTTP session contracts."""

import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from chaoxing import common, session


class InputParsingTests(unittest.TestCase):
    def test_attribute_order_case_unquoted_and_entities(self):
        source = (
            "<INPUT VALUE=123 ID=questionId TYPE=HIDDEN>"
            '<input value="a&amp;b&quot;c" name=enc type=hidden>'
            "<input type=hidden name=empty>"
            "<input type=text name=typed value=ignored>"
        )
        self.assertEqual(common._question_id(source), "123")
        self.assertEqual(common.extract_hidden_fields(source), {"enc": 'a&b"c', "empty": ""})
        self.assertEqual(common.hidden_fields_by_id(source), {"questionId": "123"})
        self.assertEqual(common.hidden_by_id(source, "missing"), "")

    def test_rich_html_is_not_truncated_or_decoded_twice(self):
        source = (
            '<input type="hidden" name="answer1" '
            "value='<p><br></p><div class=\"x\">a &amp;lt; b</div>'>"
        )
        self.assertEqual(
            common.extract_hidden_fields(source)["answer1"],
            '<p><br></p><div class="x">a &lt; b</div>',
        )
        self.assertEqual(list(common._iter_input_tags(source)), [source])

    def test_comments_script_and_self_closing_inputs(self):
        source = (
            "<!-- <input type=hidden name=comment value=ignored> -->"
            '<script>const x = "<input type=hidden name=script value=ignored>";</script>'
            '<input name="valid" value="yes" type="hidden"/>'
        )
        self.assertEqual(common.extract_hidden_fields(source), {"valid": "yes"})
        self.assertEqual(len(common.parse_inputs(source)), 1)

    def test_question_id_requires_digits_but_not_type_attribute(self):
        self.assertEqual(common._question_id('<input value="42" id="questionId">'), "42")
        self.assertIsNone(common._question_id('<input value="unknown" id="questionId">'))


class AtomicOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "output.json"

    def test_json_roundtrip(self):
        data = {"text": "中文", "values": [1, True, None]}
        common.write_json(self.path, data)
        self.assertEqual(common.read_json(self.path), data)
        self.assertIn("中文", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_serialization_failure_preserves_existing_file(self):
        self.path.write_text("original", encoding="utf-8")
        with self.assertRaises(TypeError):
            common.write_json(self.path, {"cannot_serialize": object()})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_replace_failure_preserves_existing_file_and_cleans_temporary(self):
        self.path.write_text("original", encoding="utf-8")
        with (
            patch.object(common.os, "replace", side_effect=OSError("blocked")),
            self.assertRaises(OSError),
        ):
            common.write_text(self.path, "replacement")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_optional_output_and_existing_confirmation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            common.save_output("", {"empty": True})
            common.save_output(self.path, {"saved": True})
        self.assertEqual(output.getvalue(), f"[+] -> {self.path}\n")
        self.assertEqual(common.read_json(self.path), {"saved": True})


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "cookies.json"

    def test_timeout_status_check_and_no_post_retry(self):
        response = Mock()
        with patch.object(requests.Session, "request", return_value=response) as request:
            s = session.new_session()
            self.addCleanup(s.close)
            self.assertIs(s.get("https://offline.invalid/"), response)
            self.assertEqual(request.call_args.kwargs["timeout"], 30)
            response.raise_for_status.assert_called_once_with()
            s.get("https://offline.invalid/", timeout=(1, 2))
            self.assertEqual(request.call_args.kwargs["timeout"], (1, 2))
            response.raise_for_status.side_effect = requests.HTTPError("failed")
            request.reset_mock()
            with self.assertRaises(requests.HTTPError):
                s.post("https://offline.invalid/", data={"answer": "A"})
            self.assertEqual(request.call_count, 1)
            response.close.assert_called_once_with()

    def test_new_session_headers_and_cookie_header_compatibility(self):
        s = session.new_session("fid=9; _uid=123; path=/; secure; Domain=.example.com")
        self.addCleanup(s.close)
        self.assertEqual(session.cookie_get(s, "fid"), "9")
        self.assertEqual(s.headers["User-Agent"], session.UA)
        self.assertEqual({c.name for c in s.cookies}, {"fid", "_uid"})

    def test_cookie_roundtrip_preserves_scope_secure_and_expiry(self):
        source = session.new_session()
        self.addCleanup(source.close)
        source.cookies.set(
            "token",
            "parent",
            domain=".chaoxing.com",
            path="/",
            secure=True,
            expires=2000000000,
        )
        source.cookies.set(
            "token",
            "child",
            domain="mooc1.chaoxing.com",
            path="/work",
            secure=False,
            expires=2100000000,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            session.save_session(source, self.path)
        restored = session.load_session(self.path)
        self.addCleanup(restored.close)

        def metadata(s):
            return [(c.name, c.value, c.domain, c.path, c.secure, c.expires) for c in s.cookies]

        self.assertEqual(metadata(restored), metadata(source))
        self.assertEqual(session.cookie_get(restored, "token"), "parent")

    def test_detail_wins_without_inventing_parent_cookie(self):
        common.write_json(
            self.path,
            {
                "cookies": {"token": "stale", "legacy": "fallback"},
                "detail": [
                    {
                        "name": "token",
                        "value": "correct",
                        "domain": "passport2.chaoxing.com",
                        "path": "/login",
                        "secure": True,
                        "expires": 2000000000,
                    }
                ],
            },
        )
        s = session.load_session(self.path)
        self.addCleanup(s.close)
        tokens = [c for c in s.cookies if c.name == "token"]
        self.assertEqual(len(tokens), 1)
        self.assertEqual((tokens[0].value, tokens[0].domain), ("correct", "passport2.chaoxing.com"))
        self.assertEqual(session.cookie_get(s, "legacy"), "fallback")

    def test_flattened_and_metadata_free_formats_remain_supported(self):
        for data in ({"fid": "9"}, {"cookies": {"fid": "9"}}):
            with self.subTest(data=data):
                common.write_json(self.path, data)
                s = session.load_session(self.path)
                self.addCleanup(s.close)
                self.assertEqual(session.cookie_get(s, "fid"), "9")
                self.assertEqual(next(iter(s.cookies)).domain, ".chaoxing.com")

    def test_empty_cookie_domain_is_preserved(self):
        common.write_json(
            self.path,
            {
                "cookies": {"token": "x"},
                "detail": [{"name": "token", "value": "x", "domain": "", "path": "/"}],
            },
        )
        s = session.load_session(self.path)
        self.addCleanup(s.close)
        self.assertEqual(next(iter(s.cookies)).domain, "")

    def test_jwt_expiry_handles_bad_payloads(self):
        for payload in ([], {"exp": "not-a-time"}, {"exp": True}):
            token_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            self.assertIsNone(session.jwt_expiry(f"header.{token_payload}.signature"))
        self.assertIsNone(session.jwt_expiry("header.!.signature"))
        self.assertIsNone(session.jwt_expiry(None))

    def test_jwt_expiry_rejects_nonfinite_and_unrepresentable_timestamps(self):
        for expiry in (float("nan"), float("inf"), -float("inf"), 1e300, 10**1000):
            with self.subTest(expiry=expiry):
                payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode()
                self.assertIsNone(session.jwt_expiry(f"header.{payload}.signature"))
        for expiry in (1_700_000_000, 1_700_000_000.5):
            payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode()
            self.assertEqual(session.jwt_expiry(f"header.{payload}.signature"), expiry)

    def test_jwt_expiry_handles_platform_localtime_errors(self):
        payload = base64.urlsafe_b64encode(b'{"exp":1700000000}').decode()
        for error in (OverflowError, ValueError, OSError):
            with self.subTest(error=error), patch.object(session.time, "localtime", side_effect=error):
                self.assertIsNone(session.jwt_expiry(f"header.{payload}.signature"))

    def test_session_save_and_info_tolerate_unrepresentable_jwt_expiry(self):
        payload = base64.urlsafe_b64encode(b'{"exp":1e300}').decode()
        token = f"header.{payload}.signature"
        client = session.new_session()
        self.addCleanup(client.close)
        client.cookies.set("p_auth_token", token, domain=".chaoxing.com", path="/")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            session.save_session(client, self.path)
            session.cmd_session_info(SimpleNamespace(session=self.path))
        self.assertNotIn(token, output.getvalue())

    def test_check_uses_uid_and_never_logs_cookie_values(self):
        s = SimpleNamespace(
            get=Mock(return_value=SimpleNamespace(text="uid=123 我的课程")),
            cookies=requests.cookies.RequestsCookieJar(),
        )
        s.cookies.set("_uid", "123")
        s.cookies.set("secret", "sensitive-value")
        with (
            patch.object(session, "load_session", return_value=s),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            session.cmd_check(SimpleNamespace(session="unused"))
        result = json.loads(output.getvalue())
        self.assertTrue(result["loggedIn"])
        self.assertNotIn("sensitive-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
