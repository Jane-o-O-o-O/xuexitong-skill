"""Command definitions and dispatch, without service logic."""

import argparse
import sys
from pathlib import Path

import requests

from .common import die
from .courses import cmd_chapters, cmd_courses, cmd_exams, cmd_topics
from .diagnostics import NETWORK_TARGETS, cmd_network_info, safe_error_text
from .homework import (
    cmd_download_attachments,
    cmd_fetch,
    cmd_pending,
    cmd_resolve,
    cmd_save_or_submit,
    cmd_template,
    cmd_upload,
    cmd_verify,
    cmd_works,
)
from .local import MIN_REQUEST_INTERVAL, QUERY_REQUEST_LIMIT, RequestStopped, local_runtime
from .materials import cmd_download, cmd_materials
from .overview import cmd_course
from .session import (
    SESSION_FILE,
    cmd_check,
    cmd_login,
    cmd_request_status,
    cmd_session_info,
    cmd_set_cookie,
)
from .student import cmd_notices, cmd_schedule, cmd_study, cmd_todos, cmd_wrongque


def nonnegative_float(value):
    number = float(value)
    if number < 0 or not number < float("inf"):
        raise argparse.ArgumentTypeError("必须是有限的非负数")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def positive_int(value):
    number = nonnegative_int(value)
    if number == 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def build_parser():
    parser = argparse.ArgumentParser(description="学习通本地助手：课程、作业、资料与学习信息")
    parser.add_argument("--session", default=SESSION_FILE, help="本地会话文件路径")
    parser.add_argument(
        "--sleep",
        type=nonnegative_float,
        default=MIN_REQUEST_INTERVAL,
        help="请求间隔秒，最低 8 秒；只可调慢",
    )
    sub = parser.add_subparsers(dest="cmd")

    def command(name, help_text, handler, *, course=False, out=None, html=False):
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(handler=handler)
        if course:
            sp.add_argument("--course", required=True, help="课名关键词或 courseId")
        if out is not None:
            sp.add_argument("--out", default=out)
        if html:
            sp.add_argument("--save-html", default="")
        return sp

    sp = command("login", "备用账号密码登录", cmd_login)
    sp.add_argument("--phone", required=True)
    sp.add_argument("--password", default="", help="留空时在本地终端隐式输入")
    sp = command("set-cookie", "从本机浏览器导入登录态", cmd_set_cookie)
    cookies = sp.add_mutually_exclusive_group(required=True)
    cookies.add_argument("--cookie", default="")
    cookies.add_argument("--cookie-file", default="", help="本地 Cookie 文本文件")
    command("check", "检查登录态", cmd_check)
    command("session-info", "查看本地会话到期信息", cmd_session_info)
    command("request-status", "查看本地冷却和脱敏停止原因，不联网", cmd_request_status)
    sp = command("network-info", "查看有效系统/环境代理，不联网或读取 Cookie", cmd_network_info)
    sp.add_argument("--target", choices=tuple(NETWORK_TARGETS), default="upload")
    command("courses", "列出课程及其 ID", cmd_courses, out="")

    sp = command("works", "查询指定课程的作业及状态", cmd_works, out="")
    sp.add_argument("--course", default="", help="课名关键词或 courseId")
    sp.add_argument("--all-courses", action="store_true", help="显式扫描全部课程")
    sp.add_argument("--pending-only", action="store_true")

    sp = command(
        "wrongque", "错题集 (含我的答案 vs 正确答案)", cmd_wrongque, course=True, out="", html=True
    )
    sp.add_argument("--keyword", default="")
    sp.add_argument("--qtype", default="", help="题型 id, 留空=全部")
    sp.add_argument("--limit", type=nonnegative_int, default=0, help="只显示前 N 题")
    command("study", "学习记录 (排名/完成率/出勤/直播)", cmd_study, course=True, out="", html=True)

    sp = command("notices", "通知 / 公告", cmd_notices, out="")
    sp.add_argument("--limit", type=positive_int, default=5)
    sp.add_argument("--unread-only", action="store_true")
    sp.add_argument("--keyword", default="")
    sp = command("schedule", "课表", cmd_schedule, out="")
    sp.add_argument("--week", type=positive_int, default=None, help="查第 N 周, 默认当前周")
    sp = command("todos", "查询未交作业，按需添加活动和考试", cmd_todos, out="")
    sp.add_argument("--deep", action="store_true", help="添加所选课程的未完成考试")
    sp.add_argument("--limit", type=nonnegative_int, default=30)
    sp.add_argument("--course", default="", help="活动/考试限定课程")
    sp.add_argument("--include-activities", action="store_true", help="添加所选课程的未完成活动")
    sp.add_argument("--all-courses", action="store_true", help="显式扫描全部课程")
    sp = command("download", "下载课程资料到本地", cmd_download, course=True)
    sp.add_argument("--out", required=True, help="保存目录")
    sp.add_argument("--data-id", default="", help="只下一个文件")
    sp.add_argument(
        "--all-files", action="store_true", help="显式选择当前页全部文件，仍受请求预算限制"
    )
    sp.add_argument("--name", default="")

    for name, help_text, handler in (
        ("exams", "课程考试列表", cmd_exams),
        ("topics", "课程讨论区话题", cmd_topics),
        ("chapters", "章节树 + 任务点进度", cmd_chapters),
    ):
        command(name, help_text, handler, course=True, out="", html=True)
    command("course", "组合查询课程的多个板块", cmd_course, course=True)
    sp = command("materials", "查询课程资料目录", cmd_materials, course=True, out="", html=True)
    sp.add_argument("--tree", action="store_true", help="递归展开目录，受深度上限影响")
    sp.add_argument("--max-depth", type=nonnegative_int, default=4)
    for flag in ("data-id", "data-name", "parent", "enc"):
        sp.add_argument(f"--{flag}", default="")

    sp = command(
        "pending", "列出未交作业，默认只读列表", cmd_pending, out="pending.json", html=True
    )
    sp.add_argument("--max-index", type=positive_int, default=1)
    sp.add_argument("--work-id", default="", help="只保留一份指定作业；展开详情时优先指定")
    sp.add_argument("--all", action="store_true", help="包含待批阅/已完成")
    sp.add_argument("--course", default="", help="按课名或 courseId 过滤全局作业列表")
    sp.add_argument("--resolve", action="store_true", help="解析所选作业链接及题量，增加请求")
    sp = command("resolve", "解析一份作业的作答链接", cmd_resolve, out="")
    sp.add_argument("--task-url", required=True)
    sp = command("fetch", "读取指定作业题目", cmd_fetch, out="work.json", html=True)
    sp.add_argument("--dohomework-url", required=True)
    sp.add_argument("--max-index", type=positive_int, default=1, help="本批最多读取页数，默认 1")
    sp.add_argument(
        "--start-index", type=nonnegative_int, default=0, help="本批起始页，下标从 0 开始"
    )
    sp = command("download-attachments", "下载题干附件（只读）", cmd_download_attachments)
    sp.add_argument("--work-json", required=True)
    sp.add_argument("--out", required=True, help="本地附件目录")
    sp.add_argument("--question-id", default="")
    sp.add_argument("--object-id", default="")
    sp.add_argument(
        "--all-files", action="store_true", help="显式选择匹配的全部附件，仍受请求预算限制"
    )
    sp.add_argument("--result-json", default="", help="下载结果清单")
    sp = command("template", "生成本地答案模板", cmd_template, out="answers.json")
    sp.add_argument("--work-json", required=True)
    sp = command("upload", "上传一个本地附件，返回 objectId 与答案 HTML", cmd_upload)
    sp.add_argument("--dohomework-url", required=True)
    sp.add_argument("--file", required=True)
    sp = command("verify", "回读已存作答与附件", cmd_verify)
    sp.add_argument("--dohomework-url", required=True)
    sp.add_argument("--work-json", default="")
    for name, temp in (("save", True), ("submit", False)):
        sp = command(
            name,
            "暂存作答到学习通" if temp else "提交作业，需要 --confirm",
            cmd_save_or_submit,
            out="",
        )
        sp.add_argument("--dohomework-url", required=True)
        sp.add_argument("--answers", required=True)
        sp.add_argument("--work-json", default="")
        sp.add_argument("--brute-max", type=positive_int, default=40)
        sp.add_argument("--confirm", action="store_true", help="交卷必需")
        sp.set_defaults(temp_save=temp)
    sp = command("qr-login", "学习通 App 扫码登录", cmd_qr_login)
    sp.add_argument("--timeout", type=positive_int, default=150)
    return parser


