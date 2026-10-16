"""Overview operations for the Chaoxing client."""

from .courses import (
    find_course,
    list_chapters,
    open_course,
    parse_exams,
    parse_topics,
    section_urls,
)
from .homework import course_works
from .local import RequestStopped
from .materials import list_materials
from .session import load_session


def cmd_course(args):
    """一门课的全貌: 基本信息 + 章节进度 + 作业 + 资料。"""
    s = load_session(args.session)
    c = find_course(s, args.course)
    CID, CLAZZ, CPI = c["courseId"], c["clazzId"], c["cpi"]
    _, f = open_course(s, CID, CLAZZ, CPI)
    eu, tu = section_urls(f, CID, CLAZZ, CPI)

    print("=" * 68)
    print(f"《{c['name']}》")
    print("=" * 68)
    print(f"courseId={CID}  clazzId={CLAZZ}  cpi={CPI}")

    # 章节
    try:
        ch, _ = list_chapters(s, CID, CLAZZ, CPI, f.get("enc", ""), f.get("openc", ""))
        n_sec = sum(len(x["sections"]) for x in ch["chapters"])
        print(f"\n[章节] {len(ch['chapters'])} 章 / {n_sec} 节")
        if ch["globalProgressAvailable"]:
            d, t = ch["done"], ch["total"]
            print(f"       任务点 {d}/{t}" + (f"  ({d / t * 100:.1f}%)" if t else ""))
        else:
            print(
                f"       按节统计 {ch.get('sectionsDone', 0)}/"
                f"{ch.get('sectionsWithTask', 0)} 节已完成"
                f"   待完成任务点 {ch.get('pendingTaskPoints', 0)}"
            )
        pend = [
            (x["number"], x["name"], sum(y["pendingTaskPoints"] for y in x["sections"]))
            for x in ch["chapters"]
        ]
        pend = [p for p in pend if p[2] > 0]
        for num, name, n in pend[:8]:
            print(f"       待完成 {n:>3}  {num} {name[:40]}")
        if len(pend) > 8:
            print(f"       ... 还有 {len(pend) - 8} 章有未完成任务点")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[章节] 失败: {type(e).__name__}: {e}")

    # 作业
    try:
        works = course_works(s, c, mid=f)
        sub = [w for w in works if w.get("status") == "submitted"]
        pen = [w for w in works if w.get("status") == "pending"]
        print(f"\n[作业] 共 {len(works)} 项   已提交 {len(sub)}   未提交 {len(pen)}")
        for w in pen[:8]:
            print(f"       未提交  {w['title'][:52]}")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[作业] 失败: {type(e).__name__}: {e}")

    # 资料
    try:
        items, _, total = list_materials(s, CID, CLAZZ, CPI)
        nf = sum(1 for x in items if x["isFolder"])
        print(f"\n[资料] 根目录 {len(items)} 项 (目录 {nf} / 文件 {len(items) - nf})")
        for it in items[:8]:
            kind = f"[目录 {it['childCount']}]" if it["isFolder"] else f"({it['type']})"
            print(f"       {it['name'][:48]:<50}{kind}")
        if len(items) > 8:
            print(f"       ... 还有 {len(items) - 8} 项")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[资料] 失败: {type(e).__name__}: {e}")

    # 考试
    try:
        eh = s.get(eu, timeout=40).text
        ex = parse_exams(eh)
        done = sum(1 for x in ex if "已完成" in x["status"])
        print(f"\n[考试] 共 {len(ex)} 场   已完成 {done}")
        for x in ex[:8]:
            print(f"       {x['name'][:50]:<52}{x['status']}")
        if not ex:
            print("       (无考试)")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[考试] 失败: {type(e).__name__}: {e}")

    # 讨论
    try:
        th = s.get(tu, timeout=40, headers={"Referer": "https://groupweb.chaoxing.com/"}).text
        tp = parse_topics(th)
        print(f"\n[讨论] 共 {len(tp)} 条话题")
        for x in tp[:8]:
            print(f"       {x['text'][:60]}")
        if not tp:
            print("       (本班还没有话题)")
    except RequestStopped:
        raise
    except Exception as e:
        print(f"\n[讨论] 失败: {type(e).__name__}: {e}")
