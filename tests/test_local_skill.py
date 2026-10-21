"""Observable behavior of the local skill without a learning account."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qr_login
import requests
from chaoxing import cli, courses, homework, session, student
from chaoxing.common import read_json
from chaoxing.local import RequestPolicy, RequestStopped, local_runtime, official_url


def response(url, status=200, body="", headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.request = requests.Request("GET", url).prepare()
    result._content = body.encode("utf-8")
    result._content_consumed = True
    result.encoding = "utf-8"
    result.headers.update(headers or {})
    return result


class ScopeTests(unittest.TestCase):
    def test_pending_does_not_open_any_assignment_by_default(self):
        task = {
            "name": "作业",
            "course": "课程",
            "status": "未提交",
            "remain": "",
            "taskUrl": "unused",
        }
        args = cli.build_parser().parse_args(["pending"])
        with (
            patch.object(homework, "load_session"),
            patch.object(homework, "fetch_stu_work", return_value=([task], "html")) as fetch,
            patch.object(homework, "resolve_dohomework") as resolve,
            patch.object(homework, "probe_task") as probe,
            patch.object(homework, "save_output") as save,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            homework.cmd_pending(args)
        fetch.assert_called_once()
        resolve.assert_not_called()
        probe.assert_not_called()
        result = save.call_args.args[1]
        self.assertIsNone(result[0]["questionCount"])
        self.assertFalse(result[0]["detailsResolved"])

    def test_default_todos_does_not_fetch_courses_or_activities(self):
        args = cli.build_parser().parse_args(["todos"])
        with (
            patch.object(student, "load_session"),
            patch.object(student, "fetch_stu_work", return_value=([], "")) as works,
            patch.object(student, "list_courses") as listing,
            patch.object(student, "fetch_activities") as activities,
            patch.object(student, "save_output") as export,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            student.cmd_todos(args)
        works.assert_called_once()
        listing.assert_not_called()
        activities.assert_not_called()
        self.assertIsNone(export.call_args.args[1]["activities"])
        self.assertIsNone(export.call_args.args[1]["exams"])

    def test_large_scans_require_an_explicit_scope(self):
        for argv in (
            ["works"],
            ["todos", "--deep"],
            ["todos", "--include-activities"],
            ["download", "--course", "test", "--out", "unused"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    cli.main(argv)
                self.assertEqual(stopped.exception.code, 2)

    def test_ambiguous_course_is_not_silently_selected(self):
        matches = [{"name": "课程一", "courseId": "1"}, {"name": "课程二", "courseId": "2"}]
        with patch.object(courses, "list_courses", return_value=matches):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                courses.find_course(None, "课程")
            self.assertEqual(courses.find_course(None, "2"), matches[1])

    def test_verification_stops_a_partial_scan_immediately(self):
        args = cli.build_parser().parse_args(["works", "--all-courses"])
        with (
            patch.object(homework, "load_session"),
            patch.object(homework, "list_courses", return_value=[{}, {}]),
            patch.object(homework, "course_works", side_effect=RequestStopped("verify")) as read,
        ):
            with self.assertRaises(RequestStopped):
                homework.cmd_works(args)
        self.assertEqual(read.call_count, 1)

    def test_cli_runs_from_a_different_working_directory(self):
        entry = Path(__file__).resolve().parents[1] / "xuexitong" / "scripts" / "xuexitong.py"
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, str(entry), "--help"],
                cwd=directory,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("qr-login", process.stdout)


class LocalPolicyTests(unittest.TestCase):
    def test_process_lock_is_exclusive_and_released_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with self.assertRaisesRegex(ValueError, "interrupted"):
                with local_runtime(path):
                    with self.assertRaises(RequestStopped):
                        with local_runtime(path):
                            self.fail("Concurrent access was allowed")
                    raise ValueError("interrupted")
            with local_runtime(path):
                pass

    def test_cooldown_survives_a_new_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            first = RequestPolicy(state)
            with self.assertRaises(RequestStopped):
                first.check_response(
                    response("https://i.chaoxing.com/base", 429, headers={"Retry-After": "120"})
                )
            self.assertIn("pause_until", read_json(state))
            with self.assertRaises(RequestStopped):
                RequestPolicy(state).before_request("https://i.chaoxing.com/base")

    def test_every_request_has_a_minimum_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with patch("chaoxing.local.time.time", return_value=100):
                RequestPolicy(state, interval=0).before_request("https://i.chaoxing.com/base")
            with (
                patch("chaoxing.local.time.time", return_value=100.5),
                patch("chaoxing.local.time.sleep") as sleep,
            ):
                RequestPolicy(state, interval=0).before_request("https://i.chaoxing.com/base")
            sleep.assert_called_once_with(7.5)

    def test_rate_limit_honors_seconds_and_http_dates_with_a_safe_minimum(self):
        now = 1_700_000_000.25
        for retry_after, expected in (
            ("120", 120),
            (" 120 ", 120),
            ("Tue, 14 Nov 2023 22:15:20 GMT", 120),
            ("Tue, 14 Nov 2023 22:15:20", 120),
            ("Tue, 14 Nov 2023 22:13:00 GMT", 60),
            ("0", 60),
            ("not a date", 60),
            ("²", 60),
            ("9" * 400, 60),
        ):
            with self.subTest(retry_after=retry_after):
                policy = RequestPolicy()
                with (
                    patch("chaoxing.local.time.time", return_value=now),
                    self.assertRaises(RequestStopped),
                ):
                    policy.check_response(response(
                        "https://i.chaoxing.com/base", 429,
                        headers={"Retry-After": retry_after},
                    ))
                self.assertEqual(policy.state["pause_until"], now + expected)

    def test_query_budget_blocks_transport_and_is_shared_with_next_command(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            policy = RequestPolicy(state, max_requests=2)
            with patch("chaoxing.local.time.sleep"):
                policy.before_request("https://i.chaoxing.com/base")
                policy.before_request("https://i.chaoxing.com/base")
                with self.assertRaises(RequestStopped):
                    policy.before_request("https://i.chaoxing.com/base")
                with self.assertRaises(RequestStopped):
                    RequestPolicy(state).before_request("https://i.chaoxing.com/base")
            self.assertEqual(policy.request_count, 2)
            self.assertEqual(read_json(state)["last_stop"]["reason"], "query_budget")
            self.assertEqual(read_json(state)["last_stop"]["source"], "local")

    def test_budget_still_stops_after_a_slow_request_outlasts_local_cooldown(self):
        policy = RequestPolicy(max_requests=1)
        with patch("chaoxing.local.time.time", return_value=100):
            policy.before_request("https://i.chaoxing.com/base")
        with patch("chaoxing.local.time.time", return_value=200), self.assertRaises(RequestStopped):
            policy.before_request("https://i.chaoxing.com/base")
        self.assertEqual(policy.request_count, 1)

    def test_cli_applies_query_budget_and_keeps_login_polling_paced(self):
        for argv, expected in [(["courses"], 8), (["qr-login"], None)]:
            with (
                self.subTest(argv=argv),
                patch.object(cli, "cmd_courses"),
                patch.object(cli, "cmd_qr_login"),
                patch.object(cli, "local_runtime") as runtime,
            ):
                cli.main(argv)
            self.assertEqual(runtime.call_args.kwargs["max_requests"], expected)
            self.assertEqual(runtime.call_args.args[1], 8)

    def test_pending_will_not_expand_multiple_works(self):
        args = cli.build_parser().parse_args(["pending", "--resolve"])
        tasks = [{"taskRefId": str(i), "status": "未提交"} for i in range(2)]
        with (
            patch.object(homework, "load_session"),
            patch.object(homework, "fetch_stu_work", return_value=(tasks, "")),
            patch.object(homework, "resolve_dohomework") as resolve,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                homework.cmd_pending(args)
        resolve.assert_not_called()

    def test_untrusted_urls_are_rejected(self):
        for url in (
            "https://example.com",
            "http://i.chaoxing.com",
            "https://chaoxing.com.evil.test",
            "https://user:pass@i.chaoxing.com",
            "https://i.chaoxing.com:444",
        ):
            with self.subTest(url=url), self.assertRaises(RequestStopped):
                official_url(url)
        official_url("https://mooc1-api.chaoxing.com/work")
        official_url("https://d0.cldisk.com/file")

    def test_verification_redirect_stops_before_following_it(self):
        url = "https://i.chaoxing.com/base"
        challenge = response(
            url, 302, headers={"Location": "/antispiderShowVerify.ac?token=synthetic"}
        )
        with (
            session.new_session() as client,
            patch("requests.adapters.HTTPAdapter.send", return_value=challenge) as transport,
        ):
            with self.assertRaises(RequestStopped):
                client.get(url)
        self.assertEqual(transport.call_count, 1)

    def test_external_redirect_is_blocked_before_credentials_leave(self):
        url = "https://i.chaoxing.com/base"
        redirect = response(url, 302, headers={"Location": "https://example.com/capture"})
        with (
            session.new_session() as client,
            patch("requests.adapters.HTTPAdapter.send", return_value=redirect) as transport,
        ):
            client.cookies.set("token", "synthetic-secret")
            with self.assertRaises(RequestStopped):
                client.get(url)
        self.assertEqual(transport.call_count, 1)

    def test_verification_body_stops_but_normal_login_form_is_allowed(self):
        policy = RequestPolicy()
        policy.check_response(response("https://passport2.chaoxing.com/login", body="请输入验证码"))
        with self.assertRaises(RequestStopped):
            policy.check_response(response("https://i.chaoxing.com/base", body="请完成安全验证"))

    def test_dormant_captcha_prompt_does_not_stop_a_normal_work_page(self):
        url = "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork"
        for container in (
            '<div style="z-index: 1001;display: none;">{}</div>',
            "<div hidden>{}</div>",
            '<div aria-hidden="true">{}</div>',
            '<div style="visibility: hidden">{}</div>',
            "<template>{}</template>",
        ):
            with self.subTest(container=container):
                body = "<h1>作业作答</h1>" + container.format(
                    '<div><input placeholder="请输入验证码"></div>'
                )
                policy = RequestPolicy()
                policy.check_response(response(url, body=body))
                self.assertNotIn("pause_until", policy.state)

    def test_captcha_prompt_in_script_or_comment_does_not_stop_access(self):
        body = '<script>const prompt = "请输入验证码";</script><!-- 请输入验证码 -->'
        RequestPolicy().check_response(response("https://i.chaoxing.com/base", body=body))

    def test_all_dormant_verification_markers_are_ignored(self):
        for marker in ("访问过于频繁", "操作过于频繁", "请完成安全验证", "异常访问", "拖动滑块"):
            body = (
                f'<script>const msg="{marker}";</script><!-- {marker} -->'
                f'<div style="display: none">{marker}</div><h1>作业作答</h1>'
            )
            RequestPolicy().check_response(response("https://mooc1.chaoxing.com/work", body=body))

    def test_diagnostic_records_reason_without_auth_query_or_body(self):
        policy = RequestPolicy()
        with self.assertRaises(RequestStopped):
            policy.check_response(
                response("https://i.chaoxing.com/base?token=PRIVATE", body="请完成安全验证 PRIVATE")
            )
        record = policy.state["last_stop"]
        self.assertEqual(record["reason"], "visible_verification_prompt")
        self.assertEqual(record["source"], "page_heuristic")
        self.assertNotIn("PRIVATE", str(policy.state))

    def test_known_base_redirect_is_upgraded_before_transport(self):
        url = "https://i.chaoxing.com/base"
        redirect = response(url, 302, headers={"Location": "http://i.chaoxing.com/base?x=1&x=2"})
        final = response("https://i.chaoxing.com/base?x=1&x=2", body="uid=synthetic")
        with (
            session.new_session() as client,
            patch("chaoxing.local.time.sleep"),
            patch("requests.adapters.HTTPAdapter.send", side_effect=[redirect, final]) as send,
        ):
            actual = client.get(url)
        self.assertEqual(actual.status_code, 200)
        self.assertEqual(send.call_args_list[1].args[0].url, "https://i.chaoxing.com/base?x=1&x=2")

    def test_other_http_downgrades_remain_blocked(self):
        for target in (
            "http://other.chaoxing.com/base",
            "http://i.chaoxing.com/other",
            "http://user:pass@i.chaoxing.com/base",
            "http://i.chaoxing.com:80/base",
        ):
            url = "https://i.chaoxing.com/base"
            redirect = response(url, 302, headers={"Location": target})
            with (
                self.subTest(target=target),
                session.new_session() as client,
                patch("requests.adapters.HTTPAdapter.send", return_value=redirect) as send,
            ):
                with self.assertRaises(RequestStopped):
                    client.get(url)
            self.assertEqual(send.call_count, 1)

    def test_visible_captcha_prompt_still_stops_access(self):
        for body in (
            '<input placeholder="请输入验证码">',
            "<p>请输入<span>验证码</span></p>",
            '<div hidden><input placeholder="请输入验证码"></div><p>请输入验证码</p>',
        ):
            with self.subTest(body=body), self.assertRaises(RequestStopped):
                RequestPolicy().check_response(response("https://i.chaoxing.com/base", body=body))

    def test_post_failure_is_not_replayed(self):
        with (
            session.new_session() as client,
            patch(
                "requests.adapters.HTTPAdapter.send", side_effect=requests.Timeout("uncertain")
            ) as send,
        ):
            with self.assertRaises(requests.Timeout):
                client.post("https://mooc1-api.chaoxing.com/work", data={"answer": "A"})
        self.assertEqual(send.call_count, 1)

    def test_post_redirect_does_not_resend_the_submission(self):
        url = "https://mooc1-api.chaoxing.com/work"
        redirect = response(url, 307, headers={"Location": "/other-submit-endpoint"})
        with (
            session.new_session() as client,
            patch("requests.adapters.HTTPAdapter.send", return_value=redirect) as transport,
        ):
            result = client.post(url, data={"answer": "A"})
        self.assertEqual(result.status_code, 307)
        self.assertEqual(transport.call_count, 1)


class LoginTests(unittest.TestCase):
    def test_cancel_does_not_refresh_qr(self):
        args = SimpleNamespace(timeout=150, session="unused")
        with (
            patch.object(qr_login.cx, "new_session"),
            patch.object(qr_login, "new_qr", return_value=("u", "e", "0", "0")) as new_qr,
            patch.object(qr_login, "poll", return_value={"type": 6}),
            patch.object(qr_login.time, "sleep"),
            patch.object(qr_login, "write_status") as status,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(qr_login.run_login(args), 1)
        new_qr.assert_called_once()
        status.assert_called_with(state="cancelled")


if __name__ == "__main__":
    unittest.main()