def cmd_qr_login(args):
    import qr_login

    qr_login.QR_PNG = str(Path(args.session).resolve().parent / "qr.png")
    qr_login.STATUS_JSON = str(Path(args.session).resolve().parent / "qr_status.json")
    result = qr_login.run_login(args)
    if result:
        raise SystemExit(result)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    if args.cmd == "works" and not args.course and not args.all_courses:
        parser.error("works 需要 --course；全课扫描需显式加 --all-courses")
    if args.cmd == "download" and not args.data_id and not args.all_files:
        parser.error("download 需要 --data-id；批量下载当前页需显式加 --all-files")
    if args.cmd == "todos" and (args.include_activities or args.deep):
        if not args.course and not args.all_courses:
            parser.error("活动/考试扫描需要 --course 或 --all-courses")
    try:
        if args.cmd in {"template", "session-info", "request-status", "network-info"}:
            args.handler(args)
        else:
            mutation_or_login = args.cmd in {
                "qr-login",
                "login",
                "set-cookie",
                "upload",
                "save",
                "submit",
            }
            budget = None if mutation_or_login else QUERY_REQUEST_LIMIT
            with local_runtime(args.session, args.sleep, max_requests=budget):
                args.handler(args)
    except RequestStopped as error:
        die(str(error), 3)
    except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
        die(f"{type(error).__name__}: {safe_error_text(error)}")
    except KeyboardInterrupt as error:
        print(f"[!] {safe_error_text(error)}", file=sys.stderr)
        return 130
    return 0
