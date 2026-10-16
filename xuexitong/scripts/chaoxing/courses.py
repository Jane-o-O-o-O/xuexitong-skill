"""Courses operations for the Chaoxing client."""

import html as html_lib
import re
import time

import requests

from .common import die, hidden_fields_by_id, save_output, strip_tags, write_text
from .session import get_fid, load_session


def list_courses(s: requests.Session) -> list:
    fid = get_fid(s)
    url = (
        "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        f"?courseType=1&courseFid={fid}"
    )
    html = s.get(url, timeout=30).text
    courses, seen = [], set()
    # 必须按「课程卡片」切分, 不能按 </li>:
    # courseId 在卡片内的操作按钮里也出现 (移动到/置顶), 按 </li> 切会切到按钮那块,
    # 结果课程名被解析成"移动到"。课程名在 <span class="course-name" title="...">。
    for card in re.split(r'<div class="course[ "]', html)[1:]:
        m = re.search(r"courseid=(\d+)[^\"']*?clazzid=(\d+)[^\"']*?cpi=(\d+)", card)
        if not m:
            continue
        courseid, clazzid, cpi = m.group(1), m.group(2), m.group(3)
        if courseid in seen:
            continue
        seen.add(courseid)
        nm = re.search(r'<span class="course-name[^"]*"[^>]*title="([^"]*)"', card) or re.search(
            r'<span class="course-name[^"]*"[^>]*>([^<]*)<', card
        )
        name = html_lib.unescape(nm.group(1)).strip() if nm else strip_tags(card)[:40]
        courses.append({"courseId": courseid, "clazzId": clazzid, "cpi": cpi, "name": name})
    return courses


def find_course(s, keyword):
    courses = list_courses(s)
    matches = [c for c in courses if keyword in c["name"] or keyword == c["courseId"]]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        die(f"课程匹配不唯一，请使用 courseId: {[(c['courseId'], c['name']) for c in matches]}")
    die(f"没找到课名包含「{keyword}」的课程")


def course_middle(s: requests.Session, courseid: str, clazzid: str, cpi: str) -> dict:
    _, fields = open_course(s, courseid, clazzid, cpi)
    return {name: fields.get(name, "") for name in ("enc", "openc", "oldenc", "workEnc", "examEnc")}


def open_course(s, courseid, clazzid, cpi):
    """打开课程中间页, 返回 (HTML, 隐藏域 dict)。隐藏域里是各板块的 enc。"""
    html = s.get(
        "https://mooc1.chaoxing.com/visit/stucoursemiddle"
        f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}"
        f"&ismooc2=1&v=2",
        timeout=30,
    ).text
    return html, hidden_fields_by_id(html)


SECTION_RE = re.compile(
    r'<div class="chapter_item"\s+id="cur(\d+)"[^>]*'
    r'title="([^"]*)"'
)


