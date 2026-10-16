"""Student operations for the Chaoxing client."""

import json
import re
import time

from .common import die, save_output, strip_tags, write_text
from .courses import find_course, list_courses, open_course, parse_exams, section_urls
from .homework import fetch_stu_work
from .local import RequestStopped
from .session import get_fid, load_session

NOTICE_API = "https://notice.chaoxing.com/pc/notice/getNoticeList"


KB_API = "https://kb.chaoxing.com/pc/curriculum/getMyLessons"


ACTIVE_API = "https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist"


def fetch_notices(s, page_size=20):
    r = s.post(
        NOTICE_API,
        timeout=40,
        data={
            "type": "0",
            "notice_type": "0",
            "lastValue": "",
            "sort": "0",
            "pageSize": str(page_size),
        },
    )
    return (r.json().get("notices") or {}).get("list") or []


def parse_notices(raw: list) -> list:
    """通知条目。attachment 字段是 JSON 字符串, 里面带作业/考试的跳转链接。"""
    items = []
    for it in raw:
        atts = []
        try:
            for a in json.loads(it.get("attachment") or "[]"):
                w = a.get("att_web") or {}
                atts.append(
                    {
                        "kind": a.get("attachmentType"),
                        "title": w.get("title") or a.get("examOrTextId"),
                        "url": w.get("url", ""),
                        "courseId": w.get("courseId"),
                        "clazzId": w.get("clazzId"),
                    }
                )
        except Exception:
            pass
        items.append(
            {
                "title": strip_tags(str(it.get("title") or "")),
                "time": it.get("completeTime"),
                "creater": it.get("createrName"),
                "isread": it.get("isread"),
                "content": re.sub(r"[\r\n]+", " ", str(it.get("content") or "")).strip(),
                "attachments": atts,
            }
        )
    return items


def fetch_schedule(s, week=None):
    data = {"week": str(week)} if week is not None else {}
    return s.post(KB_API, timeout=40, data=data).json().get("data") or {}


def fetch_activities(s, fid, courseid, clazzid):
    r = s.get(
        ACTIVE_API,
        timeout=40,
        params={"fid": fid, "courseId": courseid, "classId": clazzid, "showNotStartedActive": 1},
    )
    return ((r.json().get("data") or {}).get("activeList")) or []


def cmd_notices(args):
    s = load_session(args.session)
    raw = fetch_notices(s, args.limit)
    items = parse_notices(raw)
    if args.unread_only:
        items = [x for x in items if str(x["isread"]) == "0"]
    if args.keyword:
        items = [x for x in items if args.keyword in x["title"] or args.keyword in x["content"]]
    unread = sum(1 for x in items if str(x["isread"]) == "0")
    print(f"通知 / 公告  共 {len(items)} 条   未读 {unread}")
    for i, x in enumerate(items, 1):
        mark = "●" if str(x["isread"]) == "0" else " "
        print(f"  {i:>3}.{mark} {x['title'][:44]:<46} {x['time']}")
        for a in x["attachments"]:
            print(f"        └ {a['title']}  <- {a['url'][:80]}")
    save_output(args.out, items)


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def week_matches(weeks_str, week):
    """服务端 week 参数不可靠，按 weeks 中的周数或范围在本地过滤。"""
    if week is None or not weeks_str:
        return True
    week = _as_int(week)
    if week is None:
        return False
    for part in str(weeks_str).split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                if int(a) <= week <= int(b):
                    return True
            except ValueError:
                continue
        elif part.isdigit() and int(part) == week:
            return True
    return False


def cmd_schedule(args):
    s = load_session(args.session)
    d = fetch_schedule(s)
    cur = d.get("curriculum") or {}
    la = d.get("lessonArray") or []
    nw = cur.get("realCurrentWeek")
    week = args.week if args.week is not None else _as_int(nw)
    la = [x for x in la if week_matches(x.get("weeks"), week)]
    label = f"查询第 {args.week} 周" if args.week is not None else f"第 {nw} 周"
    print(f"课表   {label}   (当前第 {nw} 周 / 共 {cur.get('curriculumCount')} 周)")
    if not la:
        print("  (该周无课)")
    by_day = {}
    for x in la:
        by_day.setdefault(_as_int(x.get("dayOfWeek")), []).append(x)
    for day in sorted(by_day, key=lambda v: (v is None, v)):
        wd = "一二三四五六日"[day - 1] if isinstance(day, int) and 1 <= day <= 7 else "?"
        print(f"\n  周{wd}")
        for x in sorted(by_day[day], key=lambda v: _as_int(v.get("beginNumber"), 0)):
            b = _as_int(x.get("beginNumber"))
            ln = _as_int(x.get("length"), 1) or 1
            sect = f"第{b if b is not None else '?'}节"
            if ln > 1 and b is not None:
                sect = f"第{b}-{b + ln - 1}节"
            print(
                f"    {sect:<10} {str(x.get('name'))[:26]:<28} "
                f"{x.get('teacherName') or '':<8} @{x.get('location') or ''}"
            )
    save_output(args.out, {"currentWeek": nw, "week": args.week, "lessons": la})


