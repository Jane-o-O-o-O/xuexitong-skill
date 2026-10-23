"""Homework operations for the Chaoxing client."""

import base64
import html as html_lib
import json
import mimetypes
import os
import re
import sys
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from .common import (
    _question_id,
    abs_url,
    die,
    extract_hidden_fields,
    fmt_size,
    hidden_by_id,
    parse_inputs,
    read_json,
    save_output,
    strip_tags,
    write_json,
    write_text,
)
from .courses import course_middle, find_course, list_courses
from .diagnostics import safe_error_text, safe_server_message
from .local import RequestStopped, official_url
from .markup import parse_html
from .materials import _safe_filename, download_file
from .session import load_session

PENDING_WORDS = ("未交", "未提交", "待提交", "未开始", "去完成", "去提交")


DONE_WORDS = ("已交", "已提交", "已完成", "查看", "已批阅", "待批阅")


def _classify_status(text: str) -> str:
    if any(w in text for w in PENDING_WORDS):
        return "pending"
    if any(w in text for w in DONE_WORDS):
        return "submitted"
    return "unknown"


def course_works(s: requests.Session, course: dict, mid=None) -> list:
    if mid is None:
        mid = course_middle(s, course["courseId"], course["clazzId"], course["cpi"])
    url = (
        "https://mooc1.chaoxing.com/mooc2/work/list"
        f"?courseId={course['courseId']}&classId={course['clazzId']}&cpi={course['cpi']}&ut=s"
        f"&openc={mid.get('openc', '')}&enc={mid.get('workEnc', '')}"
        f"&t={int(time.time() * 1000)}&stuenc={mid.get('enc', '')}&isdisplaytable=2"
    )
    r = s.get(url, timeout=30)
    html = r.text
    if "无权限" in html or "未登录" in html:
        return [{"error": "无权限/登录失效", "course": course["name"]}]

    return parse_course_works(html, course["name"])


def parse_course_works(html: str, course_name="") -> list:
    """Parse each work within its own list item to avoid borrowing a neighbor's status."""
    works, seen = [], set()
    for row in parse_html(html).walk("li"):
        href = (row.attrs.get("data") or "").strip()
        if not href:
            continue
        nodes = list(row.walk())
        title_node = next((n for n in nodes if n.has_class("overHidden2")), None)
        if title_node is None:
            title_node = next((n for n in nodes if n.attrs.get("title")), None)
        if title_node is None:
            title_node = next((n for n in nodes if n.tag == "p"), None)
        title = (
            (title_node.attrs.get("title") or title_node.text())
            if title_node is not None
            else row.text()[:60]
        )
        wid = parse_qs(urlparse(href).query).get("workId", [""])[0]
        if wid and wid in seen:
            continue
        if wid:
            seen.add(wid)
        text = row.text()
        status_node = next((n for n in nodes if n.has_class("status")), None)
        labels = [n.text() for n in nodes if n.tag in {"span", "p"} and n is not title_node]
        status_text = (
            status_node.text()
            if status_node is not None
            else next(
                (label for label in labels if label in PENDING_WORDS + DONE_WORDS),
                text.replace(title, "", 1),
            )
        )
        works.append(
            {
                "course": course_name,
                "title": title,
                "workId": wid,
                "status": _classify_status(status_text),
                "statusText": status_text[:80],
                "detailUrl": abs_url("https://mooc1.chaoxing.com", href),
            }
        )
    return works


def cmd_works(args):
    s = load_session(args.session)
    courses = list_courses(s)
    if args.course:
        kw = args.course
        courses = [c for c in courses if kw in c["name"] or kw == c["courseId"]]
        if not courses:
            die(f"没有匹配的课程: {kw}")
    all_works = []
    for c in courses:
        try:
            all_works.extend(course_works(s, c))
        except RequestStopped:
            raise
        except Exception as e:
            all_works.append({"error": str(e), "course": c["name"]})
        time.sleep(args.sleep)
    if args.pending_only:
        all_works = [w for w in all_works if w.get("status") == "pending" or w.get("error")]
    print(json.dumps(all_works, ensure_ascii=False, indent=2))
    save_output(args.out, all_works)


# Retained for callers importing the legacy chaoxing_hw module.
TASK_LI_RE = re.compile(r'<li[^>]*\bdata="([^"]*mtaskmsgspecial[^"]*)"[^>]*>([\s\S]*?)</li>', re.I)

A11Y_RE = re.compile(r"作业名称(.*?)作业状态(.*?)所属课程(.*?)(?:剩余时间(.*))?$")


