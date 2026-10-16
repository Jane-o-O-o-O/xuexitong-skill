"""Materials operations for the Chaoxing client."""

import html as html_lib
import os
import re
import tempfile
import time
from contextlib import closing
from urllib.parse import quote

from .common import save_output, write_text
from .courses import find_course
from .local import RequestStopped
from .session import load_session

MAT_ROW_RE = re.compile(r'<ul[^>]*class="dataBody_td"[^>]*>', re.I)


MAT_TOTAL_RE = re.compile(r"当前页共\s*(\d+)\s*个")


def parse_materials(html: str) -> list:
    items = []
    rows = list(MAT_ROW_RE.finditer(html))
    for index, m in enumerate(rows):
        tag = m.group(0)

        def attr(n, _t=tag):
            a = re.search(rf'\b{n}="([^"]*)"', _t)
            return html_lib.unescape(a.group(1)) if a else ""

        # 无 dataName 的行是页面 JS 模板。
        if not attr("dataName"):
            continue

        end = rows[index + 1].start() if index + 1 < len(rows) else len(html)
        body = html[m.end() : end]

        def cell(cls, block=body):
            c = re.search(rf'class="{cls}"[^>]*>([^<]*)<', block)
            return html_lib.unescape(c.group(1)).strip() if c else ""

        items.append(
            {
                "dataId": attr("id"),
                "name": attr("dataName"),
                "type": attr("type"),
                "objectId": attr("objectId"),
                "isFolder": attr("type") == "afolder",
                "childCount": attr("vcount"),
                "canDownload": attr("isdown") == "1",
                "size": cell("dataBody_size_stu"),
                "creater": cell("dataBody_creater_stu"),
                "time": cell("dataBody_time_stu"),
            }
        )
    return items


def list_materials(s, courseid, clazzid, cpi, data_id="0", data_name="", enc="", parent="", page=1):
    url = (
        "https://mooc2-ans.chaoxing.com/mooc2-ans/coursedata/stu-datalist"
        f"?courseid={courseid}&clazzid={clazzid}&pages={page}&type=1"
        f"&dataName={quote(data_name)}&dataId={data_id}"
        f"&parent={quote(parent)}&order=&enc={enc}&cpi={cpi}"
        f"&t={int(time.time() * 1000)}&microTopicId="
    )
    html = s.get(url, timeout=30).text
    tot = MAT_TOTAL_RE.search(html)
    return parse_materials(html), html, (int(tot.group(1)) if tot else None)


def walk_materials(s, c, data_id="0", data_name="", depth=0, max_depth=4, sleep=0.3):
    """递归展开文件夹, 返回嵌套结构 (children)。"""
    items, _, _ = list_materials(
        s, c["courseId"], c["clazzId"], c["cpi"], data_id=data_id, data_name=data_name
    )
    for it in items:
        if not it["isFolder"]:
            continue
        if depth >= max_depth:
            it["children"] = None  # 达到深度上限, 标记未展开
        else:
            time.sleep(sleep)
            it["children"] = walk_materials(
                s, c, it["dataId"], it["name"], depth + 1, max_depth, sleep
            )
    return items


def print_tree(items, indent=0):
    pad = "  " * indent
    for it in items:
        if it["isFolder"]:
            print(f"{pad}[+] {it['name']}/  ({it.get('childCount') or '?'} 项)")
            kids = it.get("children")
            if kids is None:
                print(f"{pad}      (未展开, 超出 --max-depth)")
            elif not kids:
                print(f"{pad}      (空目录)")
            else:
                print_tree(kids, indent + 1)
        else:
            print(
                f"{pad}    {it['name']}  ({it['type']}"
                + (f", {it['size']}" if it["size"] else "")
                + ")"
            )


def count_tree(items):
    n_file = n_dir = 0
    for it in items:
        if it["isFolder"]:
            n_dir += 1
            if it.get("children"):
                f, d = count_tree(it["children"])
                n_file += f
                n_dir += d
        else:
            n_file += 1
    return n_file, n_dir


