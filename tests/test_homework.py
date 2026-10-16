import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