def parse_task_list(html: str) -> list:
    tasks, seen = [], set()
    for row in parse_html(html).walk("li"):
        url = row.attrs.get("data") or ""
        if "mtaskmsgspecial" not in url.lower():
            continue
        url = urljoin("https://mooc1-api.chaoxing.com", url)
        if url in seen:
            continue
        seen.add(url)
        name = status = course = remain = ""
        nodes = list(row.walk())
        name = next((n.text() for n in nodes if n.tag == "p"), "")
        status = next((n.text() for n in nodes if n.has_class("status")), "")
        cm = re.search(r"《([^》]*)》", row.text())
        if cm:
            course = cm.group(1).strip()
        remain = next((n.text() for n in nodes if n.has_class("fr")), "")

        # Some mobile statuses appear only in the accessible label; names may still be truncated.
        label = next((n.attrs["aria-label"] for n in nodes if n.attrs.get("aria-label")), "")
        if label:
            g = A11Y_RE.match(label)
            if g:
                name = g.group(1).strip() or name
                status = g.group(2).strip() or status
                course = g.group(3).strip() or course
                remain = remain or (g.group(4) or "")

        query = {k.lower(): v[0] for k, v in parse_qs(urlparse(url).query).items()}
        tasks.append(
            {
                "taskRefId": query.get("taskrefid", ""),
                "name": name,
                "status": status,
                "course": course,
                "courseId": query.get("courseid", ""),
                "clazzId": query.get("clazzid", query.get("classid", "")),
                "remain": remain,
                "taskUrl": url,
            }
        )
    return tasks


def fetch_stu_work(s: requests.Session):
    html = s.get("https://mooc1-api.chaoxing.com/work/stu-work", timeout=30).text
    return parse_task_list(html), html


def resolve_dohomework(s: requests.Session, task_url: str) -> str:
    official_url(task_url)
    parts = urlparse(task_url)
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    expected_work = query.get("workid") or query.get("taskrefid")
    expected_course = query.get("courseid")
    expected_class = query.get("clazzid") or query.get("classid")
    if "/mooc2/work/" in parts.path:
        if not expected_work:
            raise ValueError("电脑端作业链接缺少 workId")
        tasks, _ = fetch_stu_work(s)
        matches = [
            t
            for t in tasks
            if t["taskRefId"] == expected_work
            and (not expected_course or t.get("courseId") == expected_course)
            and (not expected_class or t.get("clazzId") == expected_class)
        ]
        if len(matches) != 1:
            raise ValueError("全局列表中未找到唯一的同课程作业；请检查作业是否已提交或过期")
        task_url = matches[0]["taskUrl"]
    if "/work/phone/doHomeWork" in urlparse(task_url).path:
        resolved = task_url
    else:
        response = s.get(task_url, timeout=30)
        if "/work/phone/doHomeWork" in urlparse(response.url).path:
            resolved = response.url
        else:
            resolved = _mobile_work_link(response.text)
    if not resolved:
        return ""
    actual = {k.lower(): v[0] for k, v in parse_qs(urlparse(resolved).query).items()}
    for key, expected in (
        ("workid", expected_work),
        ("courseid", expected_course),
        ("classid", expected_class),
    ):
        if expected and actual.get(key) != expected:
            raise ValueError("解析出的作答链接与目标作业、课程或班级不符，已停止")
    official_url(resolved)
    return resolved


def _mobile_work_link(html):
    m = re.search(r"/work/phone/doHomeWork\?[^\"']+", html) or re.search(
        r"doHomeWork\?courseId=\d+[^\"']+", html
    )
    if not m:
        return ""
    path_qs = m.group(0)
    if path_qs.startswith("/work/"):
        path_qs = "/mooc-ans" + path_qs
    elif path_qs.startswith("doHomeWork"):
        path_qs = "/mooc-ans/work/phone/" + path_qs
    return "https://mooc1-api.chaoxing.com" + html_lib.unescape(path_qs)


def fetch_index(s: requests.Session, do_url: str, idx: int) -> str:
    u = urlparse(do_url)
    q = parse_qs(u.query, keep_blank_values=True)
    q["index"] = [str(idx)]
    q.setdefault("knowledgeid", ["0"])
    new_q = urlencode(q, doseq=True)
    return s.get(urlunparse(u._replace(query=new_q)), timeout=30).text


def probe_task(s, do_url, max_index, sleep=0.4):
    """探测一份作业: 返回 (题量, 备注)。已提交/过期的作业页面只留一句提示, 题量必为 0。

    坑: 超星对超出题数范围的 index 会「重复返回同一个 questionId」而不是返回空页,
    所以必须去重, 否则题量会被算成 max_index。
    """
    if not do_url:
        return 0, "找不到 doHomeWork 链接"
    first = fetch_index(s, do_url, 0)
    txt = strip_tags(first)
    for bad in ("已过时效", "已截止", "不能操作", "已提交", "无权限", "已交卷"):
        if bad in txt:
            return 0, bad
    qid0 = _question_id(first)
    if not qid0:
        return 0, "页面无题目"

    seen = {qid0}
    for i in range(1, max_index):
        time.sleep(sleep)
        qid = _question_id(fetch_index(s, do_url, i))
        if not qid:
            break
        if qid in seen:
            break
        seen.add(qid)
    else:
        return len(seen), "已达到本批读取上限，完整题量未确认"
    return len(seen), ""