def cmd_todos(args):
    """汇总未提交作业、课堂活动及可选的逐课考试扫描。"""
    s = load_session(args.session)
    result = {"works": [], "activities": None, "exams": None, "errors": []}
    include_activities = args.include_activities

    def record_error(section, error, course=None):
        detail = f"{type(error).__name__}: {error}"
        entry = {"section": section, "error": detail}
        if course is not None:
            entry["course"] = course
        result["errors"].append(entry)
        return detail

    courses = []
    if include_activities or args.deep:
        try:
            courses = list_courses(s)
            if args.course:
                courses = [
                    c for c in courses if args.course in c["name"] or args.course == c["courseId"]
                ]
                if not courses:
                    raise ValueError(f"没有匹配的课程: {args.course}")
        except RequestStopped:
            raise
        except Exception as e:
            courses = None
            record_error("courses", e)

    print("=" * 68)
    print("待办汇总")
    print("=" * 68)

    # 1) 未提交作业
    try:
        tasks, _ = fetch_stu_work(s)
        todo_work = result["works"] = [t for t in tasks if t["status"] == "未提交"]
        print(f"\n[作业] 未提交 {len(todo_work)} 项")
        for t in todo_work:
            tail = f"  {t['remain']}" if t["remain"] else ""
            print(f"       {t['name'][:36]:<38} 《{t['course']}》{tail}")
        if not todo_work:
            print("       (全部已提交)")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[作业] 失败: {record_error('works', e)}")

    if not include_activities and not args.deep:
        print("\n[课堂活动/考试] 未扫描")
        save_output(args.out, result)
        return

    # 2) 未完成的课堂活动
    if include_activities:
        try:
            if courses is None:
                raise RuntimeError("课程列表获取失败，无法扫描课堂活动")
            fid = get_fid(s)
            acts, fail = [], 0
            for c in courses:
                try:
                    for a in fetch_activities(s, fid, c["courseId"], c["clazzId"]):
                        acts.append({"course": c["name"], **a})
                except RequestStopped:
                    raise
                except Exception as e:
                    fail += 1
                    detail = record_error("activities", e, c["name"])
                    print(f"       [课堂活动]《{c['name']}》失败: {detail}")
                time.sleep(args.sleep)
            todo_act = result["activities"] = [a for a in acts if str(a.get("userStatus")) != "1"]
            print(
                f"\n[课堂活动] 待完成 {len(todo_act)} / 共 {len(acts)} 条"
                + (f"   (失败 {fail} 门)" if fail else "")
            )
            for a in todo_act[: args.limit]:
                print(
                    f"       {a['course'][:20]:<22} "
                    f"{str(a.get('nameTwo') or a.get('nameOne'))[:24]:<26} "
                    f"{a.get('nameFour')}"
                )
            if not todo_act:
                print(
                    "       (已扫描课程无待完成活动，扫描不完整)" if fail else "       (全部已完成)"
                )
        except RequestStopped:
            raise
        except Exception as e:
            print(f"\n[课堂活动] 失败: {record_error('activities', e)}")
    else:
        print("\n[课堂活动] 未扫描")

    # 3) 未完成的考试 (可选, 慢)
    if args.deep:
        result["exams"] = []
        try:
            if courses is None:
                raise RuntimeError("课程列表获取失败，无法扫描考试")
            todo_exam, fail = result["exams"], 0
            for c in courses:
                try:
                    _, V = open_course(s, c["courseId"], c["clazzId"], c["cpi"])
                    eu, _ = section_urls(V, c["courseId"], c["clazzId"], c["cpi"])
                    for x in parse_exams(s.get(eu, timeout=40).text):
                        if "已完成" not in x["status"]:
                            x["course"] = c["name"]
                            todo_exam.append(x)
                except RequestStopped:
                    raise
                except Exception as e:
                    fail += 1
                    detail = record_error("exams", e, c["name"])
                    print(f"       [考试]《{c['name']}》失败: {detail}")
                time.sleep(args.sleep)
            print(f"\n[考试] 未完成 {len(todo_exam)} 场" + (f"   (失败 {fail} 门)" if fail else ""))
            for x in todo_exam:
                print(f"       {x['course'][:20]:<22} {x['name'][:34]:<36} {x['status']}")
            if not todo_exam:
                print(
                    "       (已扫描课程无未完成考试，扫描不完整)" if fail else "       (全部已完成)"
                )
        except RequestStopped:
            raise
        except Exception as e:
            print(f"\n[考试] 失败: {record_error('exams', e)}")
    else:
        print("\n[考试] 未扫描 (加 --deep 逐课扫, 较慢)")
    save_output(args.out, result)


