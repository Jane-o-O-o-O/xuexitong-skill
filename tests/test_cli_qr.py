import io
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from unittest.mock import patch

import chaoxing_hw
import qr_login
import requests
from chaoxing import cli, homework, session


class CliTests(unittest.TestCase):
    def test_all_legacy_commands_parse_and_have_handlers(self):
        required = {
            "login": ["--phone", "13800000000", "--password", "test"],
            "set-cookie": ["--cookie", "fid=test"],
            "check": [],
            "session-info": [],
            "courses": [],
            "works": [],
            "wrongque": ["--course", "test"],
            "study": ["--course", "test"],
            "notices": [],
            "schedule": [],
            "todos": [],
            "download": ["--course", "test", "--out", "downloads"],
            "exams": ["--course", "test"],
            "topics": ["--course", "test"],
            "course": ["--course", "test"],
            "chapters": ["--course", "test"],
            "materials": ["--course", "test"],
            "pending": [],
            "fetch": ["--dohomework-url", "https://example.test/work"],
            "template": ["--work-json", "work.json"],
            "upload": ["--dohomework-url", "https://example.test/work", "--file", "test.txt"],
            "verify": ["--dohomework-url", "https://example.test/work"],
            "save": ["--dohomework-url", "https://example.test/work", "--answers", "answers.json"],
            "submit": [
                "--dohomework-url",
                "https://example.test/work",
                "--answers",
                "answers.json",
            ],
        }
        parser = cli.build_parser()
        self.assertEqual(len(required), 24)
        for name, arguments in required.items():
            with self.subTest(command=name):
                args = parser.parse_args([name, *arguments])
                self.assertEqual(args.cmd, name)
                self.assertTrue(callable(args.handler))
                self.assertEqual(args.session, session.SESSION_FILE)
                self.assertEqual(args.sleep, 8)
        self.assertTrue(parser.parse_args(["save", *required["save"]]).temp_save)
        self.assertFalse(parser.parse_args(["submit", *required["submit"]]).temp_save)

    def test_legacy_exports_refer_to_services(self):
        self.assertIs(chaoxing_hw.do_save_or_submit, homework.do_save_or_submit)
        self.assertIs(chaoxing_hw.save_session, session.save_session)
        self.assertIs(chaoxing_hw.main, cli.main)

    def test_numeric_limits_reject_invalid_values(self):
        with redirect_stderr(io.StringIO()):
            for arguments in (
                ["--sleep", "-1", "courses"],
                ["--sleep", "nan", "courses"],
                ["pending", "--max-index", "0"],
                ["schedule", "--week", "0"],
                ["materials", "--course", "test", "--max-depth", "-1"],
                ["fetch", "--dohomework-url", "unused", "--start-index", "-1"],
            ):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                    cli.build_parser().parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_dispatch_and_user_facing_errors(self):
        with patch.object(cli, "cmd_courses") as handler:
            self.assertEqual(cli.main(["courses"]), 0)
            handler.assert_called_once()
        with (
            patch.object(cli, "cmd_courses", side_effect=OSError("missing")),
            redirect_stderr(io.StringIO()) as output,
            self.assertRaises(SystemExit) as error,
        ):
            cli.main(["courses"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("OSError: missing", output.getvalue())

    def test_no_command_shows_help(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([]), 0)
        self.assertIn("pending", output.getvalue())

    def test_defaults_read_one_page_and_few_notices(self):
        parser = cli.build_parser()
        fetch = parser.parse_args(["fetch", "--dohomework-url", "unused"])
        self.assertEqual((fetch.start_index, fetch.max_index), (0, 1))
        self.assertEqual(parser.parse_args(["pending"]).max_index, 1)
        self.assertEqual(parser.parse_args(["notices"]).limit, 5)


class QrTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(qr_login, "local_runtime", return_value=nullcontext()))
        self.enterContext(
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Network is forbidden"),
            )
        )

    def test_shared_input_parser_accepts_reversed_attributes(self):
        self.assertEqual(qr_login.hidden_value('<INPUT value="E&amp;X" id=enc>', "enc"), "E&X")

    def test_success_saves_session_without_printing_cookie_values(self):
        client = requests.Session()
        client.cookies.set("_uid", "1001")
        client.cookies.set("vc3", "SECRET_COOKIE")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(qr_login.cx, "new_session", return_value=client),
            patch.object(qr_login, "new_qr", return_value=("u", "e", "0", "0")),
            patch.object(qr_login, "poll", return_value={"status": True}),
            patch.object(qr_login.cx, "save_session") as save,
            patch.object(qr_login, "write_status"),
            patch.object(qr_login.time, "sleep"),
            redirect_stdout(io.StringIO()) as output,
        ):
            path = directory + "/session.json"
            self.assertEqual(qr_login.main(["--session", path]), 0)
        save.assert_called_once_with(client, path)
        self.assertNotIn("SECRET_COOKIE", output.getvalue())
        self.assertIn('"session":', output.getvalue())

    def test_timeout_does_not_poll_after_deadline(self):
        with (
            patch.object(qr_login.cx, "new_session"),
            patch.object(qr_login, "new_qr", return_value=("u", "e", "0", "0")),
            patch.object(qr_login, "poll") as poll,
            patch.object(qr_login.time, "monotonic", side_effect=[0, 0, 2, 2]),
            patch.object(qr_login, "write_status") as status,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(qr_login.main(["--timeout", "1"]), 1)
        poll.assert_not_called()
        status.assert_called_with(state="timeout")

    def test_invalid_timeout_is_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            qr_login.main(["--timeout", "0"])

    def test_request_failure_sets_error_status(self):
        with (
            patch.object(qr_login.cx, "new_session"),
            patch.object(qr_login, "new_qr", side_effect=requests.HTTPError("HTTP 500")),
            patch.object(qr_login, "write_status") as status,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(qr_login.main([]), 1)
        self.assertEqual(status.call_args.kwargs["state"], "error")
        self.assertIn("HTTP 500", status.call_args.kwargs["error"])

    def test_auth_response_must_be_an_object(self):
        client = unittest.mock.Mock()
        client.post.return_value.json.return_value = []
        with self.assertRaises(RuntimeError):
            qr_login.poll(client, "u", "e", "0", "0")


if __name__ == "__main__":
    unittest.main()