def cmd_pending(args):
    s = load_session(args.session)
    tasks, html = fetch_stu_work(s)
    if args.save_html:
        write_text(args.save_html, html)

    total = len(tasks)
    if not args.all:
        tasks = [t for t in tasks if t["status"] == "未提交"]
    if args.course:
        course = find_course(s, args.course)
        tasks = [t for t in tasks if t.get("courseId") == course["courseId"]]
        for task in tasks:
            task["course"] = course["name"]
    if args.work_id:
        tasks = [t for t in tasks if t.get("taskRefId") == args.work_id]
    if args.resolve and len(tasks) > 1:
        die("一次只展开一份作业；请用 --work-id 指定目标，或单独执行 resolve")

    out = []
    for t in tasks:
        t = dict(t)
        t["doHomeWorkUrl"] = ""
        t["questionCount"] = None
        t["sampledQuestionCount"] = None
        t["detailsResolved"] = False
        t["resolveError"] = ""
        try:
            if args.resolve:
                t["detailsResolved"] = True
                do = resolve_dohomework(s, t["taskUrl"])
                t["doHomeWorkUrl"] = do
                n, note = probe_task(s, do, args.max_index, args.sleep)
                t["sampledQuestionCount"] = n
                t["questionCount"] = n if not note else None
                t["resolveError"] = note
        except RequestStopped:
            raise
        except Exception as e:
            t["resolveError"] = str(e)
        out.append(t)
        if args.resolve:
            time.sleep(args.sleep)

    if args.all:
        print(f"[全部] {len(out)} 项")
    else:
        print(f"[未提交] {len(out)} 项 (从 {total} 条作业记录中筛出)")
    for i, t in enumerate(out, 1):
        tail = f"  {t['remain']}" if t["remain"] else ""
        q = (
            f"  {t['questionCount']}题"
            if t["questionCount"]
            else (f"  [{t['resolveError']}]" if t["resolveError"] else "")
        )
        print(f"  {i:>2}. {t['name']}  《{t['course']}》{tail}{q}")

    save_output(args.out, out)


def cmd_resolve(args):
    url = resolve_dohomework(load_session(args.session), args.task_url)
    if not url:
        die("未找到作答链接；请检查作业状态或在官方页面查看。")
    payload = {"doHomeWorkUrl": url}
    if args.out:
        save_output(args.out, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False))


def parse_question(html: str) -> dict:
    qid = _question_id(html)
    inputs = parse_inputs(html)
    fields = {item["name"]: item.get("value", "") for item in inputs if item.get("name")}
    tree = parse_html(html)
    content = next((n for n in tree.walk() if n.has_class("ans-cc")), None)
    stem = content.text() if content is not None else ""
    warnings, attachments, images = [], [], []
    if content is not None:
        for node in content.walk():
            if node.tag == "img":
                images.append({"src": node.attrs.get("src", ""), "alt": node.attrs.get("alt", "")})
            elif node.tag == "iframe":
                if node.attrs.get("module", "").lower() != "insertattach":
                    warnings.append("题干含未解析的 iframe，请结合原始页面查看")
                    continue
                try:
                    encoded = node.attrs.get("name") or ""
                    meta = json.loads(unquote(base64.b64decode(encoded, validate=True).decode()))
                    oid = meta.get("objectid") or meta.get("objectId")
                    if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid):
                        raise ValueError("invalid objectid")
                    attachment = {
                        "objectId": oid,
                        "name": str(meta.get("name") or node.attrs.get("filename") or oid),
                        "type": str(meta.get("type") or node.attrs.get("filetype") or ""),
                        "size": meta.get("size", ""),
                    }
                    if attachment not in attachments:
                        attachments.append(attachment)
                except (ValueError, TypeError, AttributeError, UnicodeError):
                    warnings.append("附件元数据无法解析，请结合原始页面查看")
    qtype = score = None
    if qid:
        qtype = fields.get(f"type{qid}")
        score = fields.get(f"score{qid}")

    # 选择题的候选值 —— 答案要填的就是这些 value
    options = []
    labels = {n.attrs.get("for"): n for n in tree.walk("label") if n.attrs.get("for")}
    for node in tree.walk("input"):
        kind = (node.attrs.get("type") or "").lower()
        if kind not in {"radio", "checkbox"}:
            continue
        option = {"kind": kind, "value": node.attrs.get("value", "")}
        label = labels.get(node.attrs.get("id"))
        parent = node.parent
        while label is None and parent is not None and parent is not tree:
            if parent.tag in {"label", "li", "p"}:
                label = parent
                break
            parent = parent.parent
        if label is not None:
            option["label"] = label.text()
            option["html"] = label.inner_html()
        options.append(option)
    if qid and content is None:
        warnings.append("未找到题干容器，请结合原始页面查看")
    return {
        "questionId": qid,
        "type": qtype,
        "score": score,
        "stem": stem,
        "stemHtml": content.inner_html() if content is not None else "",
        "options": options,
        "images": images,
        "attachments": attachments,
        "contentWarnings": warnings,
    }