WRONGQ_PAGE = "https://mooc2-ans.chaoxing.com/mooc2-ans/wrongque/page"


WRONGQ_DATA = "https://mooc2-ans.chaoxing.com/mooc2-ans/wrongque/data"


STUDY_DATA = "https://stat2-ans.chaoxing.com/study-data/index"


def _clean_ans(t: str) -> str:
    """错题集的答案文本会带「我的答案：」前缀和尾部分隔符, 清掉。"""
    t = strip_tags(t)
    t = re.sub(r"^(我的答案|正确答案)\s*[:：]\s*", "", t)
    return t.strip(" :：;；、").strip()


def parse_wrongque(html: str) -> list:
    """错题集条目。每题含: 序号 / 题型 / 题干 / 我的答案 / 正确答案 / recordId。"""
    items = []
    for m in re.finditer(r'<div class="topic-list">([\s\S]*?)(?=<div class="topic-list">|$)', html):
        blk = m.group(1)
        num = re.search(r'class="topic-number[^"]*">([^<]*)<', blk)
        qt = re.search(r'class="fs15 colorShallow">\[([^\]]*)\]<', blk)
        # 题干: 每题标记不同 —— 有的无嵌套 span, 有的 1 层, 有的文本被拆成 2 个 span。
        # 不能只匹配到第一个 </span> (会截断), 要锚定到标题块的结束边界。
        stem = (
            re.search(r'class="topic-txt[^"]*">([\s\S]*?)(?=<div class="delete-con)', blk)
            or re.search(r'class="topic-txt[^"]*">([\s\S]*?)(?=<ul )', blk)
            or re.search(r'class="topic-txt[^"]*">([\s\S]*?)</span>', blk)
        )
        mine = re.search(r'answer-my[^"]*"[^>]*>([\s\S]*?)</li>', blk)
        corr = re.search(r'answer-default[^"]*"[^>]*>([\s\S]*?)</li>', blk)
        rid = re.search(r'recordId="(\d+)"', blk)
        items.append(
            {
                "number": strip_tags(num.group(1)) if num else "",
                "type": qt.group(1) if qt else "",
                "stem": strip_tags(stem.group(1)) if stem else "",
                "myAnswer": _clean_ans(mine.group(1)) if mine else "",
                "correctAnswer": _clean_ans(corr.group(1)) if corr else "",
                "recordId": rid.group(1) if rid else "",
            }
        )
    return items


