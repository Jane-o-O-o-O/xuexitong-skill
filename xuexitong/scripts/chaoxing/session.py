"""Session operations for the Chaoxing client."""

import base64
import binascii
import json
import os
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .common import die, read_json, write_json
from .local import ACTIVE_POLICY, DATA_HOME, RequestPolicy, official_url

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad as _pkcs7_pad

    _HAS_AES = True
except ImportError:
    _HAS_AES = False

SESSION_FILE = str(DATA_HOME / "session_cookies.json")


AES_KEY = b"u2oh6Vu^HWe4_AES"  # 16 bytes -> AES-128


UA = (
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)


SKIP_COOKIE_KEYS = {
    "path",
    "domain",
    "expires",
    "max-age",
    "samesite",
    "secure",
    "httponly",
    "version",
    "priority",
}


def encode_account(text: str) -> str:
    """超星 fanyalogin 的 uname/password 加密: AES-128-CBC + PKCS7 + base64"""
    if not _HAS_AES:
        raise SystemExit("缺少 pycryptodome, 请先: pip install pycryptodome")
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
    return base64.b64encode(cipher.encrypt(_pkcs7_pad(text.encode("utf-8"), 16))).decode()


class ChaoxingSession(requests.Session):
    """Apply consistent HTTP errors and timeouts without replaying submissions."""

    def __init__(self):
        super().__init__()
        self.policy = ACTIVE_POLICY.get() or RequestPolicy()
        self.hooks["response"].append(self.guard_response)

    def send(self, request, **kwargs):
        # send() also runs for every redirect, before any cookies leave this process.
        self.policy.before_request(request.url)
        return super().send(request, **kwargs)

    def guard_response(self, response, **kwargs):
        # Response hooks run before requests follows a redirect.
        try:
            self.policy.check_response(response, stream=kwargs.get("stream", False))
            # The account base endpoint advertises a legacy HTTP URL even over TLS.
            # Upgrade only this observed same-host/path GET redirect before preparing it.
            source = urlsplit(response.url)
            target = urlsplit(urljoin(response.url, response.headers.get("Location", "")))
            if (
                300 <= response.status_code < 400
                and response.request.method in {"GET", "HEAD"}
                and source.scheme == "https"
                and source.hostname == target.hostname == "i.chaoxing.com"
                and source.path == target.path == "/base"
                and target.scheme == "http"
            ):
                upgraded = urlunsplit(target._replace(scheme="https"))
                official_url(upgraded)
                response.headers["Location"] = upgraded
        except BaseException:
            response.close()
            raise
        return response

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 30)
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            response.close()
            raise
        return response


def new_session(cookie_header: str = "") -> requests.Session:
    s = ChaoxingSession()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    if cookie_header:
        attach_cookie(s, cookie_header)
    return s


def cookie_get(s: requests.Session, name: str, default=None):
    """cookies.get() 在存在同名多域 cookie 时会抛 CookieConflictError, 这里取第一个匹配。"""
    for c in s.cookies:
        if c.name == name:
            return c.value
    return default


def attach_cookie(s: requests.Session, cookie_header: str):
    """把 cookie 串种进 session 的 cookie jar。

    只种在父域 .chaoxing.com 一次: 同一个 cookie 种到多个域会让 requests 的
    cookies.get() 抛 CookieConflictError, 而 .chaoxing.com 本身已覆盖
    passport2 / mooc1 / mooc2-ans / mooc1-api 等全部作业相关子域。
    """
    for kv in re.split(r"[;\n]", cookie_header):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or k.lower() in SKIP_COOKIE_KEYS:
            continue
        s.cookies.set(k, v, domain=".chaoxing.com", path="/")


def jwt_expiry(token: str):
    """从 p_auth_token 这个 JWT 里取 exp。这是登录态过期时间的权威来源。"""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def save_session(s: requests.Session, path: str = SESSION_FILE):
    """保存 cookie 并保留 expires —— 只存 name/value 就无从判断登录态还剩多久。"""
    detail = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": bool(c.secure),
        }
        for c in s.cookies
    ]
    data = {
        "cookies": {c["name"]: c["value"] for c in detail},
        "detail": detail,
        "savedAt": int(time.time()),
    }
    write_json(path, data)
    print(f"[+] cookie 已保存 -> {path}")
    exp = jwt_expiry(data["cookies"].get("p_auth_token", ""))
    if exp:
        print(
            f"[i] 登录态到期 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}"
            f" (剩余 {(exp - time.time()) / 86400:.2f} 天)"
        )


def load_session(path: str = SESSION_FILE) -> requests.Session:
    """兼容两种格式: 老版扁平 {name: value} 与新版 {cookies:{...}, detail:[...]}"""
    if not os.path.exists(path):
        die(f"找不到 cookie 文件 {path}, 先跑 login 或手工喂 cookie")
    pairs, detail = _cookie_data(read_json(path))
    s = new_session()
    detailed_names = set()
    for c in detail:
        s.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", ".chaoxing.com"),
            path=c.get("path", "/"),
            expires=c.get("expires"),
            secure=bool(c.get("secure", False)),
        )
        detailed_names.add(c["name"])
    # Flattened legacy files have no scope metadata; only those names need a parent domain.
    for name, value in pairs.items():
        if name not in detailed_names:
            s.cookies.set(name, value, domain=".chaoxing.com", path="/")
    return s