def cmd_fetch(args):
    s = load_session(args.session)
    questions, seen, base_fields = [], set(), None
    stop_reason = "max_index"
    start_index = args.start_index
    next_index = start_index
    stopped = None
    try:
        for idx in range(start_index, start_index + args.max_index):
            html = fetch_index(s, args.dohomework_url, idx)
            next_index = idx + 1
            if base_fields is None:
                base_fields = extract_hidden_fields(html)
            if args.save_html:
                os.makedirs(args.save_html, exist_ok=True)
                write_text(os.path.join(args.save_html, f"index{idx}.html"), html)
            q = parse_question(html)
            qid = q.get("questionId")
            if not qid:
                stop_reason = "no_question"
                break
            if qid in seen:
                stop_reason = "repeated_question"
                break
            seen.add(qid)
            q["index"] = idx
            questions.append(q)
            time.sleep(args.sleep)
    except RequestStopped as error:
        stop_reason = "request_stopped"
        stopped = error
    payload = {
        "doHomeWorkUrl": args.dohomework_url,
        "questions": questions,
        "baseFields": base_fields or {},
        "fetchedAt": int(time.time()),
        "stopReason": stop_reason,
        "limitReached": stop_reason == "max_index",
        "startIndex": start_index,
        "nextIndex": next_index,
    }
    write_json(args.out, payload)
    print(f"[+] 抓题完成: {len(questions)} 题 -> {args.out}")
    for q in questions:
        opt = f"  选项={[o['value'] for o in q['options']]}" if q["options"] else ""
        print(f"    qid={q['questionId']}  type={q['type']}  score={q['score']}{opt}")
        if q["stem"]:
            print(f"      题干: {q['stem'][:80]}")
        for attachment in q["attachments"]:
            print(f"      附件: {attachment['name']} ({attachment['size']})")
        for warning in q["contentWarnings"]:
            print(f"      [!] {warning}")
    if stop_reason == "max_index" or not questions:
        print("[!] 达到页数上限或页面无题目，不能确认已完整读取；请检查作业状态和原始页面。")
    if stopped is not None:
        raise stopped


def cmd_template(args):
    data = read_json(args.work_json)
    tmpl = {q["questionId"]: "" for q in data.get("questions", []) if q.get("questionId")}
    write_json(args.out, tmpl)
    print(f"[+] 答案模板 {len(tmpl)} 项 -> {args.out}  (填 answer 字段即可)")


def attachment_download_url(s, attachment):
    """Follow the PC preview widget's observed objectId -> download anchor flow."""
    oid = attachment["objectId"]
    if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid):
        raise ValueError("无效附件 objectId")
    preview = "https://mooc1.chaoxing.com/mooc-ans/ueditorupload/read?" + urlencode(
        {
            "objectId": oid,
            "fileOriName": attachment["name"],
        }
    )
    response = s.get(preview, timeout=30)
    links = set()
    for anchor in parse_html(response.text).walk("a"):
        href = urljoin(response.url, anchor.attrs.get("href") or "")
        path = urlparse(href).path.rstrip("/")
        if path == f"/download/{oid}":
            official_url(href)
            links.add(href)
    if len(links) != 1:
        raise ValueError("官方预览页未提供唯一的附件下载链接；可能不允许下载或页面结构已变化")
    return links.pop()


def cmd_download_attachments(args):
    work = read_json(args.work_json)
    targets = [
        (q["questionId"], attachment)
        for q in work.get("questions", [])
        if not args.question_id or q.get("questionId") == args.question_id
        for attachment in q.get("attachments", [])
        if not args.object_id or attachment.get("objectId") == args.object_id
    ]
    if not targets:
        die("缓存中没有匹配的题目附件；请用新版 fetch 重新读取并检查 contentWarnings")
    if len(targets) > 1 and not getattr(args, "all_files", False):
        die("一次只下载一个附件；请用 --object-id 选定目标，或明确使用 --all-files 分批下载")
    os.makedirs(args.out, exist_ok=True)
    s = load_session(args.session)
    results = []
    try:
        for qid, attachment in targets:
            oid = attachment["objectId"]
            safe = _safe_filename(f"{qid}-{oid}-{attachment['name']}")
            path = os.path.abspath(os.path.join(args.out, safe))
            result = {
                "questionId": qid,
                "objectId": oid,
                "name": attachment["name"],
                "path": path,
                "success": False,
            }
            results.append(result)
            try:
                url = attachment_download_url(s, attachment)
                result["bytes"] = download_file(s, url, path)
                result["success"] = True
                print(f"  [ok] {attachment['name']} ({result['bytes']} bytes)")
            except RequestStopped:
                result["error"] = "访问停止，未完成下载"
                raise
            except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
                result["error"] = safe_error_text(error)
                print(f"  [x] {attachment['name']}: {result['error']}")
    finally:
        if args.result_json:
            write_json(
                args.result_json,
                {
                    "files": results,
                    "success": sum(r["success"] for r in results),
                    "failed": sum(not r["success"] for r in results),
                    "skipped": len(targets) - len(results),
                    "total": len(targets),
                },
            )
        s.close()
    print(f"附件下载: 成功 {sum(r['success'] for r in results)} / {len(targets)} -> {args.out}")