def cmd_materials(args):
    s = load_session(args.session)
    c = find_course(s, args.course)
    data_id, data_name, parent, enc = "0", "", "", ""
    if args.data_id:
        data_id, data_name, parent, enc = (args.data_id, args.data_name, args.parent, args.enc)

    if args.tree:
        items = walk_materials(s, c, data_id, data_name, 0, args.max_depth, args.sleep)
        n_file, n_dir = count_tree(items)
        where = f"  (从 {data_name} 起)" if args.data_id else ""
        print(f"《{c['name']}》 资料区 完整目录树{where}")
        print_tree(items)
        print(f"\n合计 {n_dir} 个目录, {n_file} 个文件")
    else:
        items, html, total = list_materials(
            s, c["courseId"], c["clazzId"], c["cpi"], data_id, data_name, enc, parent
        )
        if args.save_html:
            write_text(args.save_html, html)
        where = f"  (文件夹 {data_name} id={data_id})" if args.data_id else "  (根目录)"
        print(f"《{c['name']}》 资料区{where}")
        print(f"当前页 {len(items)} 项" + (f" / 共 {total} 项" if total else ""))
        for i, it in enumerate(items, 1):
            kind = f"目录({it['childCount']}项)" if it["isFolder"] else it["type"]
            print(f"  {i:>3}. {it['name'][:52]:<54} {kind:<12} {it['size']:<7} {it['time']}")

    save_output(args.out, items)


DOWNLOAD_API = "https://mooc1.chaoxing.com/coursedata/downloadData"


def _safe_filename(name, fallback="download"):
    for candidate in (name, fallback, "download"):
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(candidate or ""))
        safe = safe.strip().rstrip(" .")
        if safe:
            break
    if re.fullmatch(
        r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3]",
        safe.split(".", 1)[0].rstrip(),
        flags=re.I,
    ):
        safe = "_" + safe
    return safe


def cmd_download(args):
    """下载课程资料。downloadData 走 302 到 d0.cldisk.com, 必须带 pan-yz Referer。"""
    s = load_session(args.session)
    c = find_course(s, args.course)
    if args.data_id:
        targets = [{"dataId": args.data_id, "name": args.name or args.data_id, "isFolder": False}]
    else:
        items, _, _ = list_materials(s, c["courseId"], c["clazzId"], c["cpi"])
        targets = [x for x in items if not x["isFolder"]]
    os.makedirs(args.out, exist_ok=True)
    ok = fail = 0
    for it in targets:
        safe = _safe_filename(it["name"], it["dataId"])
        path = os.path.join(args.out, safe)
        url = (
            f"{DOWNLOAD_API}?dataId={it['dataId']}&classId={c['clazzId']}"
            f"&cpi={c['cpi']}&courseId={c['courseId']}&ut=s"
        )
        temp_path = None
        try:
            with closing(
                s.get(
                    url,
                    timeout=180,
                    allow_redirects=True,
                    stream=True,
                    headers={"Referer": "https://pan-yz.chaoxing.com/"},
                )
            ) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                n = 0
                with tempfile.NamedTemporaryFile(
                    dir=args.out, prefix=".download-", suffix=".part", delete=False
                ) as f:
                    temp_path = f.name
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                        n += len(chunk)
            os.replace(temp_path, path)
            temp_path = None
            print(f"  [ok] {safe}  {n / 1024:.0f} KB")
            ok += 1
        except RequestStopped:
            raise
        except Exception as e:
            print(f"  [x] {safe}  {type(e).__name__}: {e}")
            fail += 1
        finally:
            if temp_path is not None:
                os.unlink(temp_path)
        time.sleep(args.sleep)
    print(f"\n下载完成: 成功 {ok}  失败 {fail}  -> {os.path.abspath(args.out)}")
