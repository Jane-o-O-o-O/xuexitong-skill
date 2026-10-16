"""Local state, process exclusion, and request pacing for the skill CLI."""

import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .common import read_json, write_json

DATA_HOME = Path(os.environ.get("XUEXITONG_HOME", Path.home() / ".xuexitong")).expanduser()
ACTIVE_POLICY = ContextVar("xuexitong_request_policy", default=None)
MIN_REQUEST_INTERVAL = 8
QUERY_REQUEST_LIMIT = 8
BATCH_COOLDOWN = 60


class RequestStopped(RuntimeError):
    """Stop the entire operation, including handlers that allow partial results."""


class _CaptchaPromptParser(HTMLParser):
    """Exclude dormant dialogs and scripts when looking for a captcha prompt."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
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


def _visible_prompt_text(body):
    parser = _CaptchaPromptParser()
    parser.feed(body)
    parser.close()
    return "".join(parser.text)


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
    def __init__(self, state_path=None, interval=MIN_REQUEST_INTERVAL, max_requests=None):
        self.path = Path(state_path) if state_path else None
        self.interval = max(MIN_REQUEST_INTERVAL, interval)
        if max_requests is not None and (not isinstance(max_requests, int) or max_requests < 1):
            raise ValueError("请求预算必须是正整数")
        self.max_requests = max_requests
        self.request_count = 0
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
            code = self.state.get("last_stop", {}).get("reason", "unknown")
            raise RequestStopped(
                f"本地请求冷却中，剩余约 {math.ceil(remaining)} 秒，原因: {code}。"
                "可用 request-status 查看记录；这不表示 App 一定无法访问。"
            )
        if self.max_requests is not None and self.request_count >= self.max_requests:
            self.stop(
                f"本次查询已达到 {self.max_requests} 个请求的本地上限，已停止并休息 60 秒。"
                "请复用已有结果，缩小范围后分批继续，不要自动重跑整条命令。",
                BATCH_COOLDOWN,
                code="query_budget",
                source="local",
            )
        delay = self.state.get("last_request_at", 0) + self.interval - time.time()
        if delay > 0:
            time.sleep(delay)
        self.state["last_request_at"] = time.time()
        self.request_count += 1
        if self.max_requests is not None and self.request_count == self.max_requests:
            self._record_stop(BATCH_COOLDOWN, code="query_budget", source="local")
        else:
            self.persist()

    def _record_stop(self, seconds, *, response=None, code="verification", source="http"):
        self.state["pause_until"] = time.time() + seconds
        parts = urlsplit(response.url) if response is not None else None
        self.state["last_stop"] = {
            "at": time.time(),
            "reason": code,
            "source": source,
            "status": response.status_code if response is not None else None,
            "host": parts.hostname if parts else None,
            "path": re.sub(r"[0-9a-fA-F]{16,}|\d{5,}", "[id]", parts.path) if parts else None,
        }
        self.persist()

    def stop(self, reason, seconds=900, *, response=None, code="verification", source="http"):
        self._record_stop(seconds, response=response, code=code, source=source)
        raise RequestStopped(reason)

    def check_response(self, response, *, stream=False):
        destination = urljoin(response.url, response.headers.get("Location", ""))
        if "antispidershowverify.ac" in urlsplit(destination).path.lower():
            self.stop(
                "学习通跳转到官方验证，已在本地冷却 15 分钟。",
                response=response,
                code="verification_redirect",
                source="redirect",
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            cooldown = max(60, int(retry_after)) if retry_after.isdigit() else 60
            self.stop(
                f"学习通返回 HTTP 429，已在本地冷却 {cooldown} 秒。",
                cooldown,
                response=response,
                code="rate_limited",
            )
        if response.status_code in (401, 403):
            self.stop(
                "学习通返回 HTTP 401/403，请检查登录、权限或接口上下文；本地冷却 60 秒。",
                60,
                response=response,
                code="access_denied",
            )
        content_type = response.headers.get("Content-Type", "").lower()
        if not stream or "text/html" in content_type:
            body = _visible_prompt_text(response.text[:262144]).lower()
            is_login_page = (
                urlsplit(response.url).hostname == "passport2.chaoxing.com"
                and urlsplit(response.url).path == "/login"
            )
            if any(
                marker in body
                for marker in (
                    "访问过于频繁",
                    "操作过于频繁",
                    "请完成安全验证",
                    "异常访问",
                    "拖动滑块",
                )
            ) or (not is_login_page and "请输入验证码" in body):
                self.stop(
                    "页面可见内容疑似要求验证或限流，已在本地冷却 15 分钟；"
                    "请结合官方页面核对，识别结果不代表账号已被封禁。",
                    response=response,
                    code="visible_verification_prompt",
                    source="page_heuristic",
                )
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
def local_runtime(session_path, interval=MIN_REQUEST_INTERVAL, max_requests=None):
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
        token = ACTIVE_POLICY.set(
            RequestPolicy(str(path) + ".requests.json", interval, max_requests)
        )
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
