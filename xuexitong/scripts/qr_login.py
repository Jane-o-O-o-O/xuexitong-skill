#!/usr/bin/env python3
"""
超星学习通 扫码登录 (学习通 App 扫二维码)
=========================================
比账号密码更安全: 不落密码, 登录由学习通 App 官方确认。

流程 (逆向自 passport2.chaoxing.com 登录页内联脚本):
  1. GET  /login?fid=-1&refer=https://i.chaoxing.com
         页面隐藏域给 uuid + enc, 二维码图片在 /createqr?uuid=<uuid>&fid=-1
  2. 轮询 POST /getauthstatus/v2  body: enc, uuid, doubleFactorLogin, forbidotherlogin
         data.type == 4 -> 已扫码, 待手机端点确认 (返回 nickname/uid)
         data.type == 6 -> 手机端取消
         data.type == 7 -> 出错, 需刷新二维码
         data.status   -> 登录成功, cookie 由该响应种下
  3. 成功 -> 存 session_cookies.json

用法:
  python xuexitong.py qr-login       # 展示一张二维码，取消/过期/拒绝即停止
"""

import argparse
import json
import os
import sys
import time

import requests
from chaoxing import session as cx
from chaoxing.common import hidden_by_id, parse_inputs, write_json
from chaoxing.diagnostics import safe_error_text
from chaoxing.local import DATA_HOME, RequestStopped, local_runtime

LOGIN_PAGE = "https://passport2.chaoxing.com/login?fid=-1&refer=https%3A%2F%2Fi.chaoxing.com"
CREATE_QR = "https://passport2.chaoxing.com/createqr?uuid={uuid}&fid=-1"
AUTH_STATUS = "https://passport2.chaoxing.com/getauthstatus/v2"

HERE = str(DATA_HOME)
QR_PNG = os.path.join(HERE, "qr.png")
STATUS_JSON = os.path.join(HERE, "qr_status.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(msg):
    print(msg, flush=True)


def write_status(**kw):
    kw["at"] = int(time.time())
    write_json(STATUS_JSON, kw)


def hidden_value(html, elem_id):
    return hidden_by_id(html, elem_id)


def new_qr(s: requests.Session):
    """取一套新的 uuid/enc 并下载二维码 PNG。"""
    html = s.get(LOGIN_PAGE, timeout=30).text
    fields = {item["id"]: item.get("value", "") for item in parse_inputs(html) if item.get("id")}
    uuid, enc = fields.get("uuid"), fields.get("enc")
    dfl = fields.get("doubleFactorLogin") or "0"
    fol = fields.get("forbidotherlogin") or "0"
    if not uuid or not enc:
        raise RuntimeError("登录页里没解析到 uuid/enc, 页面结构可能已变")

    r = s.get(
        CREATE_QR.format(uuid=uuid),
        timeout=30,
        headers={"Referer": LOGIN_PAGE, "Accept": "image/*,*/*"},
    )
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        raise RuntimeError(f"二维码下载失败: HTTP {r.status_code}, {r.content[:60]!r}")
    os.makedirs(os.path.dirname(QR_PNG), exist_ok=True)
    with open(QR_PNG, "wb") as f:
        f.write(r.content)
    return uuid, enc, dfl, fol


def poll(s: requests.Session, uuid, enc, dfl, fol) -> dict:
    r = s.post(
        AUTH_STATUS,
        timeout=30,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_PAGE,
            "Accept": "application/json,*/*",
        },
        data={"enc": enc, "uuid": uuid, "doubleFactorLogin": dfl, "forbidotherlogin": fol},
    )
    try:
        response = r.json()
    except ValueError:
        return {"status": False, "_raw": r.text[:200]}
    if not isinstance(response, dict):
        raise RuntimeError("扫码状态响应不是 JSON 对象")
    return response


def main(argv=None):
    ap = argparse.ArgumentParser(description="超星学习通扫码登录")
    ap.add_argument("--timeout", type=int, default=150, help="总等待秒数")
    ap.add_argument("--session", default=cx.SESSION_FILE)
    a = ap.parse_args(argv)
    if a.timeout <= 0:
        ap.error("--timeout 必须大于 0")
    global QR_PNG, STATUS_JSON
    HERE = os.path.dirname(os.path.abspath(a.session))
    QR_PNG = os.path.join(HERE, "qr.png")
    STATUS_JSON = os.path.join(HERE, "qr_status.json")

    try:
        with local_runtime(a.session):
            return run_login(a)
    except RequestStopped as error:
        write_status(state="paused", error=str(error))
        log(str(error))
        return 3
    except (requests.RequestException, OSError, RuntimeError, ValueError) as error:
        detail = f"{type(error).__name__}: {safe_error_text(error)}"
        write_status(state="error", error=detail)
        log(f"[x] 扫码登录失败: {detail}")
        return 1


def run_login(a):

    s = cx.new_session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    deadline = time.monotonic() + a.timeout
    round_no = 0

    while time.monotonic() < deadline:
        round_no += 1
        uuid, enc, dfl, fol = new_qr(s)
        log(f"\n{'=' * 52}")
        log(f"二维码已就绪 (第 {round_no} 张)  uuid={uuid}")
        log(f"图片: {QR_PNG}")
        log("用「学习通」App -> 右上角扫一扫, 扫描打开该图片")
        log(f"{'=' * 52}")
        write_status(state="waiting_scan", qr=QR_PNG, uuid=uuid, round=round_no)

        typed_seen = False
        for _ in range(50):  # 3s * 50 = 150s 过期
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(3, remaining))
            if time.monotonic() >= deadline:
                break
            d = poll(s, uuid, enc, dfl, fol)

            if d.get("status"):
                uid = cx.cookie_get(s, "_uid")
                log(f"\n[+] 登录成功! uid={uid}")
                if d.get("containTwoFactorLogin"):
                    log(f"[!] 账号开了双因子: {d.get('twoFactorLoginPCUrl')}")
                cx.save_session(s, a.session)
                write_status(state="success", uid=uid, session=a.session)
                print(
                    json.dumps(
                        {"ok": True, "uid": uid, "session": a.session}, ensure_ascii=False, indent=2
                    )
                )
                return 0

            t = d.get("type")
            if t == 4 and not typed_seen:
                typed_seen = True
                log(
                    f"[~] 已扫码, 等待手机端点「确认」  "
                    f"nickname={d.get('nickname')} uid={d.get('uid')}"
                )
                write_status(
                    state="waiting_confirm", uid=d.get("uid"), nickname=d.get("nickname"), uuid=uuid
                )
            elif t == 6:
                log("[!] 手机端取消了登录，已停止")
                write_status(state="cancelled")
                return 1
            elif t == 7:
                raise RuntimeError("服务端拒绝扫码登录，请先检查官方页面；已停止")

        else:
            log("[!] 本张二维码已过期，已停止，请重新发起登录")
            write_status(state="expired")
            return 1
        if time.monotonic() >= deadline:
            break

    log("[x] 超时, 未完成扫码登录")
    write_status(state="timeout")
    return 1


if __name__ == "__main__":
    sys.exit(main())