UPLOAD_DOMAIN = "https://mooc1-api.chaoxing.com/upload-ans"


IMG_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def _cached_index(question, fallback):
    index = question.get("index", fallback)
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("题目缓存中的 index 必须是非负整数")
    return index


def cmd_verify(args):
    """回读服务器上已存的作答内容 (含附件 base64 解码), 交卷前用它确认。"""
    s = load_session(args.session)
    questions = []
    if args.work_json and os.path.exists(args.work_json):
        questions = [
            q for q in read_json(args.work_json).get("questions", []) if q.get("questionId")
        ]
    targets = [
        (_cached_index(q, pos), str(q["questionId"])) for pos, q in enumerate(questions)
    ] or [(0, None)]
    for idx, expected_id in targets:
        html = fetch_index(s, args.dohomework_url, idx)
        fields = extract_hidden_fields(html)
        qid = _question_id(html)
        if expected_id is not None and expected_id != str(qid):
            print(f"   [!] 题序已变化: 缓存题号 {expected_id}, 实际题号 {qid}")
        val = fields.get(f"answer{qid}", "")
        state = "已作答" if "已作答" in html else "未作答"
        print(f"--- index={idx} qid={qid} 答案{len(val)}字符 页面标记={state} ---")
        if not val:
            print("   (空)")
        else:
            fn = re.search(r'filename="([^"]+)"', val)
            if fn:
                print(f"   附文件名: {html_lib.unescape(fn.group(1))}")
            for m in re.finditer(r'name="([A-Za-z0-9+/=]{16,})"', val):
                try:
                    print("   附件元数据:", unquote(base64.b64decode(m.group(1)).decode()))
                except Exception as e:
                    # 不要静默吞异常: 之前这里藏了个 NameError, 导致元数据一直不打印
                    print(f"   [!] 附件元数据解析失败: {type(e).__name__}: {e}")
            img = re.search(r'class="ans-ued-img"[^>]*src="([^"]+)"', val)
            if img:
                print(f"   图片: {img.group(1)}")
            print(f"   原文: {val[:260]}")
        if not questions and not _question_id(html):
            break


def build_answer_html(oid: str, name: str, ext: str, resp=None) -> str:
    """按超星 UEditor 的格式拼答案 HTML: 图片走 <img>, 其它文件走附件 iframe。"""
    if ext in IMG_EXT:
        return (
            f'<p><img class="ans-ued-img" '
            f'src="https://p.ananas.chaoxing.com/star3/origin/{oid}"></p>'
        )
    r = resp or {}
    # 体积字段是 hsize("2 MB"); 退路才是 size/length
    size = r.get("hsize") or r.get("size") or r.get("length") or ""
    meta = {"objectid": oid, "name": name, "type": ext, "size": fmt_size(size)}
    # 超星原格式是紧凑的 {"objectid":"..","name":".."}, 不加空格
    payload = base64.b64encode(
        quote(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), safe="").encode()
    ).decode()
    escaped_name = html_lib.escape(name, quote=True)
    return (
        f'<p><br></p><div class="editor-iframe" contenteditable="false">'
        f'<iframe frameborder="0" scrolling="no" '
        f'src="/ananas/common-modules/attachment/insertCloud.html" '
        f'class="attach-iframe" module="insertAttach" filename="{escaped_name}" '
        f'filetype="{ext}" name="{payload}" '
        f'style="display: block; max-width: 620px; width: 100%; height: 74px;">'
        f"</iframe></div><p><br></p>"
    )


class UploadError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.upload_uncertain = uncertain