def fetch_wrongque(s, courseid, clazzid, cpi, key_word="", topic_arr="", qtype=""):
    r = s.post(
        WRONGQ_DATA,
        params={"courseid": courseid, "clazzid": clazzid, "cpi": cpi, "ut": "s"},
        data={
            "clazzid": clazzid,
            "courseid": courseid,
            "cpi": cpi,
            "keyWord": key_word,
            "topicArr": topic_arr,
            "type": qtype,
            "stuId": "",
        },
        timeout=40,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    return r.text


def cmd_wrongque(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    html = fetch_wrongque(s, c["courseId"], c["clazzId"], c["cpi"], args.keyword, qtype=args.qtype)
    if args.save_html:
        write_text(args.save_html, html)
    items = parse_wrongque(html)
    if args.limit:
        items = items[: args.limit]
    print(f"《{c['name']}》 错题集  共 {len(items)} 题")
    for it in items:
        print(f"\n  {it['number']} [{it['type']}] {it['stem'][:70]}")
        print(f"      我的答案: {it['myAnswer'][:44]:<46} 正确答案: {it['correctAnswer'][:44]}")
    if not items:
        print("       (错题集为空)")
    save_output(args.out, items)


STUDY_STATS = [
    "job",
    "sign",
    "score",
    "work",
    "test",
    "live",
    "weekPv",
    "point",
    "groupTask",
    "self-exam",
    "videoTest",
    "cxclass",
]


def fetch_study_stats(s, courseid, clazzid, cpi, sleep=0.2, html=None):
    """统计接口须携带页面 pEnc 和小写 courseid/clazzid，不能带 enc/openc。"""
    if html is None:
        page = f"{STUDY_DATA}?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s"
        html = s.get(page, timeout=40).text
    m = re.search(r"var pEnc\s*=\s*'([^']*)'", html)
    if not m:
        die("学习记录页里没取到 pEnc, 统计接口无法调用 (可能该课无学习记录, 或登录态已失效)")
    penc = m.group(1)
    out, errors = {}, {}
    for ep in STUDY_STATS:
        try:
            r = s.get(
                f"https://stat2-ans.chaoxing.com/study-data/{ep}",
                params={
                    "clazzid": clazzid,
                    "courseid": courseid,
                    "cpi": cpi,
                    "ut": "s",
                    "pEnc": penc,
                },
                timeout=30,
            )
            value = r.json()
            if not isinstance(value, dict):
                raise ValueError("统计响应不是 JSON 对象")
            out[ep] = value
        except RequestStopped:
            raise
        except Exception as e:
            out[ep] = {}
            errors[ep] = f"{type(e).__name__}: {e}"
        time.sleep(sleep)
    if errors:
        out["errors"] = errors
    return out, html


def cmd_study(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    r = s.get(
        f"{STUDY_DATA}?courseid={c['courseId']}&clazzid={c['clazzId']}&cpi={c['cpi']}&ut=s",
        timeout=40,
    )
    if r.status_code != 200:
        die(f"学习记录返回 HTTP {r.status_code}: {strip_tags(r.text)[:80]}")
    if args.save_html:
        write_text(args.save_html, r.text)
    st, page_html = fetch_study_stats(
        s, c["courseId"], c["clazzId"], c["cpi"], args.sleep, html=r.text
    )

    def g(ep, *keys, default="-"):
        if ep in st.get("errors", {}):
            return "不可用"
        d = st.get(ep) or {}
        d = d.get("data") if isinstance(d.get("data"), dict) else d
        for k in keys:
            if k in d:
                return d[k]
        return default

    print(f"《{c['name']}》 学习记录")
    print("─" * 56)
    for ep, error in st.get("errors", {}).items():
        print(f"  [x] {ep}: {error}")
    print(
        f"  作业     {g('work', 'finishCount', default=0)}/{g('work', 'receivedNum', default=0)} 项"
        f"   完成率 {g('work', 'finishPer', default=0)}%"
        f"   **成绩 {g('work', 'workScore')}**"
    )
    print(
        f"  测验     共 {g('test', 'testNum', default=0)} 场"
        f"   已完成 {g('test', 'finishCount', default=0)}"
        f"   平均分 {g('test', 'testAvgScore')}"
    )
    print(
        f"  签到     出勤率 {g('sign', 'signPer', default=0)}%   "
        f"出勤 {g('sign', 'attendanceCount', default=0)} / 缺勤 {g('sign', 'absenceCount', default=0)}"
        f" / 迟到 {g('sign', 'lateCount', default=0)} / 总 {g('sign', 'allCount', default=0)}"
    )
    print(
        f"  任务点   完成率 {g('job', 'jobPer', default=0)}%   "
        f"班级排名 {g('job', 'jobRank')}   "
        f"{g('job', 'job', default=0)}/{g('job', 'publishJobNum', default=0)}"
    )
    print(
        f"  直播     观看 {g('live', 'livingTime', default=0)} 分   "
        f"回看 {g('live', 'playbackTime', default=0)} 分"
    )
    print(f"  积分     {g('point', 'ponits', default=0)}")
    print(f"  分组任务 完成率 {g('groupTask', 'taskPer', default=0)}%")
    print(
        f"  自测     {g('self-exam', 'quesCount', default=0)} 题   "
        f"正确率 {g('self-exam', 'objRightPercent', default=0)}%"
    )
    pv = (st.get("weekPv") or {}).get("data") or {}
    if pv.get("dates"):
        print(
            f"  学习次数 (近 {len(pv['dates'])} 天): "
            + ", ".join(
                f"{d}:{n}"
                for d, n in list(zip(pv["dates"], pv.get("counts", []), strict=False))[:7]
            )
        )
    for m in re.finditer(r'intelligentTips_font"[^>]*>\s*([^<]{4,120})<', page_html):
        t = strip_tags(m.group(1))
        if t:
            print(f"  ⚠ {t}")
    save_output(args.out, st)
