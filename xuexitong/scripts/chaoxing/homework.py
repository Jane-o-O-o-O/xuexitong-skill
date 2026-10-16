"""Homework operations for the Chaoxing client."""

import base64
import html as html_lib
import json
import os
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

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
from .courses import course_middle, list_courses
from .local import RequestStopped
from .session import cookie_get, load_session

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
    for mt in re.finditer(r'<li\b[^>]*\bdata="([^"]+)"[^>]*>([\s\S]*?)</li>', html, re.I):
        href = html_lib.unescape(mt.group(1)).strip()
        win = mt.group(2)
        title_m = re.search(r'title="([^"]+)"', win)
        title = html_lib.unescape(title_m.group(1)).strip() if title_m else strip_tags(win)[:60]
        wid_m = re.search(r"workId=(\d+)", href) or re.search(r"workId=(\d+)", win)
        wid = wid_m.group(1) if wid_m else ""
        if wid and wid in seen:
            continue
        if wid:
            seen.add(wid)
        text = strip_tags(win)
        labels = [
            strip_tags(match.group(1))
            for match in re.finditer(r"<span\b[^>]*>([\s\S]*?)</span>", win, re.I)
        ]
        status_text = next(
            (label for label in labels if label != title and label in PENDING_WORDS + DONE_WORDS),
            text.replace(title, "", 1),
        )
        works.append(
            {
                "course": course_name,
                "title": title,
                "workId": wid,
                "status": _classify_status(status_text),
                "statusText": text[:80],
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


TASK_LI_RE = re.compile(r'<li[^>]*\bdata="([^"]*mtaskmsgspecial[^"]*)"[^>]*>([\s\S]*?)</li>', re.I)


A11Y_RE = re.compile(r"作业名称(.*?)作业状态(.*?)所属课程(.*?)(?:剩余时间(.*))?$")


def parse_task_list(html: str) -> list:
    tasks, seen = [], set()
    for m in TASK_LI_RE.finditer(html):
        url = html_lib.unescape(m.group(1)).replace("&amp;", "&")
        if url in seen:
            continue
        seen.add(url)
        block = m.group(2)
        name = status = course = remain = ""

        nm = re.search(r"<p[^>]*>([\s\S]*?)</p>", block)
        if nm:
            name = strip_tags(nm.group(1))
        sm = re.search(r'<span[^>]*class="status"[^>]*>([\s\S]*?)</span>', block)
        if sm:
            status = strip_tags(sm.group(1))
        cm = re.search(r"《([^》]*)》", block)
        if cm:
            course = cm.group(1).strip()
        rm = re.search(r'<span[^>]*class="fr"[^>]*>([\s\S]*?)</span>', block)
        if rm:
            remain = strip_tags(rm.group(1))

        # aria-label 最可靠: 用来补全 / 纠正上面的正则 (待批阅和已完成只能靠它)
        lm = re.search(r'<span[^>]*aria-label="([^"]*)"', block)
        if lm:
            g = A11Y_RE.match(html_lib.unescape(lm.group(1)))
            if g:
                name = name or g.group(1)
                status = status or g.group(2)
                course = course or g.group(3)
                remain = remain or (g.group(4) or "")

        tid = re.search(r"taskrefId=(\d+)", url)
        tasks.append(
            {
                "taskRefId": tid.group(1) if tid else "",
                "name": name,
                "status": status,
                "course": course,
                "remain": remain,
                "taskUrl": url,
            }
        )
    return tasks


def fetch_stu_work(s: requests.Session):
    html = s.get("https://mooc1-api.chaoxing.com/work/stu-work", timeout=30).text
    return parse_task_list(html), html


def resolve_dohomework(s: requests.Session, task_url: str) -> str:
    html = s.get(task_url, timeout=30).text
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

    seen, dup = {qid0}, 0
    for i in range(1, max_index):
        time.sleep(sleep)
        qid = _question_id(fetch_index(s, do_url, i))
        if not qid:
            break
        if qid in seen:
            dup += 1
            if dup >= 2:
                break
            continue
        dup = 0
        seen.add(qid)
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
        tasks = [t for t in tasks if args.course in t["course"]]

    out = []
    for t in tasks:
        t = dict(t)
        t["doHomeWorkUrl"] = ""
        t["questionCount"] = None
        t["detailsResolved"] = False
        t["resolveError"] = ""
        try:
            if args.resolve:
                t["detailsResolved"] = True
                do = resolve_dohomework(s, t["taskUrl"])
                t["doHomeWorkUrl"] = do
                n, note = probe_task(s, do, args.max_index, args.sleep)
                t["questionCount"] = n
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
    print(json.dumps({"doHomeWorkUrl": url}, ensure_ascii=False))


def parse_question(html: str) -> dict:
    qid = _question_id(html)
    inputs = parse_inputs(html)
    fields = {item["name"]: item.get("value", "") for item in inputs if item.get("name")}
    stem = ""
    m = re.search(r'<div class="ans-cc[^"]*"[^>]*>([\s\S]*?)</div>', html)
    if m:
        stem = strip_tags(m.group(1))
    qtype = score = None
    if qid:
        qtype = fields.get(f"type{qid}")
        score = fields.get(f"score{qid}")

    # 选择题的候选值 —— 答案要填的就是这些 value
    options = [
        {"kind": item["type"].lower(), "value": item.get("value", "")}
        for item in inputs
        if item.get("type", "").lower() in {"radio", "checkbox"}
    ]
    return {"questionId": qid, "type": qtype, "score": score, "stem": stem, "options": options}


def cmd_fetch(args):
    s = load_session(args.session)
    questions, seen, base_fields = [], set(), None
    dup = 0
    for idx in range(args.max_index):
        html = fetch_index(s, args.dohomework_url, idx)
        if base_fields is None:
            base_fields = extract_hidden_fields(html)
        # 存原始页便于事后判断题型/选项格式 (未知题型的第一手材料)
        if args.save_html:
            os.makedirs(args.save_html, exist_ok=True)
            write_text(os.path.join(args.save_html, f"index{idx}.html"), html)
        q = parse_question(html)
        qid = q.get("questionId")
        if not qid:
            break
        if qid in seen:
            time.sleep(args.sleep)
            dup += 1
            if dup >= 2:
                break
            continue
        dup = 0
        seen.add(qid)
        questions.append(q)
        time.sleep(args.sleep)
    payload = {
        "doHomeWorkUrl": args.dohomework_url,
        "questions": questions,
        "baseFields": base_fields or {},
        "fetchedAt": int(time.time()),
    }
    write_json(args.out, payload)
    print(f"[+] 抓题完成: {len(questions)} 题 -> {args.out}")
    for q in questions:
        opt = f"  选项={[o['value'] for o in q['options']]}" if q["options"] else ""
        print(f"    qid={q['questionId']}  type={q['type']}  score={q['score']}{opt}")
        if q["stem"]:
            print(f"      题干: {q['stem'][:80]}")


def cmd_template(args):
    data = read_json(args.work_json)
    tmpl = {q["questionId"]: "" for q in data.get("questions", []) if q.get("questionId")}
    write_json(args.out, tmpl)
    print(f"[+] 答案模板 {len(tmpl)} 项 -> {args.out}  (填 answer 字段即可)")


UPLOAD_DOMAIN = "https://mooc1-api.chaoxing.com/upload-ans"


IMG_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def cmd_verify(args):
    """回读服务器上已存的作答内容 (含附件 base64 解码), 交卷前用它确认。"""
    s = load_session(args.session)
    qids = []
    if args.work_json and os.path.exists(args.work_json):
        qids = [
            q["questionId"]
            for q in read_json(args.work_json).get("questions", [])
            if q.get("questionId")
        ]
    n = max(len(qids), 1)
    for idx in range(n):
        html = fetch_index(s, args.dohomework_url, idx)
        fields = extract_hidden_fields(html)
        qid = _question_id(html)
        if idx < len(qids) and str(qids[idx]) != str(qid):
            print(f"   [!] 题序已变化: 缓存题号 {qids[idx]}, 实际题号 {qid}")
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
        if not qids and not _question_id(html):
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


def upload_raw(s, do_url: str, path: str):
    """上传本地文件到超星, 返回 (原始响应 JSON, 文件名, 扩展名)。"""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    page = fetch_index(s, do_url, 0)
    uid = hidden_by_id(page, "uploadUid") or cookie_get(s, "_uid") or ""
    enc = hidden_by_id(page, "uploadEnc")
    ts = hidden_by_id(page, "currentTime") or str(int(time.time() * 1000))
    url = f"{UPLOAD_DOMAIN}/edit/uploadBase64?uid={uid}&enc2={enc}&t={ts}&uploadtype=work"
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    with open(path, "rb") as fh:
        resp = s.post(
            url,
            timeout=180,
            data={"fileType": ext},
            files={"filePart": (name, fh, "application/octet-stream")},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": do_url},
        )
    try:
        j = resp.json()
    except ValueError:
        raise RuntimeError(f"上传响应非 JSON: {resp.text[:300]}") from None
    if not j.get("status") or not (j.get("objectId") or j.get("objectid")):
        raise RuntimeError(f"上传失败: {json.dumps(j, ensure_ascii=False)[:300]}")
    return j, name, ext


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
            visit(index)
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


def do_save_or_submit(args, temp_save: bool):
    if not temp_save and not args.confirm:
        die("交卷需要显式确认: 加 --confirm")

    answers = read_json(args.answers)
    if not isinstance(answers, dict) or not answers:
        die("答案必须是非空的 {题号: 答案} JSON 对象")
    if any(value is not None and not isinstance(value, str) for value in answers.values()):
        die("答案必须是字符串或 null；多选题请用 AB 这样的字符串")
    answers = {str(key): value or "" for key, value in answers.items()}
    work = read_json(args.work_json) if args.work_json and os.path.exists(args.work_json) else None
    s = load_session(args.session)
    qid_fields = collect_question_fields(
        s, args.dohomework_url, answers, work, args.brute_max, args.sleep
    )

    results = {}
    for qid, ans in answers.items():
        ans = format_answer(ans)
        try:
            if ans.startswith("@"):
                path = ans[1:].strip()
                print(f"[UPLOAD] qid={qid} <- {path}")
                oid, ans = upload_answer_file(s, args.dohomework_url, path)
                print(f"         objectid={oid}  答案 HTML {len(ans)} 字符")
            payload = dict(qid_fields[qid])
            payload["tempSave"] = "true" if temp_save else "false"
            payload[f"answer{qid}"] = ans
            resp = post_answer(s, payload, temp_save)
        except RequestStopped as error:
            results[qid] = {"status": None, "msg": str(error), "uncertain": True}
            save_output(args.out, results)
            raise
        except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
            results[qid] = {"status": None, "msg": str(error), "uncertain": True}
            save_output(args.out, results)
            die(f"题号 {qid} 结果未确认，已停止；请用 verify 回读后再操作: {error}")
        results[qid] = resp
        print(
            f"[{'SAVE' if temp_save else 'SUBMIT'}] qid={qid} "
            f"status={resp.get('status')} msg={resp.get('msg')}"
        )
        if resp.get("status") is not True:
            save_output(args.out, results)
            die(f"题号 {qid} 保存/提交失败，已停止: {resp.get('msg', '')}")
        if args.out:
            write_json(args.out, results)
        time.sleep(args.sleep)

    save_output(args.out, results)
    return results


def cmd_save_or_submit(args):
    do_save_or_submit(args, args.temp_save)


def cmd_upload(args):
    response, name, ext = upload_raw(load_session(args.session), args.dohomework_url, args.file)
    oid = response.get("objectId") or response.get("objectid") or ""
    print(
        json.dumps(
            {"raw": response, "objectId": oid, "html": build_answer_html(oid, name, ext, response)},
            ensure_ascii=False,
            indent=2,
        )
    )