def _cookie_data(data):
    if not isinstance(data, dict):
        die("cookie 文件格式错误: 应为 JSON 对象")
    if isinstance(data.get("cookies"), dict):
        pairs, detail = data["cookies"], data.get("detail") or []
    else:
        pairs, detail = data, []
    if not isinstance(detail, list) or any(
        not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie
        for cookie in detail
    ):
        die("cookie 文件格式错误: detail 应包含 name/value")
    return pairs, detail


def session_alive(base_html: str, uid: str) -> bool:
    """判断登录态是否有效。

    坑: 未登录的 base 页里**同样含字面「账号」**, 早期版本用它判断, 结果永远返回
    loggedIn=true (假阳性)。硬判据 = 页面里出现你自己的 uid。
    """
    return bool(uid) and uid in base_html


def cmd_session_info(args):
    """打印登录态剩余有效期。"""
    path = args.session
    if not os.path.exists(path):
        die(f"找不到 {path}")
    data = read_json(path)
    pairs, cookies = _cookie_data(data)
    pairs = dict(pairs)
    detail = {c["name"]: c for c in cookies}
    for name, cookie in detail.items():
        pairs[name] = cookie["value"]
    now = time.time()

    print(f"cookie 文件: {path}")
    if data.get("savedAt"):
        print(
            "保存时间   :",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["savedAt"])),
        )
    exp = jwt_expiry(pairs.get("p_auth_token", ""))
    if exp:
        print(
            "权威过期   :",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)),
            "(p_auth_token 的 JWT exp)",
        )
        left = exp - now
        print(
            f"剩余       : {left / 86400:.2f} 天"
            if left > 0
            else f"剩余       : 已过期 {-left / 86400:.2f} 天"
        )
    print()
    print(f"  {'cookie':<16}{'声明过期':<22}剩余")
    for name in sorted(pairs):
        e = (detail.get(name) or {}).get("expires")
        if e:
            print(
                f"  {name:<16}{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e)):<22}"
                f"{(e - now) / 86400:.2f} 天"
            )
        else:
            print(f"  {name:<16}{'会话级(未声明)':<22}-")


def get_fid(s: requests.Session) -> str:
    fid = cookie_get(s, "fid")
    if fid:
        return str(fid)
    r = s.get("https://i.chaoxing.com/base", timeout=30)
    m = re.search(r"""(?:fid|FID)\s*["']?\s*[:=]\s*["']?(\d+)""", r.text)
    if m:
        return m.group(1)
    die("拿不到 fid, cookie 可能已失效")


def do_login(phone: str, password: str) -> requests.Session:
    s = new_session()
    payload = {
        "fid": "-1",
        "uname": encode_account(phone),
        "password": encode_account(password),
        "refer": "https%3A%2F%2Fi.chaoxing.com",
        "t": "true",
        "forbidotherlogin": "0",
        "validate": "",
        "doubleFactorLogin": "0",
        "independentId": "0",
        "independentNameId": "0",
    }
    r = s.post("https://passport2.chaoxing.com/fanyalogin", data=payload, timeout=30)
    try:
        j = r.json()
    except ValueError:
        die(f"登录响应非 JSON (可能触发风控/验证码): {r.text[:300]}")
    if not j.get("status"):
        die(f"登录失败: {j.get('msg')} | {json.dumps(j, ensure_ascii=False)[:300]}")
    print(f"[+] 登录成功: {j.get('msg')}")
    return s


def cmd_login(args):
    from getpass import getpass

    save_session(do_login(args.phone, args.password or getpass("学习通密码: ")), args.session)


def cmd_set_cookie(args):
    cookie = args.cookie
    if args.cookie_file:
        with open(args.cookie_file, encoding="utf-8") as file:
            cookie = file.read().strip()
    save_session(new_session(cookie), args.session)


def cmd_check(args):
    s = load_session(args.session)
    response = s.get("https://i.chaoxing.com/base")
    uid = cookie_get(s, "_uid") or ""
    print(
        json.dumps(
            {
                "loggedIn": session_alive(response.text, uid),
                "uid": uid,
                "uidInPage": bool(uid) and uid in response.text,
                "hasMyCourse": "我的课程" in response.text,
                "kickToLogin": "passport2" in response.text or "space/index" in response.text,
                "htmlLen": len(response.text),
            },
            ensure_ascii=False,
        )
    )


def cmd_request_status(args):
    """Inspect only local pacing state, without sending an authenticated request."""
    policy = RequestPolicy(str(args.session) + ".requests.json")
    print(
        json.dumps(
            {
                "localCooldownSeconds": max(
                    0, int(policy.state.get("pause_until", 0) - time.time())
                ),
                "lastStop": policy.state.get("last_stop"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
