"""Credential-free request diagnostics and offline proxy inspection."""

import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests
from requests.utils import select_proxy

NETWORK_TARGETS = {
    "upload": "https://mooc1-api.chaoxing.com/upload-ans/ueditorupload/attachment",
    "course": "https://mooc1.chaoxing.com/",
    "login": "https://passport2.chaoxing.com/",
}
OPERATIONS = {
    "read": "读取",
    "upload": "上传附件",
    "save": "暂存",
    "submit": "交卷",
    "login": "登录",
    "write": "写入",
}


def proxy_summary(url, proxies):
    """Use Requests' effective selection, including Windows system proxies."""
    selected = select_proxy(url, proxies or {})
    if not selected:
        return {"enabled": False}
    try:
        parsed = urlsplit(selected if "://" in selected else "http://" + selected)
        return {
            "enabled": True,
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "authenticated": parsed.username is not None,
        }
    except (TypeError, ValueError):
        return {"enabled": True, "configurationInvalid": True}


def request_context(method, url, *, timeout=None, proxies=None):
    parsed = urlsplit(url)
    method = method.upper()
    operation = "read" if method in {"GET", "HEAD", "OPTIONS"} else "write"
    if operation == "write":
        if parsed.path.endswith(("/edit/uploadBase64", "/ueditorupload/attachment")):
            operation = "upload"
        elif parsed.path.endswith("/doNormalHomeWorkSubmit"):
            operation = "save" if parse_qs(parsed.query).get("tempSave") == ["true"] else "submit"
        elif parsed.hostname == "passport2.chaoxing.com":
            operation = "login"
    connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    return {
        "method": method,
        "operation": operation,
        "host": parsed.hostname,
        "path": re.sub(r"[0-9a-fA-F]{16,}|\d{5,}", "[id]", parsed.path),
        "timeoutSeconds": {"connect": connect, "read": read},
        "proxy": proxy_summary(url, proxies),
    }


def error_kind(error):
    # More specific classes precede Timeout / ConnectionError.
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    for cls, kind in (
        (requests.ConnectTimeout, "connect_timeout"),
        (requests.ReadTimeout, "read_timeout"),
        (requests.exceptions.ProxyError, "proxy_error"),
        (requests.exceptions.SSLError, "tls_error"),
        (requests.HTTPError, "http_error"),
        (requests.Timeout, "timeout"),
        (requests.ConnectionError, "connection_error"),
    ):
        if isinstance(error, cls):
            return kind
    return "request_error"


def diagnostic_message(data):
    kind = data["kind"]
    labels = {
        "connect_timeout": "建立连接超时",
        "read_timeout": "等待或读取服务器响应超时",
        "proxy_error": "代理连接异常",
        "tls_error": "TLS 连接或证书校验异常",
        "http_error": f"服务器返回 HTTP {data.get('httpStatus') or '错误'}",
        "http_redirect": "写请求收到重定向，已停止且未跟随跳转",
        "timeout": "网络请求超时",
        "connection_error": "网络连接中断或无法建立",
        "request_error": "网络请求异常",
        "interrupted": "操作在请求期间被中断",
    }
    parts = [f"{OPERATIONS.get(data.get('operation'), '请求')}：{labels[kind]}。"]
    if data.get("host"):
        parts.append(f"目标 {data['host']}{data.get('path', '')}。")
    if "elapsedSeconds" in data:
        parts.append(f"本次请求耗时 {data['elapsedSeconds']:.1f} 秒（不含本地限速等待）。")
    timeout = data.get("timeoutSeconds", {}).get("connect" if kind == "connect_timeout" else "read")
    if kind in {"connect_timeout", "read_timeout", "timeout"} and timeout is not None:
        parts.append(f"该阶段超时设置 {timeout} 秒；超时本身不能证明触发风控。")
    proxy = data.get("proxy", {})
    if proxy.get("enabled"):
        parts.append(
            "本次请求使用了代理；可运行 network-info 查看脱敏配置，代理存在不代表它导致故障。"
        )
    if data.get("remoteOutcome") == "not_sent":
        parts.append("连接阶段未完成；先检查网络、代理和登录上下文，再决定是否重新操作。")
    elif data.get("operation") == "upload":
        parts.append(
            "附件上传状态需核对；单独上传不会保存答案或交卷。保留本地文件和已有附件记录，勿自动重传。"
        )
    elif data.get("remoteOutcome") == "unknown":
        parts.append(
            "远端写入结果未确认，勿自动重放；先 verify 回读，交卷还需查询 works 核实状态。"
        )
    else:
        parts.append("保留已读取的结果；先检查网络和 request-status，再按原对象继续。")
    if data.get("httpStatus") in {413, 415}:
        parts.append("检查附件大小或文件类型是否被服务器接受。")
    if kind == "tls_error":
        parts.append("检查系统时间、证书或代理设置，保留 TLS 证书校验。")
    return "".join(parts)


def annotate_error(error, context, policy=None, *, elapsed=None):
    """Preserve exception classes while removing raw URLs/bodies from messages."""
    if isinstance(getattr(error, "diagnostic", None), dict):
        return error
    kind = error_kind(error)
    data = {**context, "at": time.time(), "kind": kind}
    if isinstance(elapsed, (int, float)):
        data["elapsedSeconds"] = round(elapsed, 3)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    data["httpStatus"] = status if isinstance(status, int) else None
    if kind == "http_error" and isinstance(status, int) and 300 <= status < 400:
        data["kind"] = "http_redirect"
    write = data.get("method") not in {"GET", "HEAD", "OPTIONS", None}
    data["remoteOutcome"] = (
        "not_sent" if kind == "connect_timeout" else "unknown" if write else "not_applicable"
    )
    error.diagnostic = data
    error.args = (diagnostic_message(data),)
    if policy is not None:
        policy.state["last_network_error"] = data
        try:
            policy.persist()
        except OSError:
            # A diagnostics disk error must not hide the uncertain remote operation.
            error.args = (str(error) + "本地网络诊断记录写入失败。",)
    return error


def safe_error_text(error):
    if isinstance(error, KeyboardInterrupt):
        return str(error) or "用户中断操作"
    if isinstance(error, requests.RequestException):
        if not isinstance(getattr(error, "diagnostic", None), dict):
            annotate_error(error, {})
    return str(error)


def safe_server_message(value):
    """Keep short human messages without echoing URLs or credential fields."""
    if not isinstance(value, str):
        return "服务器未提供文字说明"
    text = re.sub(r"https?://[^\s<>\"']+", "[链接已隐藏]", value)
    text = re.sub(r"(?i)\b(cookie|authorization)[\"']?\s*[:=][^\r\n]*", r"\1=[已隐藏]", text)
    text = re.sub(
        r"(?i)\b(enc2?|token|signature|password|uid|cpi|openc)\b[\"']?\s*[:=]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        "[鉴权字段已隐藏]",
        text,
    )
    return text[:300]


def cmd_network_info(args):
    # This creates no authenticated session and makes no network request.
    url = NETWORK_TARGETS[args.target]
    with requests.Session() as client:
        settings = client.merge_environment_settings(url, {}, None, None, None)
        data = {
            "target": args.target,
            "host": urlsplit(url).hostname,
            "effectiveProxy": proxy_summary(url, settings["proxies"]),
            "trustEnvironment": client.trust_env,
            "note": "仅读取本机 Requests 有效代理配置（含系统代理），不联网、不读取 Cookie。"
            "未配置显式代理仍可能经过 TUN/VPN；此结果不判断网络可用性或故障归因。",
        }
    print(json.dumps(data, ensure_ascii=False, indent=2))
