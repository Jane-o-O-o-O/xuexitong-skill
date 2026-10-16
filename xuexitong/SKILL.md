---
name: xuexitong
description: 在用户本机操作超星学习通（Chaoxing）：扫码登录、查询课程和未交作业、课表与通知、读取课程资料并下载、查看章节进度及错题、上传作业附件和暂存或提交用户指定的作答。用于学习通账户内的实际查询与操作，不用于普通学习问答。
---

# 学习通本地助手

用此目录自带的 Python CLI 在用户本机执行。模型、文件处理和对话由当前 Agent 提供，无需平台后端、数据库或另配模型密钥。所有请求从运行脚本的设备发出；本地运行仍可能触发学习通验证。

## 执行入口

先定位本文件所在目录，下面的 `SKILL_DIR` 指它的绝对路径。命令不依赖当前工作目录；全局参数放在子命令前。使用 Python 3.10+ 和可用的本地虚拟环境。

```text
python "SKILL_DIR/scripts/xuexitong.py" --help
python -m pip install -r "SKILL_DIR/scripts/requirements.txt"
```

默认会话在用户主目录的 `.xuexitong/session_cookies.json`，二维码及状态文件在同一目录。`XUEXITONG_HOME` 可在进程启动前指定数据目录，或在命令前部使用 `--session ABSOLUTE_PATH`。查询导出与下载位置用显式的绝对路径 `--out` 指定；`fetch --save-html` 接收目录，其他查询的 `--save-html` 接收文件。

## 常用流程

1. 首次或登录过期时运行 `qr-login`，保持该进程运行，将它输出位置的**实际二维码图片**展示给用户。用户用学习通 App 扫码并确认。二维码过期、被取消或服务端拒绝后结束本次登录，不循环刷新。
2. 已有会话时直接执行所需查询；仅在登录态不明时用 `check`，本地查看到期信息用 `session-info`。`check` 返回 `loggedIn: false` 时先登录。
3. “查未交作业”先用 `pending --out ABSOLUTE_PATH`；“待办”先用 `todos --out ABSOLUTE_PATH`。默认均不扫描课堂活动、考试或作业题量。
4. 查课程用 `courses --out ABSOLUTE_PATH`。单课查询用明确的课名或 `courseId`；模糊匹配多门时让用户选择。只查询用户所需板块。
5. 需要某份作业题目时，取列表中的 `taskUrl`，运行 `resolve --task-url URL`，再用返回的 `doHomeWorkUrl` 执行 `fetch`。保留导出的 `work.json`，供后续题号核对使用。

```text
python "SKILL_DIR/scripts/xuexitong.py" qr-login
python "SKILL_DIR/scripts/xuexitong.py" pending --out "OUTPUT_DIR/pending.json"
python "SKILL_DIR/scripts/xuexitong.py" courses --out "OUTPUT_DIR/courses.json"
python "SKILL_DIR/scripts/xuexitong.py" works --course "COURSE_ID" --out "OUTPUT_DIR/works.json"
python "SKILL_DIR/scripts/xuexitong.py" materials --course "COURSE_ID" --out "OUTPUT_DIR/materials.json"
```

在执行示例前替换大写路径和 ID 占位符，并确保输出目录存在。

## 查询与资料

课表、通知、章节、学习记录、错题、考试列表、资料目录及下载参数见 [references/queries.md](references/queries.md)，仅在用到对应能力时读取。

活动和考试按课程查，不能从全局作业列表推断它们不存在。只有用户需要时才增加 `todos --include-activities --course COURSE_ID` 或 `todos --deep --course COURSE_ID`。全课扫描需明确范围并加 `--all-courses`。`--limit` 只限制显示，不减少扫描课程数。优先复用当前任务已导出的结果。

遇到验证、限流、权限错误或登录跳转时停止并向用户说明。CLI 会按会话串行执行，网络请求间隔至少 2 秒；HTTP 429 冷却至少 60 秒，验证页冷却 15 分钟，冷却信息跨进程保留。不要通过并发、删状态文件、换代理或反复重登来继续探测。让用户在本机官方页面完成验证，等待冷却后再执行原任务。扫码登录也受同一冷却约束。

## 作业操作

抓题、生成答案文件、上传、暂存和交卷的完整流程见 [references/homework.md](references/homework.md)。

读取与整理可直接按用户请求执行。只有用户明确要求上传、保存或提交对应内容时才执行该写操作；已有授权足够具体时直接继续。交卷需匹配具体作业和最终答案，使用 `submit --confirm`；只让生成草稿或查看题目不表示授权交卷。

保存或提交请求超时、响应异常时，结果可能已经生效。先用 `verify` 回读及官方页面确认，不自动重放写请求，也不要把“暂存成功”说成“交卷成功”。

## 凭据与结果

- Cookie 只保存在本机，属于敏感凭据。不要读入模型上下文、粘贴到对话、提交到仓库或打包分发。推荐扫码；需要从浏览器导入时使用本地 `set-cookie --cookie-file PATH`，不要把 Cookie 放在命令行。浏览器自动化可用时也可由用户在官方页面完成操作，不要求额外安装另一套 skill。
- 账号密码登录仅作备用。留空 `--password` 可在交互终端隐式输入；不要要求用户在聊天中发密码。
- `work.json`、作答链接、原始 HTML 和下载的学习资料也只用于当前用户的本地任务。返回给用户时提炼课程、标题、截止信息和文件路径，不展示鉴权参数。
- CLI 的标准输出可能同时包含日志和数据，需结构化处理时读取 `--out` 导出的 JSON。退出码 `0` 表示命令完成，`2` 表示参数或执行错误，`3` 表示访问已停止，`130` 表示被中断；扫码取消或过期返回 `1`。查询 JSON 内的 `errors`、`error`、`resolveError` 仍须检查；下载需检查控制台成功/失败统计及落地文件，不能将部分成功说成完整结果。
- 未扫描或未解析不等于没有待办。默认 `pending` 的 `questionCount` 为 `null`，`todos` 的未扫描板块为 `null`。历史实测覆盖不代表当前账户接口仍然有效。

当前没有实现消息私聊、签到、刷课、视频进度上报或考试作答接口。不要通过猜测接口补做这些操作。
