"""Local state, process exclusion, and request pacing for the skill CLI."""

import math
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import read_json, write_json

DATA_HOME = Path(os.environ.get("XUEXITONG_HOME", Path.home() / ".xuexitong")).expanduser()
ACTIVE_POLICY = ContextVar("xuexitong_request_policy", default=None)


class RequestStopped(RuntimeError):
    """Stop the entire operation, including handlers that allow partial results."""


class _CaptchaPromptParser(HTMLParser):
    """Exclude dormant dialogs and scripts when looking for a captcha prompt."""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = "".join((attrs.get("style") or "").lower().split())
        hidden = (
            (self.stack and self.stack[-1][1])
            or tag in {"script", "style", "template"}
            or "hidden" in attrs
            or (attrs.get("aria-hidden") or "").lower() == "true"
            or (attrs.get("type") or "").lower() == "hidden"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if not hidden:
            for name in ("placeholder", "aria-label", "title", "alt"):
                self.text.append(attrs.get(name) or "")
        if tag not in self._VOID_TAGS:
            self.stack.append((tag, bool(hidden)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.text.append(data)


def _has_captcha_prompt(body):
    parser = _CaptchaPromptParser()
    parser.feed(body)
    return "请输入验证码" in "".join(parser.text)


def official_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or not any(
            host == domain or host.endswith("." + domain)
            for domain in ("chaoxing.com", "cldisk.com")
        )
    ):
        raise RequestStopped("仅允许访问学习通及其资料下载域名的 HTTPS 地址。")


class RequestPolicy:
    def __init__(self, state_path=None, interval=2):
        self.path = Path(state_path) if state_path else None
        self.interval = max(2, interval)
        self.state = read_json(self.path) if self.path and self.path.exists() else {}
        if not isinstance(self.state, dict):
            raise ValueError("请求状态文件应为 JSON 对象")
        for name in ("pause_until", "last_request_at"):
            value = self.state.get(name, 0)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("请求状态文件包含无效时间")

    def persist(self):
        if self.path:
            write_json(self.path, self.state)

    def before_request(self, url):
        official_url(url)
        remaining = self.state.get("pause_until", 0) - time.time()
        if remaining > 0:
            raise RequestStopped(
                f"访问已暂停，剩余约 {math.ceil(remaining)} 秒。请在官方页面完成验证后等待冷却。"
            )
        delay = self.state.get("last_request_at", 0) + self.interval - time.time()
        if delay > 0:
            time.sleep(delay)
        self.state["last_request_at"] = time.time()
        self.persist()

    def stop(self, reason, seconds=900):
        self.state["pause_until"] = time.time() + seconds
        self.persist()
        raise RequestStopped(reason)

    def check_response(self, response, *, stream=False):
        destination = urljoin(response.url, response.headers.get("Location", ""))
        if "antispidershowverify.ac" in destination.lower():
            self.stop("学习通要求官方验证，已停止请求并冷却 15 分钟。")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            cooldown = max(60, int(retry_after)) if retry_after.isdigit() else 60
            self.stop(f"学习通限制了请求频率，已停止请求并冷却 {cooldown} 秒。", cooldown)
        if response.status_code in (401, 403):
            self.stop("学习通拒绝访问，请检查官方页面的登录和权限；已冷却 60 秒。", 60)
        content_type = response.headers.get("Content-Type", "").lower()
        if not stream or "text/html" in content_type:
            body = response.text[:262144].lower()
            is_login_page = (
                urlsplit(response.url).hostname == "passport2.chaoxing.com"
                and urlsplit(response.url).path == "/login"
            )
            if any(
                marker in body
                for marker in (
                    "antispidershowverify.ac",
                    "访问过于频繁",
                    "操作过于频繁",
                    "请完成安全验证",
                    "异常访问",
                    "拖动滑块",
                )
            ) or (not is_login_page and _has_captcha_prompt(body)):
                self.stop("学习通返回验证或频控页面，已停止请求并冷却 15 分钟。")
        target = urlsplit(destination)
        source = urlsplit(response.url)
        if (
            300 <= response.status_code < 400
            and target.hostname == "passport2.chaoxing.com"
            and target.path.startswith("/login")
            and source.hostname != "passport2.chaoxing.com"
        ):
            raise RequestStopped("登录态已失效，请重新扫码登录。")


@contextmanager
def local_runtime(session_path, interval=2):
    """The OS releases the lock even when the agent process is interrupted."""
    path = Path(session_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + ".lock", "a+b")
    acquired = False
    try:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise RequestStopped("同一会话已有本地命令在运行，请等待它结束。") from None
        token = ACTIVE_POLICY.set(RequestPolicy(str(path) + ".requests.json", interval))
        try:
            yield
        finally:
            ACTIVE_POLICY.reset(token)
    finally:
        if acquired:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