def check_upload_file(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("附件不存在或不是普通文件；尚未上传，请检查文件路径")
    if not os.path.splitext(os.path.basename(path))[1].lstrip("."):
        raise ValueError("附件缺少文件扩展名，平台可能无法识别；尚未上传，请先检查文件格式")
    with open(path, "rb") as source:
        if not source.read(1):
            raise ValueError("附件为空文件；尚未上传，请先生成完整内容")


def upload_uncertain(error):
    diagnostic = getattr(error, "diagnostic", {})
    if (
        diagnostic.get("remoteOutcome") == "not_sent"
        or getattr(error, "request_sent", None) is False
    ):
        return False
    return getattr(error, "upload_uncertain", False) or getattr(error, "upload_attempted", False)


def _post_upload(s, url, **kwargs):
    try:
        return s.post(url, **kwargs)
    except (requests.RequestException, RequestStopped, KeyboardInterrupt) as error:
        error.upload_attempted = True
        raise


def _upload_json(response):
    try:
        result = response.json()
    except ValueError:
        raise UploadError(
            "上传响应不是 JSON，无法确认附件是否已保存；不要自动重传，先核对官方页面。"
            "原始响应可能含鉴权信息，未写入错误提示。",
            uncertain=True,
        ) from None
    if not isinstance(result, dict):
        raise UploadError("上传响应结构异常，无法确认结果；先核对附件状态。", uncertain=True)
    return result


def _upload_context(page, *, pc=False):
    fields = {
        "uid": hidden_by_id(page, "userId" if pc else "uploadUid"),
        "enc2": hidden_by_id(page, "uploadEnc"),
        "t": hidden_by_id(page, "uploadTimeStamp" if pc else "currentTime"),
    }
    if not all(fields.values()):
        raise UploadError(
            "作业页面缺少上传所需的用户、签名或时间字段，尚未上传。"
            "请核对同一份作业的入口、登录状态及页面是否改版；不会用本机时间替代服务端签名时间。"
        )
    return fields


def _upload_object_id(result):
    oid = result.get("objectId") or result.get("objectid")
    if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid):
        raise UploadError("上传响应缺少有效的附件编号，结果未确认；不要自动重传。", uncertain=True)
    return oid


def upload_raw(s, do_url: str, path: str):
    """上传本地文件到超星, 返回 (原始响应 JSON, 文件名, 扩展名)。"""
    check_upload_file(path)
    page = fetch_index(s, do_url, 0)
    fields = _upload_context(page)
    url = f"{UPLOAD_DOMAIN}/edit/uploadBase64?" + urlencode({**fields, "uploadtype": "work"})
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    with open(path, "rb") as fh:
        resp = _post_upload(
            s,
            url,
            timeout=(30, 180),
            data={"fileType": ext},
            files={"filePart": (name, fh, "application/octet-stream")},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": do_url},
        )
    j = _upload_json(resp)
    if j.get("status") is False:
        raise UploadError(
            "服务端明确拒绝上传（status=false）；检查附件类型、大小和上传上下文后再处理。"
        )
    if j.get("status") is not True:
        raise UploadError("上传响应没有明确成功状态；先核对附件，勿自动重传。", uncertain=True)
    _upload_object_id(j)
    return j, name, ext


