"""Offline regression tests for student workflows and material downloads."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chaoxing import materials, overview, student

COURSE = {"courseId": "1", "clazzId": "2", "cpi": "3", "name": "课程 A"}


class FeatureTests(unittest.TestCase):
    def setUp(self):
        network_guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected network request"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def test_week_matches_accepts_numeric_strings(self):
        self.assertTrue(student.week_matches("1-8,10,12-16", "7"))
        self.assertTrue(student.week_matches("1-8,10,12-16", 10))
        self.assertFalse(student.week_matches("1-8,10,12-16", "9"))
        self.assertFalse(student.week_matches("invalid,1-8", "9"))
        self.assertTrue(student.week_matches("", 9))

    def test_schedule_defaults_to_current_week_with_string_numbers(self):
        lessons = [
            {"name": "本周课", "weeks": "4-8", "dayOfWeek": "2", "beginNumber": "3", "length": "2"},
            {"name": "其他周课", "weeks": "1-3", "dayOfWeek": 1, "beginNumber": 1, "length": 1},
        ]
        data = {
            "curriculum": {"realCurrentWeek": "4", "curriculumCount": 16},
            "lessonArray": lessons,
        }
        args = SimpleNamespace(session="unused", week=None, out="unused")
        output = io.StringIO()
        with (
            patch.object(student, "load_session"),
            patch.object(student, "fetch_schedule", return_value=data),
            patch.object(student, "save_output") as export,
            contextlib.redirect_stdout(output),
        ):
            student.cmd_schedule(args)
        self.assertIn("周二", output.getvalue())
        self.assertIn("第3-4节", output.getvalue())
        self.assertNotIn("其他周课", output.getvalue())
        self.assertEqual(export.call_args.args[1]["lessons"], lessons[:1])

        args.week = 2
        with (
            patch.object(student, "load_session"),
            patch.object(student, "fetch_schedule", return_value=data),
            patch.object(student, "save_output") as export,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            student.cmd_schedule(args)
        self.assertEqual(export.call_args.args[1]["lessons"], lessons[1:])

    def test_todos_exports_partial_failures_without_completion_claim(self):
        second = {**COURSE, "name": "课程 B", "courseId": "4"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "todos.json"
            args = SimpleNamespace(
                session="unused",
                deep=True,
                limit=10,
                sleep=0,
                out=str(destination),
                include_activities=True,
                course="",
            )
            output = io.StringIO()
            with (
                patch.object(student, "load_session", return_value=Mock()),
                patch.object(student, "list_courses", return_value=[COURSE, second]),
                patch.object(student, "get_fid", return_value="fid"),
                patch.object(
                    student, "fetch_stu_work", side_effect=ValueError("works unavailable")
                ),
                patch.object(
                    student, "fetch_activities", side_effect=[[], OSError("activity unavailable")]
                ),
                patch.object(
                    student, "open_course", side_effect=[OSError("exam unavailable"), ("", {})]
                ),
                patch.object(student, "section_urls", return_value=("exam", "topic")),
                patch.object(student, "parse_exams", return_value=[]),
                contextlib.redirect_stdout(output),
            ):
                student.cmd_todos(args)
            result = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(set(result), {"works", "activities", "exams", "errors"})
        self.assertEqual(result["exams"], [])
        self.assertEqual(
            [item["section"] for item in result["errors"]], ["works", "activities", "exams"]
        )
        self.assertEqual(result["errors"][1]["course"], "课程 B")
        self.assertNotIn("全部已完成", output.getvalue())
        self.assertIn("扫描不完整", output.getvalue())

    def test_todos_marks_unscanned_exams_and_survives_course_list_failure(self):
        work = {"status": "未提交", "name": "作业", "course": "课程 A", "remain": ""}
        args = SimpleNamespace(
            session="unused",
            deep=False,
            limit=10,
            sleep=0,
            out="unused",
            include_activities=True,
            course="",
        )
        with (
            patch.object(student, "load_session"),
            patch.object(student, "list_courses", side_effect=ValueError("bad courses")),
            patch.object(student, "fetch_stu_work", return_value=([work], "")),
            patch.object(student, "save_output") as export,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            student.cmd_todos(args)
        result = export.call_args.args[1]
        self.assertEqual(result["works"], [work])
        self.assertIsNone(result["exams"])
        self.assertEqual(result["errors"][0]["section"], "courses")
        self.assertEqual(result["errors"][1]["section"], "activities")

    def test_study_reuses_page_and_preserves_endpoint_failure(self):
        session = Mock()
        page = Mock(status_code=200, text="var pEnc = 'page-token';")
        work = Mock()
        work.json.return_value = {"data": {"finishCount": 2, "receivedNum": 3}}
        sign = Mock()
        sign.json.side_effect = ValueError("not JSON")
        session.get.side_effect = [page, work, sign]
        args = SimpleNamespace(session="unused", course="1", sleep=0, save_html=None, out="unused")
        output = io.StringIO()
        with (
            patch.object(student, "load_session", return_value=session),
            patch.object(student, "find_course", return_value=COURSE),
            patch.object(student, "STUDY_STATS", ["work", "sign"]),
            patch.object(student, "save_output") as export,
            contextlib.redirect_stdout(output),
        ):
            student.cmd_study(args)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(session.get.call_args.kwargs["params"]["pEnc"], "page-token")
        result = export.call_args.args[1]
        self.assertEqual(result["work"], work.json.return_value)
        self.assertIn("not JSON", result["errors"]["sign"])
        self.assertIn("不可用", output.getvalue())

    def test_material_rows_do_not_borrow_next_rows_cells(self):
        html = (
            '<ul class="dataBody_td" id="1" dataName="first" type="file"></ul>'
            '<ul class="dataBody_td" id="2" dataName="second" type="file">'
            '<li class="dataBody_size_stu">42KB</li>'
            + " " * 1600
            + '<li class="dataBody_time_stu">2026-10-10</li></ul>'
        )
        items = materials.parse_materials(html)
        self.assertEqual(items[0]["size"], "")
        self.assertEqual(items[1]["size"], "42KB")
        self.assertEqual(items[1]["time"], "2026-10-10")

    def test_download_filenames_are_safe_for_windows(self):
        for name, expected in [
            (".", "42"),
            ("..", "42"),
            ("CON.txt", "_CON.txt"),
            ("aux", "_aux"),
            ("COM1.csv", "_COM1.csv"),
            ("CONIN$", "_CONIN$"),
            ("NUL .txt", "_NUL .txt"),
            ("report. ", "report"),
            ("a/b\\c", "a_b_c"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(materials._safe_filename(name, "42"), expected)
        self.assertEqual(materials._safe_filename("..", ".."), "download")

    def test_download_failure_preserves_file_and_closes_response(self):
        def chunks(size):
            yield b"partial"
            raise OSError("connection interrupted")

        response = Mock(status_code=200)
        response.iter_content.side_effect = chunks
        self._check_download(response, b"existing", b"existing", "失败 1")

    def test_download_replaces_file_only_after_success(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b"full", b"", b" content"]
        self._check_download(response, b"existing", b"full content", "成功 1")

    def test_download_http_failure_closes_response(self):
        response = Mock(status_code=404)
        self._check_download(response, b"existing", b"existing", "HTTP 404")
        response.iter_content.assert_not_called()

    def _check_download(self, response, original, expected, message):
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.txt"
            target.write_bytes(original)
            args = SimpleNamespace(
                session="unused",
                course="1",
                data_id="42",
                name="report.txt",
                out=directory,
                sleep=0,
            )
            output = io.StringIO()
            with (
                patch.object(materials, "load_session", return_value=session),
                patch.object(materials, "find_course", return_value=COURSE),
                contextlib.redirect_stdout(output),
            ):
                materials.cmd_download(args)
            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["report.txt"])
        response.close.assert_called_once()
        self.assertEqual(session.get.call_count, 1)
        self.assertIn(message, output.getvalue())

    def test_course_overview_keeps_topics_when_exam_request_fails(self):
        session = Mock()
        session.get.side_effect = [OSError("exam unavailable"), Mock(text="topics")]
        mid = {"enc": "token"}
        chapters = {"chapters": [], "globalProgressAvailable": False}
        args = SimpleNamespace(session="unused", course="1")
        output = io.StringIO()
        with (
            patch.object(overview, "load_session", return_value=session),
            patch.object(overview, "find_course", return_value=COURSE),
            patch.object(overview, "open_course", return_value=("", mid)) as opening,
            patch.object(overview, "section_urls", return_value=("exam", "topic")),
            patch.object(overview, "list_chapters", return_value=(chapters, "")),
            patch.object(overview, "course_works", return_value=[]) as works,
            patch.object(overview, "list_materials", return_value=([], "", 0)),
            patch.object(overview, "parse_topics", return_value=[{"text": "discussion"}]),
            contextlib.redirect_stdout(output),
        ):
            overview.cmd_course(args)
        opening.assert_called_once()
        works.assert_called_once_with(session, COURSE, mid=mid)
        self.assertEqual(session.get.call_args.args[0], "topic")
        self.assertIn("discussion", output.getvalue())


if __name__ == "__main__":
    unittest.main()
