import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from chaoxing import courses, homework
from chaoxing.common import read_json, write_json


def page(qid, enc="E"):
    return (
        f'<input value="{qid}" id="questionId">'
        f'<input name="enc" type="hidden" value="{enc}">'
        f'<input name="type{qid}" type="hidden" value="1">'
        f'<input name="score{qid}" type="hidden" value="5">'
        f'<input name="answer{qid}" type="hidden" value="">'
    )


class HomeworkSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Network is forbidden"),
            )
        )
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        self.args = argparse.Namespace(
            session=str(root / "unused-session.json"),
            dohomework_url="https://example.test/work",
            answers=str(root / "answers.json"),
            work_json=str(root / "work.json"),
            brute_max=10,
            confirm=True,
            out=str(root / "result.json"),
            sleep=0,
        )
        write_json(self.args.answers, {"7001": "A", "7002": "plain:a < b & c", "7003": "AB"})
        write_json(
            self.args.work_json,
            {"questions": [{"questionId": qid} for qid in ("7001", "7002", "7003")]},
        )
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(homework, "load_session", return_value=object()))
        self.pages = [page("7001", "E1"), page("7002", "E2"), page("7003", "E3")]
        self.fetch = self.enterContext(
            patch.object(
                homework,
                "fetch_index",
                side_effect=lambda s, url, index: (
                    self.pages[index] if index < len(self.pages) else "<html></html>"
                ),
            )
        )
        self.post = self.enterContext(
            patch.object(homework, "post_answer", return_value={"status": True, "msg": "ok"})
        )

    def assert_payloads(self, temp_save):
        self.assertEqual(self.post.call_count, 3)
        for i, call in enumerate(self.post.call_args_list, 1):
            _, payload, temp = call.args
            qid = str(7000 + i)
            self.assertEqual(temp, temp_save)
            self.assertEqual(payload["tempSave"], "true" if temp_save else "false")
            self.assertEqual(payload["enc"], f"E{i}")
            self.assertIn(f"type{qid}", payload)
            self.assertEqual([key for key in payload if key.startswith("answer")], [f"answer{qid}"])
        self.assertEqual(
            self.post.call_args_list[1].args[1]["answer7002"], "<p>a &lt; b &amp; c</p>"
        )

    def test_submit_with_cached_work_uses_per_question_fields(self):
        homework.do_save_or_submit(self.args, False)
        self.assert_payloads(False)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(set(read_json(self.args.out)), {"7001", "7002", "7003"})

    def test_submit_without_work_scans_pages(self):
        self.args.work_json = ""
        homework.do_save_or_submit(self.args, False)
        self.assert_payloads(False)

    def test_save_does_not_require_confirm(self):
        self.args.confirm = False
        homework.do_save_or_submit(self.args, True)
        self.assert_payloads(True)

    def test_submit_without_confirm_stops_before_loading_or_posting(self):
        self.args.confirm = False
        with self.assertRaises(SystemExit) as error:
            homework.do_save_or_submit(self.args, False)
        self.assertEqual(error.exception.code, 2)
        self.fetch.assert_not_called()
        self.post.assert_not_called()

    def test_reordered_server_pages_use_actual_ids(self):
        self.pages = [page("7003", "E3"), page("7001", "E1"), page("7002", "E2")]
        homework.do_save_or_submit(self.args, False)
        self.assert_payloads(False)
        self.assertEqual(self.fetch.call_count, 3)

    def test_fallback_reuses_hint_pages_and_does_not_bind_unknown_ids(self):
        self.pages = [page("9999"), page("7003", "E3"), page("7001", "E1"), page("7002", "E2")]
        homework.do_save_or_submit(self.args, False)
        self.assert_payloads(False)
        self.assertEqual(self.fetch.call_count, 4)

    def test_missing_question_prevents_all_submissions(self):
        self.pages = [page("7001", "E1"), "<html>expired</html>"]
        with self.assertRaises(SystemExit):
            homework.do_save_or_submit(self.args, False)
        self.post.assert_not_called()

    def test_failed_response_stops_and_records_partial_results(self):
        self.post.side_effect = [{"status": True}, {"status": False, "msg": "rejected"}]
        with self.assertRaises(SystemExit):
            homework.do_save_or_submit(self.args, False)
        self.assertEqual(self.post.call_count, 2)
        self.assertFalse(read_json(self.args.out)["7002"]["status"])

    def test_invalid_answers_fail_before_fetch(self):
        for value in ([], {}, {"7001": ["A", "B"]}):
            with self.subTest(value=value):
                write_json(self.args.answers, value)
                with self.assertRaises(SystemExit):
                    homework.do_save_or_submit(self.args, False)
        self.fetch.assert_not_called()
        self.post.assert_not_called()

    def test_uncertain_response_keeps_prior_results_without_retry(self):
        self.post.side_effect = [{"status": True}, RuntimeError("connection lost")]
        with self.assertRaises(SystemExit):
            homework.do_save_or_submit(self.args, False)
        self.assertEqual(self.post.call_count, 2)
        result = read_json(self.args.out)
        self.assertTrue(result["7001"]["status"])
        self.assertIsNone(result["7002"]["status"])
        self.assertTrue(result["7002"]["uncertain"])