def parse_chapters(html: str) -> dict:
    prog = re.search(r"已完成任务点:\s*<span[^>]*>(\d+)</span>\s*/\s*(\d+)", html)
    result = {
        "done": int(prog.group(1)) if prog else None,
        "total": int(prog.group(2)) if prog else None,
        "globalProgressAvailable": bool(prog),
        "chapters": [],
    }
    for unit in re.split(r'<div class="chapter_unit">', html)[1:]:
        m = re.search(
            r'<div class="catalog_num[^"]*">[\s\S]{0,140}?'
            r"<em[^>]*>(\d+)</em>",
            unit,
        )
        num = m.group(1) if m else ""
        nm = re.search(r'<div class="catalog_name[^"]*"[^>]*>([\s\S]{0,400}?)</div>', unit)
        name = ""
        if nm:
            t = re.search(r'title="([^"]*)"', nm.group(1))
            name = html_lib.unescape(t.group(1)) if t else strip_tags(nm.group(1))

        # 按「下一个 chapter_item」切窗, 避免把下一节的标记算到本节头上
        marks = list(SECTION_RE.finditer(unit))
        sections = []
        for i, sm in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(unit)
            win = unit[sm.start() : end]
            bar = re.search(r'class="catalog_sbar">([^<]*)<', win)
            pts = re.search(r'class="catalog_points_yi">(\d+)<', win)
            pending = int(pts.group(1)) if pts else 0
            icon_done = bool(re.search(r'class="catalog_state[^"]*icon_yiwanc', win))
            # 两套格式统一: icon_yiwanc=已完成 (旧), catalog_points_yi==0=已完成 (新)
            done = icon_done or (pts is not None and pending == 0)
            sections.append(
                {
                    "chapterId": sm.group(1),
                    "number": html_lib.unescape(bar.group(1)).strip() if bar else "",
                    "name": html_lib.unescape(sm.group(2)),
                    "pendingTaskPoints": pending,
                    "done": done,
                    "hasTask": icon_done or pts is not None,
                }
            )
        result["chapters"].append({"number": num, "name": name, "sections": sections})

    # 格式 B: 页面没给全局计数, 退化为按节统计
    if not prog:
        secs = [s for c in result["chapters"] for s in c["sections"]]
        withtask = [s for s in secs if s["hasTask"]]
        result["sectionsWithTask"] = len(withtask)
        result["sectionsDone"] = sum(1 for s in withtask if s["done"])
        result["pendingTaskPoints"] = sum(s["pendingTaskPoints"] for s in secs)
    return result


def list_chapters(s, courseid, clazzid, cpi, enc="", openc=""):
    url = (
        "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
        f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&enc={enc}"
        f"&openc={openc}&fromMiddle=1&ut=s"
    )
    html = s.get(url, timeout=40).text
    return parse_chapters(html), html


