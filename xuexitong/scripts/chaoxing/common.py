"""Common operations for the Chaoxing client."""

import html as html_lib
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser


def die(msg: str, code: int = 2):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def abs_url(base_host: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return base_host.rstrip("/") + "/" + href.lstrip("/")


def strip_tags(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    # 超星把标题里的中文转成了数字实体 (&#23454;&#39564;4), 必须反转义
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _iter_input_tags(html: str):
    """扫描 <input ...> 时跳过引号内的 '>'.

    坑: 富文本答案会写成 value='<p><br></p><div class="x">...' —— 用 <input[^>]*>
    会在第一个 '>' 处截断标签, 导致字段丢失或被截半。
    """
    pos = 0
    while True:
        match = re.search(r"<input\b", html[pos:], re.IGNORECASE)
        if match is None:
            return
        i = pos + match.start()
        j, q = i + 6, None
        while j < len(html):
            c = html[j]
            if q:
                if c == q:
                    q = None
            elif c in "\"'":
                q = c
            elif c == ">":
                break
            j += 1
        yield html[i : j + 1]
        pos = j + 1


class _InputParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append({name: value or "" for name, value in attrs})


def parse_inputs(html: str) -> list:
    """Parse inputs without truncating rich HTML stored inside quoted values."""
    parser = _InputParser()
    parser.feed(html)
    parser.close()
    return parser.inputs


def extract_hidden_fields(html: str) -> dict:
    return {
        attrs["name"]: attrs.get("value", "")
        for attrs in parse_inputs(html)
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name")
    }


def hidden_fields_by_id(html: str) -> dict:
    return {
        attrs["id"]: attrs.get("value", "")
        for attrs in parse_inputs(html)
        if attrs.get("type", "").lower() == "hidden" and attrs.get("id")
    }


def _question_id(html: str):
    qid = hidden_by_id(html, "questionId")
    return qid if qid.isdigit() else None


def hidden_by_id(html: str, eid: str) -> str:
    """上传相关的 enc 用的是 id 而不是 name, 常规 hidden 提取拿不到。"""
    return next(
        (attrs.get("value", "") for attrs in parse_inputs(html) if attrs.get("id") == eid),
        "",
    )


def read_json(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def write_text(path, text):
    """Replace only after a complete write, preserving existing files on failure."""
    target = os.path.abspath(os.fspath(path))
    fd, temporary = tempfile.mkstemp(
        prefix=".chaoxing-", suffix=".tmp", dir=os.path.dirname(target)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            output.write(text)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def save_output(path, data):
    if path:
        write_json(path, data)
        print(f"[+] -> {path}")


def fmt_size(v) -> str:
    """超星的 size 字段是 "11.30K" 这种人类可读格式, 保持一致。"""
    if isinstance(v, str) and v and not v.isdigit():
        return v
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    for unit in ("", "K", "M", "G"):
        if n < 1024:
            return f"{n:.2f}{unit}" if unit else str(int(n))
        n /= 1024
    return f"{n:.2f}G"