def upload_pc_raw(s, pc_url: str, path: str):
    """Explicit PC form support; never used as an automatic retry/fallback."""
    check_upload_file(path)
    if os.path.getsize(path) > 500 * 1024 * 1024:
        raise ValueError("附件超过电脑端单文件 500 MB 限制；尚未上传，请先缩小文件")
    official_url(pc_url)
    parsed = urlparse(pc_url)
    expected = {key.lower(): values[0] for key, values in parse_qs(parsed.query).items()}
    ids = ("courseid", "classid", "workid")
    if (
        parsed.hostname != "mooc1.chaoxing.com"
        or parsed.path not in {"/mooc-ans/mooc2/work/task", "/mooc-ans/mooc2/work/dowork"}
        or any(not expected.get(key, "").isdigit() for key in ids)
    ):
        raise UploadError("电脑端上传需要作业列表提供的完整链接，且包含课程、班级和作业编号。")
    response = s.get(pc_url, timeout=30)
    current = {
        key.lower(): values[0] for key, values in parse_qs(urlparse(response.url).query).items()
    }
    if any(current.get(key) != expected[key] for key in ids):
        raise UploadError("电脑端页面跳转后课程、班级或作业不匹配，已停止，尚未上传。")
    page = response.text
    if not any(
        re.fullmatch(r"answer\d+", node.attrs.get("name", ""))
        for node in parse_html(page).walk("textarea")
    ):
        raise UploadError(
            "电脑端页面没有可编辑的作答区域，作业可能已交、过期或入口关闭；尚未上传。"
        )
    if not re.search(
        r'fileUrl\s*=\s*ServerHost\.uploadDomain\s*\+\s*["\']'
        r'/ueditorupload/attachment\?uploadtype=work["\']',
        page,
    ):
        raise UploadError("电脑端页面未找到已支持的附件表单；页面可能改版或入口已关闭，尚未上传。")
    fields = _upload_context(page, pc=True)
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    size = os.path.getsize(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    url = f"{UPLOAD_DOMAIN}/ueditorupload/attachment?" + urlencode(
        {"uploadtype": "work", "source": "1", **fields}
    )
    with open(path, "rb") as source:
        result = _upload_json(
            _post_upload(
                s,
                url,
                timeout=(30, 180),
                files={"upfile": (name, source, mime)},
                data={"id": "WU_FILE_0", "name": name, "size": str(size), "type": mime},
                headers={
                    "Referer": response.url,
                    "Origin": "https://mooc1.chaoxing.com",
                    "Accept": "*/*",
                },
            )
        )
    if result.get("state") != "SUCCESS":
        raise UploadError(
            "电脑端上传没有返回 SUCCESS；附件状态未确认，请先核对官方页面。", uncertain=True
        )
    result_url = result.get("url")
    if not isinstance(result_url, str):
        raise UploadError("上传响应缺少附件地址，结果未确认。", uncertain=True)
    result["objectId"] = parse_qs(urlparse(result_url).query).get("objectId", [""])[0]
    _upload_object_id(result)
    returned_type = result.get("fileType")
    if (
        result.get("original") != name
        or str(result.get("size")) != str(size)
        or not isinstance(returned_type, str)
        or returned_type.lstrip(".").lower() != ext
    ):
        raise UploadError(
            "服务器返回的文件名、大小或类型与本地文件不符；保留文件并核对，勿自动重传。",
            uncertain=True,
        )
    return result, name, ext


def upload_answer_file(s, do_url: str, path: str):
    """把本地文件上传到超星, 返回 (objectid, 可塞进答案的 HTML 片段)。"""
    j, name, ext = upload_raw(s, do_url, path)
    oid = j.get("objectId") or j.get("objectid") or ""
    return oid, build_answer_html(oid, name, ext, j)


def post_answer(s: requests.Session, payload: dict, temp_save: bool) -> dict:
    url = (
        "https://mooc1-api.chaoxing.com/mooc-ans/work/phone/doNormalHomeWorkSubmit"
        f"?tempSave={'true' if temp_save else 'false'}"
    )
    r = s.post(
        url,
        data=payload,
        timeout=30,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json,*/*",
            "Referer": payload.get("referer", "https://mooc1-api.chaoxing.com/work/stu-work"),
        },
    )
    try:
        response = r.json()
    except ValueError:
        raise RuntimeError("作答响应非 JSON，无法确认是否保存/提交成功") from None
    if not isinstance(response, dict) or "status" not in response:
        raise RuntimeError("作答响应缺少状态，无法确认是否保存/提交成功")
    return response


def format_answer(ans: str) -> str:
    """答案预处理。

    - `@相对/绝对路径`  -> 上传本地文件 (附件题必需), 交给 upload_answer_file 处理
    - `plain:多行文本`  -> 包成 <p>...</p> (UEditor 存的是 HTML)
    - 其它             -> 原样发送

    刻意**不**按题型自动包 HTML: 选择题的答案是个 token (如 "A"), 包成 <p>A</p>
    有可能影响判分。所以要不要包 HTML 由调用方用 `plain:` 显式声明。
    """
    if not isinstance(ans, str):
        return ans
    if ans.startswith("@"):
        return ans
    if ans.startswith("plain:"):
        lines = [ln for ln in ans[len("plain:") :].splitlines() if ln.strip()]
        return "".join(f"<p>{html_lib.escape(ln)}</p>" for ln in lines) or "<p></p>"
    return ans


def collect_question_fields(s, do_url, wanted, work=None, max_index=40, sleep=0.4):
    """Resolve answer fields by the actual page ID, reusing pages from cache hints."""
    wanted = set(wanted)
    fields, pages = {}, {}

    def visit(index):
        if index not in pages:
            pages[index] = fetch_index(s, do_url, index)
            time.sleep(sleep)
        html = pages[index]
        qid = _question_id(html)
        if qid in wanted and qid not in fields:
            fields[qid] = extract_hidden_fields(html)
        return qid

    for index, question in enumerate((work or {}).get("questions", [])):
        if str(question.get("questionId", "")) in wanted - fields.keys():
            visit(_cached_index(question, index))
    if wanted - fields.keys():
        previous, duplicates = None, 0
        for index in range(max_index):
            qid = visit(index)
            if not wanted - fields.keys() or not qid:
                break
            duplicates = duplicates + 1 if qid == previous else 0
            if duplicates >= 2:
                break
            previous = qid
    missing = sorted(wanted - fields.keys())
    if missing:
        die(f"这些题号没抓到 hidden 字段, 无法提交: {missing}")
    return fields


def _persist_write_results(path, results):
    if not path:
        return
    try:
        write_json(path, results)
    except OSError:
        accepted = [qid for qid, result in results.items() if result.get("status") is True]
        raise RuntimeError(
            f"本地作答结果文件写入失败；已收到服务端成功响应的题号: {accepted}。"
            "该本地错误不撤销远端操作，请先回读答案及作业状态，勿重复提交。"
        ) from None


def do_save_or_submit(args, temp_save: bool):
    if not temp_save and not args.confirm:
        die("交卷需要显式确认: 加 --confirm")

    answers = read_json(args.answers)
    if not isinstance(answers, dict) or not answers:
        die("答案必须是非空的 {题号: 答案} JSON 对象")
    if any(value is not None and not isinstance(value, str) for value in answers.values()):
        die("答案必须是字符串或 null；多选题请用 AB 这样的字符串")
    answers = {str(key): value or "" for key, value in answers.items()}
    for qid, answer in answers.items():
        if answer.startswith("@"):
            try:
                check_upload_file(answer[1:].strip())
            except (OSError, ValueError) as error:
                save_output(
                    args.out,
                    {
                        qid: {
                            "status": None,
                            "stage": "prepare",
                            "uncertain": False,
                            "attachmentUncertain": False,
                            "msg": safe_error_text(error),
                        }
                    },
                )
                die(f"题号 {qid} 的本地附件检查失败，尚未发送任何答案: {safe_error_text(error)}")
    work = read_json(args.work_json) if args.work_json and os.path.exists(args.work_json) else None
    s = load_session(args.session)
    qid_fields = collect_question_fields(
        s, args.dohomework_url, answers, work, args.brute_max, args.sleep
    )

    results = {}
    for qid, ans in answers.items():
        ans = format_answer(ans)
        stage = "prepare"
        attachment = None
        try:
            if ans.startswith("@"):
                path = ans[1:].strip()
                print(f"[UPLOAD] qid={qid} <- {path}")
                stage = "upload"
                oid, ans = upload_answer_file(s, args.dohomework_url, path)
                attachment = {"objectId": oid, "html": ans}
                print(f"         objectid={oid}  答案 HTML {len(ans)} 字符")
            payload = dict(qid_fields[qid])
            payload["tempSave"] = "true" if temp_save else "false"
            payload[f"answer{qid}"] = ans
            stage = "save" if temp_save else "submit"
            resp = post_answer(s, payload, temp_save)
        except (
            RequestStopped,
            requests.RequestException,
            OSError,
            ValueError,
            RuntimeError,
            KeyboardInterrupt,
        ) as error:
            diagnostic = getattr(error, "diagnostic", None)
            uncertain = (
                stage in {"save", "submit"}
                and (not diagnostic or diagnostic.get("remoteOutcome") != "not_sent")
                and getattr(error, "request_sent", None) is not False
            )
            results[qid] = {
                "status": None,
                "msg": safe_error_text(error),
                "stage": stage,
                "uncertain": uncertain,
                "attachmentUncertain": upload_uncertain(error),
            }
            if diagnostic:
                results[qid]["diagnostic"] = diagnostic
            if attachment:
                results[qid]["uploadedAttachment"] = attachment
            _persist_write_results(args.out, results)
            if isinstance(error, (RequestStopped, KeyboardInterrupt)):
                raise
            hint = (
                "请用 verify 回读后再操作"
                if uncertain
                else "本题答案尚未发送，请处理当前阶段的问题"
            )
            die(f"题号 {qid} 在 {stage} 阶段停止；{hint}: {safe_error_text(error)}")
        results[qid] = {**resp, "uploadedAttachment": attachment} if attachment else resp
        print(
            f"[{'SAVE' if temp_save else 'SUBMIT'}] qid={qid} "
            f"status={resp.get('status')} msg={safe_server_message(resp.get('msg'))}"
        )
        if resp.get("status") is not True:
            _persist_write_results(args.out, results)
            die(f"题号 {qid} 保存/提交失败，已停止: {safe_server_message(resp.get('msg', ''))}")
        if args.out:
            _persist_write_results(args.out, results)
        time.sleep(args.sleep)

    _persist_write_results(args.out, results)
    if args.out:
        print(f"[+] 作答结果 -> {args.out}")
    return results


def cmd_save_or_submit(args):
    do_save_or_submit(args, args.temp_save)


def cmd_upload(args):
    out = getattr(args, "out", "")
    if out and os.path.exists(out):
        raise ValueError(
            "上传结果文件已存在，请先检查已有附件记录；需要新的上传时使用新的输出路径。"
        )
    if out and not os.path.isdir(os.path.dirname(os.path.abspath(out))):
        raise ValueError("上传结果目录不存在，请先创建目录；尚未上传。")
    try:
        check_upload_file(args.file)
        uploader = upload_pc_raw if getattr(args, "pc_work_url", None) else upload_raw
        url = getattr(args, "pc_work_url", None) or args.dohomework_url
        response, name, ext = uploader(load_session(args.session), url, args.file)
    except (
        RequestStopped,
        requests.RequestException,
        OSError,
        ValueError,
        RuntimeError,
        KeyboardInterrupt,
    ) as error:
        if out:
            result = {
                "status": None,
                "operation": "upload",
                "uncertain": upload_uncertain(error),
                "msg": safe_error_text(error),
            }
            if isinstance(getattr(error, "diagnostic", None), dict):
                result["diagnostic"] = error.diagnostic
            try:
                write_json(out, result)
            except OSError:
                print("[!] 上传异常记录无法写入本地文件，请保留当前错误提示。", file=sys.stderr)
        raise
    oid = response.get("objectId") or response.get("objectid") or ""
    result = {
        "raw": response,
        "objectId": oid,
        "html": build_answer_html(oid, name, ext, response),
        "name": name,
        "status": True,
    }
    if out:
        try:
            write_json(out, result)
        except OSError:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise RuntimeError(
                "附件已上传成功，但本地结果文件写入失败；请保留上面的附件记录，勿重复上传。"
            ) from None
        print(f"[+] 附件已上传，记录 -> {out}；尚未保存答案或交卷。")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