def cmd_chapters(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    _, f = open_course(s, c["courseId"], c["clazzId"], c["cpi"])
    data, html = list_chapters(
        s, c["courseId"], c["clazzId"], c["cpi"], f.get("enc", ""), f.get("openc", "")
    )
    if args.save_html:
        write_text(args.save_html, html)
    print(f"《{c['name']}》 章节 / 任务点")
    if data["globalProgressAvailable"]:
        d, t = data["done"], data["total"]
        print(f"已完成任务点: {d}/{t}" + (f"  ({d / t * 100:.1f}%)" if t else ""))
    else:
        print(
            f"该课程页面不提供全局任务点计数, 按节统计: "
            f"{data.get('sectionsDone', 0)}/{data.get('sectionsWithTask', 0)} 节已完成"
            f"   待完成任务点 {data.get('pendingTaskPoints', 0)}"
        )
    print()
    for ch in data["chapters"]:
        pend = sum(x["pendingTaskPoints"] for x in ch["sections"])
        tail = f"   [待完成 {pend}]" if pend else ""
        print(f"  {ch['number']:>3} {ch['name']}{tail}")
        for sec in ch["sections"]:
            if not sec["hasTask"]:
                mark = "-"
            elif sec["done"]:
                mark = "已完成"
            else:
                mark = f"待完成 {sec['pendingTaskPoints']}"
            print(f"       {sec['number']:<8} {sec['name'][:44]:<46} {mark}")
    n_sec = sum(len(x["sections"]) for x in data["chapters"])
    print(f"\n共 {len(data['chapters'])} 章, {n_sec} 节")
    save_output(args.out, data)


def section_urls(V, courseid, clazzid, cpi):
    """按 course-stu.js 的逻辑拼出考试 / 讨论的 URL。"""
    t = V.get("t") or str(int(time.time() * 1000))
    enc = V.get("enc", "")
    exam = (
        "https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list"
        f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s&t={t}"
        f"&stuenc={enc}&enc={V.get('examEnc', '')}"
        f"&openc={V.get('openc', '')}"
    )
    if V.get("courseApp") == "2" and V.get("bbsUrlSwitch") == "false":
        q = f"?courseId={courseid}&clazzid={clazzid}&ut=s&enc={V.get('oldenc', '')}&cpi={cpi}"
    else:
        q = (
            f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s&t={t}"
            f"&stuenc={enc}&bbsid={V.get('bbsid', '')}"
            f"&showAIEnc={V.get('showAIEnc', '')}"
            f"&supportAI={V.get('showAiAssistantTea', '')}&enc={enc}"
        )
    return exam, "https://groupweb.chaoxing.com/course/topic/topicList" + q


def parse_exams(html: str) -> list:
    """考试行: <div onclick="viewExamAnswer('<examId>','<recordId>')">
    ... <p class="overHidden2 fl">名称</p> ... <p class="status fl">状态</p>"""
    items = []
    matches = list(re.finditer(r"viewExamAnswer\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)", html))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        win = html[m.start() : end]
        nm = re.search(r'<p class="[^"]*overHidden2[^"]*"[^>]*>([\s\S]*?)</p>', win)
        st = re.search(r'<p class="[^"]*status[^"]*"[^>]*>([\s\S]*?)</p>', win)
        items.append(
            {
                "examId": m.group(1),
                "recordId": m.group(2),
                "name": strip_tags(nm.group(1)) if nm else "",
                "status": strip_tags(st.group(1)) if st else "",
            }
        )
    return items


def parse_topics(html: str) -> list:
    """讨论话题。列表在 <ul class="dataBody"> 内, 话题链接形如
    .../pc/topic/jumpToTopicDetail?bbsid=X&uuid=Y。
    模板 <script id="topic"> 里也有同样的链接骨架, 必须先剥掉 script。"""
    body = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
    ul = re.search(r'<ul class="dataBody"[^>]*>([\s\S]*?)</ul>', body)
    if not ul:
        return []
    items = []
    for m in re.finditer(
        r'<a\b[^>]*href="([^"]*jumpToTopicDetail[^"]*)"[\s\S]{0,1200}?</a>', ul.group(1)
    ):
        u = html_lib.unescape(m.group(1))
        uuid = re.search(r"uuid=([^&\"']*)", u)
        items.append(
            {
                "uuid": uuid.group(1) if uuid else "",
                "url": u,
                "text": strip_tags(m.group(0))[:200],
            }
        )
    return items


def cmd_exams(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    _, V = open_course(s, c["courseId"], c["clazzId"], c["cpi"])
    url, _ = section_urls(V, c["courseId"], c["clazzId"], c["cpi"])
    html = s.get(url, timeout=40).text
    if args.save_html:
        write_text(args.save_html, html)
    items = parse_exams(html)
    print(f"《{c['name']}》 考试  共 {len(items)} 场")
    for i, it in enumerate(items, 1):
        print(f"  {i:>3}. {it['name'][:56]:<58} {it['status']}")
    if not items:
        print("       (无考试)")
    save_output(args.out, items)


def cmd_topics(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    _, V = open_course(s, c["courseId"], c["clazzId"], c["cpi"])
    _, url = section_urls(V, c["courseId"], c["clazzId"], c["cpi"])
    html = s.get(url, timeout=40, headers={"Referer": "https://groupweb.chaoxing.com/"}).text
    if args.save_html:
        write_text(args.save_html, html)
    if "非法请求" in html or len(html) < 3000:
        die(f"讨论区请求被拒 (可能限流, 稍后重试): {strip_tags(html)[:80]}")
    items = parse_topics(html)
    print(f"《{c['name']}》 讨论区  共 {len(items)} 条话题")
    for i, it in enumerate(items, 1):
        print(f"  {i:>3}. {it['text'][:70]}")
    if not items:
        print("       (本班还没有话题)")
    save_output(args.out, items)


def cmd_courses(args):
    courses = list_courses(load_session(args.session))
    import json

    print(json.dumps(courses, ensure_ascii=False, indent=2))
    save_output(args.out, courses)