class HomeworkParsingTests(unittest.TestCase):
    def test_pc_rows_use_title_and_p_status_without_countdown_or_tools(self):
        html = """
        <li data='/mooc-ans/mooc2/work/task?workId=1&amp;courseId=10'>
          <div class='right-content'><p class='overHidden2 fl'>分割实验</p>
          <p class='status fl'>待批阅</p><a title='查看分析'>智能分析</a></div>
          <div class='time notOver'>剩余46小时</div></li>
        <li data='/work?workId=2'><p class='overHidden2 fl'>下一次实验</p>
          <p class='status fl'>未开始</p></li>
        """
        rows = homework.parse_course_works(html)
        self.assertEqual(
            [(r["title"], r["status"], r["statusText"]) for r in rows],
            [("分割实验", "submitted", "待批阅"), ("下一次实验", "pending", "未开始")],
        )

    def test_nested_stem_preserves_content_images_and_attachments(self):
        attach = homework.build_answer_html("synthetic-object", "实验.docx", "docx")
        source = (
            "<input id=questionId value=12>"
            '<div class="ans-cc extra"><div>第一段<div>第二段</div>第三段</div>'
            '<img src="/sample.png" alt="图示">' + attach + "<p>末段</p></div>"
        )
        question = homework.parse_question(source)
        self.assertIn("末段", question["stem"])
        self.assertIn("第三段", question["stemHtml"])
        self.assertEqual(question["images"], [{"src": "/sample.png", "alt": "图示"}])
        self.assertEqual(question["attachments"][0]["objectId"], "synthetic-object")
        self.assertEqual(question["attachments"][0]["name"], "实验.docx")
        self.assertEqual(question["contentWarnings"], [])

    def test_attachment_only_question_is_not_empty(self):
        source = (
            "<input id=questionId value=12><div class=ans-cc>"
            + homework.build_answer_html("obj", "notes.docx", "docx")
            + "</div>"
        )
        question = homework.parse_question(source)
        self.assertEqual(question["stem"], "")
        self.assertEqual(len(question["attachments"]), 1)
        self.assertEqual(question["contentWarnings"], [])

    def test_unknown_or_broken_embeds_are_reported(self):
        source = (
            "<input id=questionId value=12><div class=ans-cc>"
            "<iframe module=insertAttach name=broken></iframe>"
            '<iframe src="/unknown"></iframe></div>'
        )
        question = homework.parse_question(source)
        self.assertEqual(question["attachments"], [])
        self.assertEqual(len(question["contentWarnings"]), 2)

    def test_option_values_are_paired_with_their_labels(self):
        question = homework.parse_question(
            "<input id=questionId value=12><div class=ans-cc>选择</div>"
            "<label><input type=radio value=A>区域生长</label>"
            "<input id=option-b type=radio value=B><label for=option-b>分水岭</label>"
        )
        self.assertEqual(
            [(o["value"], o["label"]) for o in question["options"]],
            [("A", "区域生长"), ("B", "分水岭")],
        )

    def test_mobile_task_ids_survive_truncated_course_names(self):
        rows = homework.parse_task_list(
            "<li data='/mtaskmsgspecial?taskrefId=1&amp;courseId=10&amp;clazzId=20'>"
            '<p>实验</p><span class="status fl">未提交</span>《智能制造...》'
            '<span aria-label="作业名称实验作业状态未提交所属课程智能制造创新思维方法'
            '剩余时间2天"></span></li>'
        )
        self.assertEqual(rows[0]["course"], "智能制造创新思维方法")
        self.assertEqual((rows[0]["courseId"], rows[0]["clazzId"]), ("10", "20"))
        self.assertEqual(rows[0]["remain"], "2天")

    def test_course_status_and_title_stay_within_each_row(self):
        html = (
            '<li data="/work?workId=1"><p title="A">A</p>已完成</li>'
            '<li data="/work?workId=2"><p title="B">B</p>未提交</li>'
        )
        rows = homework.parse_course_works(html)
        self.assertEqual(
            [(row["title"], row["status"]) for row in rows], [("A", "submitted"), ("B", "pending")]
        )

    def test_course_titles_do_not_override_status_labels(self):
        html = (
            '<li data="/work?workId=1"><p title="未提交的示例">未提交的示例</p>'
            '<span>已完成</span></li><li data="/work?workId=2">'
            '<p title="已完成的示例">已完成的示例</p><span>未提交</span></li>'
        )
        self.assertEqual(
            [row["status"] for row in homework.parse_course_works(html)], ["submitted", "pending"]
        )

    def test_question_attributes_and_long_stems(self):
        html = (
            '<div class="ans-cc">' + "long " * 200 + "</div>"
            "<INPUT VALUE=12 ID=questionId><input value=2 name=type12 type=hidden>"
            "<input value=3.5 name=score12 type=hidden><input value=A TYPE=radio>"
        )
        question = homework.parse_question(html)
        self.assertEqual(
            (question["questionId"], question["type"], question["score"]), ("12", "2", "3.5")
        )
        self.assertGreater(len(question["stem"]), 600)
        self.assertEqual(question["options"], [{"kind": "radio", "value": "A"}])

    def test_attachment_filename_is_escaped(self):
        html = homework.build_answer_html("object", 'a"<&.txt', "txt")
        self.assertIn('filename="a&quot;&lt;&amp;.txt"', html)

    def test_page_url_keeps_blank_and_repeated_query_parameters(self):
        session = Mock()
        homework.fetch_index(session, "https://example.test/work?enc=&x=1&x=2", 3)
        query = parse_qs(urlparse(session.get.call_args.args[0]).query, keep_blank_values=True)
        self.assertEqual(
            query, {"enc": [""], "x": ["1", "2"], "index": ["3"], "knowledgeid": ["0"]}
        )

    def test_non_json_answer_response_is_an_error(self):
        session = Mock()
        session.post.return_value.json.side_effect = ValueError("HTML response")
        with self.assertRaises(RuntimeError):
            homework.post_answer(session, {}, False)

    def test_answer_response_requires_status_object(self):
        for response in ([], None, {"raw": "HTML"}):
            with self.subTest(response=response):
                session = Mock()
                session.post.return_value.json.return_value = response
                with self.assertRaises(RuntimeError):
                    homework.post_answer(session, {}, False)

    def test_course_middle_parsing_is_shared_and_order_independent(self):
        session = Mock()
        session.get.return_value.text = '<INPUT value="E&amp;X" id=workEnc TYPE=hidden>'
        fields = courses.course_middle(session, "1", "2", "3")
        self.assertEqual(fields["workEnc"], "E&X")
        self.assertEqual(fields["examEnc"], "")
        session.get.assert_called_once()

    def test_exam_rows_do_not_borrow_next_status(self):
        html = (
            "<div onclick=\"viewExamAnswer('1','2')\">"
            '<p class="overHidden2">A</p></div>'
            "<div onclick=\"viewExamAnswer('3','4')\">"
            '<p class="overHidden2">B</p><p class="status">已完成</p></div>'
        )
        self.assertEqual([row["status"] for row in courses.parse_exams(html)], ["", "已完成"])


