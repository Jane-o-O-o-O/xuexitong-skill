"""Opt-in account smoke check. All HTTP writes are blocked; outputs stay private."""

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "xuexitong" / "scripts"))

from chaoxing import cli  # noqa: E402
from chaoxing.common import read_json, write_json  # noqa: E402
from chaoxing.session import SESSION_FILE, ChaoxingSession  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=SESSION_FILE)
    parser.add_argument("--course", required=True)
    parser.add_argument("--work-id", required=True, help="A currently readable homework ID")
    parser.add_argument("--max-index", type=cli.positive_int, default=1)
    parser.add_argument("--download", action="store_true", help="Also download stem attachments")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    destination = (
        Path(args.out).expanduser()
        if args.out
        else Path(args.session).resolve().parent / f"smoke-{int(time.time())}"
    ).resolve()
    if destination.is_relative_to(ROOT):
        parser.error("Account outputs must be outside the repository")
    destination.mkdir(parents=True, exist_ok=True)
    report = {"passed": [], "requests": 0, "httpWrites": 0}
    original_send = ChaoxingSession.send

    def read_only_send(client, request, **kwargs):
        if request.method not in {"GET", "HEAD"}:
            raise RuntimeError("Live smoke check blocked a write request")
        report["requests"] += 1
        return original_send(client, request, **kwargs)

    def run(*arguments):
        # CLI stdout can contain authenticated work URLs; keep it out of console logs.
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                code = cli.main(["--session", args.session, *arguments])
            except SystemExit as error:
                code = error.code
        if code:
            raise RuntimeError(
                f"{arguments[0]} stopped with exit code {code}; inspect request-status"
            )
        report["passed"].append(arguments[0])
        print(f"[ok] {arguments[0]}", flush=True)
        return output.getvalue()

    try:
        with patch.object(ChaoxingSession, "send", read_only_send):
            if not json.loads(run("check"))["loggedIn"]:
                raise RuntimeError("Session is not logged in")
            courses_path = str(destination / "courses.json")
            run("courses", "--out", courses_path)
            matches = [
                c
                for c in read_json(courses_path)
                if args.course == c["courseId"] or args.course in c["name"]
            ]
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one course")
            course = matches[0]
            works_path = str(destination / "works.json")
            run("works", "--course", course["courseId"], "--out", works_path)
            works = read_json(works_path)
            if any(w.get("error") for w in works):
                raise RuntimeError("Course works contain partial failures")
            report["workStatuses"] = [w["status"] for w in works]
            targets = [w for w in works if w.get("workId") == args.work_id]
            if len(targets) != 1:
                raise RuntimeError("Expected exactly one target assignment")
            run(
                "pending",
                "--all",
                "--course",
                course["name"],
                "--out",
                str(destination / "pending.json"),
            )
            resolved_path = str(destination / "resolved.json")
            run("resolve", "--task-url", targets[0]["detailUrl"], "--out", resolved_path)
            work_url = read_json(resolved_path)["doHomeWorkUrl"]
            work_path = str(destination / "work.json")
            run(
                "fetch",
                "--dohomework-url",
                work_url,
                "--max-index",
                str(args.max_index),
                "--out",
                work_path,
            )
            work = read_json(work_path)
            if not work["questions"] or any(q["contentWarnings"] for q in work["questions"]):
                raise RuntimeError("Question content is missing or has extraction warnings")
            report["pageLimitReached"] = work["limitReached"]
            report["nextIndex"] = work["nextIndex"]
            report["questions"] = len(work["questions"])
            report["attachments"] = sum(len(q["attachments"]) for q in work["questions"])
            if args.download and report["attachments"]:
                manifest = str(destination / "downloads.json")
                run(
                    "download-attachments",
                    "--work-json",
                    work_path,
                    "--out",
                    str(destination / "files"),
                    "--result-json",
                    manifest,
                )
                data = read_json(manifest)
                if data["failed"] or data["success"] != data["total"]:
                    raise RuntimeError("Attachments were not all downloaded")
                report["downloadBytes"] = sum(f["bytes"] for f in data["files"])
            report["success"] = True
    except (RuntimeError, OSError, ValueError) as error:
        report["success"] = False
        report["error"] = str(error)
    write_json(destination / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Private outputs: {destination}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