class HomeworkResolutionTests(unittest.TestCase):
    def test_pc_url_resolves_only_matching_work_course_and_class(self):
        client = Mock()
        mobile = "https://mooc1-api.chaoxing.com/mtaskmsgspecial?taskrefId=1&courseId=10&clazzId=20"
        do_url = (
            "https://mooc1-api.chaoxing.com/mooc-ans/work/phone/"
            "doHomeWork?workId=1&courseId=10&classId=20&enc=synthetic"
        )
        client.get.return_value = SimpleNamespace(text=f'<a href="{do_url}">作答</a>', url=mobile)
        tasks = [
            {"taskRefId": "1", "courseId": "10", "clazzId": "20", "taskUrl": mobile},
            {"taskRefId": "1", "courseId": "99", "clazzId": "20", "taskUrl": "wrong"},
        ]
        with patch.object(homework, "fetch_stu_work", return_value=(tasks, "")):
            actual = homework.resolve_dohomework(
                client,
                "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?workId=1&courseId=10&classId=20",
            )
        self.assertEqual(actual, do_url)
        self.assertEqual(client.get.call_args.args[0], mobile)

    def test_pc_link_without_unique_match_stops_before_opening_work(self):
        client = Mock()
        with patch.object(homework, "fetch_stu_work", return_value=([], "")):
            with self.assertRaises(ValueError):
                homework.resolve_dohomework(
                    client, "https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?workId=1"
                )
        client.get.assert_not_called()

    def test_mismatched_work_link_is_rejected(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(
            url="https://mooc1-api.chaoxing.com/mtaskmsgspecial",
            text='<a href="/work/phone/doHomeWork?courseId=10&workId=999">作答</a>',
        )
        with self.assertRaises(ValueError):
            homework.resolve_dohomework(
                client, "https://mooc1-api.chaoxing.com/mtaskmsgspecial?taskrefId=1&courseId=10"
            )

    def test_pending_filter_uses_authoritative_course_id(self):
        from chaoxing.cli import build_parser

        args = build_parser().parse_args(["pending", "--course", "智能制造创新思维方法"])
        task = {
            "taskRefId": "1",
            "name": "实验",
            "status": "未提交",
            "course": "智能制造...",
            "courseId": "10",
            "remain": "",
            "taskUrl": "unused",
        }
        with (
            patch.object(homework, "load_session"),
            patch.object(
                homework,
                "find_course",
                return_value={"courseId": "10", "name": "智能制造创新思维方法"},
            ),
            patch.object(homework, "fetch_stu_work", return_value=([task], "")),
            patch.object(homework, "save_output") as save,
            redirect_stdout(io.StringIO()),
        ):
            homework.cmd_pending(args)
        self.assertEqual(save.call_args.args[1][0]["course"], "智能制造创新思维方法")


class HomeworkFetchTests(unittest.TestCase):
    def fetch(self, pages, max_index=5, start_index=0):
        from chaoxing.cli import build_parser

        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(
                [
                    "fetch",
                    "--dohomework-url",
                    "unused",
                    "--out",
                    str(Path(directory) / "work.json"),
                    "--max-index",
                    str(max_index),
                    "--start-index",
                    str(start_index),
                ]
            )
            args.sleep = 0
            with (
                patch.object(homework, "load_session"),
                patch.object(homework, "fetch_index", side_effect=pages),
                redirect_stdout(io.StringIO()),
            ):
                homework.cmd_fetch(args)
            return read_json(args.out)

    def test_repeated_last_question_finishes_without_duplicate_content(self):
        data = self.fetch([page("1")] * 3)
        self.assertEqual(len(data["questions"]), 1)
        self.assertEqual(data["stopReason"], "repeated_question")
        self.assertFalse(data["limitReached"])

    def test_page_limit_is_not_reported_as_complete(self):
        data = self.fetch([page("1"), page("2")], max_index=2)
        self.assertEqual(len(data["questions"]), 2)
        self.assertTrue(data["limitReached"])

    def test_closed_entry_records_no_question(self):
        data = self.fetch(["<html>作业已过时效</html>"])
        self.assertEqual(data["questions"], [])
        self.assertEqual(data["stopReason"], "no_question")

    def test_next_batch_starts_at_requested_page(self):
        data = self.fetch([page("12")], max_index=1, start_index=7)
        self.assertEqual(data["startIndex"], 7)
        self.assertEqual(data["nextIndex"], 8)
        self.assertEqual(data["questions"][0]["index"], 7)

    def test_stopped_read_preserves_completed_pages_and_resume_index(self):
        from chaoxing.cli import build_parser
        from chaoxing.local import RequestStopped

        with tempfile.TemporaryDirectory() as directory:
            destination = str(Path(directory) / "work.json")
            args = build_parser().parse_args(
                [
                    "fetch",
                    "--dohomework-url",
                    "unused",
                    "--out",
                    destination,
                    "--max-index",
                    "5",
                    "--start-index",
                    "7",
                ]
            )
            args.sleep = 0
            with (
                patch.object(homework, "load_session"),
                patch.object(
                    homework, "fetch_index", side_effect=[page("12"), RequestStopped("budget")]
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaises(RequestStopped),
            ):
                homework.cmd_fetch(args)
            data = read_json(destination)
            self.assertEqual(data["nextIndex"], 8)
            self.assertEqual(data["stopReason"], "request_stopped")
            self.assertEqual([q["questionId"] for q in data["questions"]], ["12"])

    def test_partial_cache_does_not_trigger_a_scan_from_page_zero(self):
        work = {"questions": [{"questionId": "12", "index": 7}]}
        with (
            patch.object(homework, "fetch_index", return_value=page("12")) as fetch,
            patch.object(homework.time, "sleep"),
        ):
            fields = homework.collect_question_fields(None, "unused", {"12"}, work=work)
        self.assertIn("12", fields)
        fetch.assert_called_once_with(None, "unused", 7)


if __name__ == "__main__":
    unittest.main()
