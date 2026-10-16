# 学习通 Skill

把原来的统一管理平台改为由用户本地 Agent 直接调用的学习通工具。当前目录是分发项目，`xuexitong/` 是可独立安装的完整 skill；不再依赖 Web 前端、API 服务、PostgreSQL、Redis、远程 Worker 或平台模型网关。

```text
用户 -> 本地 Agent -> xuexitong skill -> 学习通官方接口
             |
             +-> 本地下载、阅读、生成文件
```

本地执行减少了平台集中持有账户和集中发请求的需求，但不是绕过风控的方案。仍可能要求验证码、重新登录或在官方页面操作；脚本遇到验证与限流会停止。

## 目录

```text
xuexitong/
  SKILL.md                  Agent 的能力入口、流程与操作边界
  agents/openai.yaml        Codex 展示元数据
  references/               查询和作业流程，按需读取
  scripts/
    xuexitong.py             统一 CLI
    qr_login.py              扫码登录
    chaoxing/               从旧项目提取并调整的本地业务模块
    chaoxing_hw.py           原 Python 导入兼容层
    requirements.txt         requests、pycryptodome
tests/                      离线回归与本地执行行为验证
```

## 本地使用

Python 3.10+。Windows PowerShell，在本项目目录运行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\xuexitong\scripts\requirements.txt
& .\.venv\Scripts\python.exe .\xuexitong\scripts\xuexitong.py qr-login
& .\.venv\Scripts\python.exe .\xuexitong\scripts\xuexitong.py pending
```

登录命令保持运行时，打开输出位置的二维码，用学习通 App 扫码并确认。`pending` 默认在当前目录导出 `pending.json`；自动化调用建议显式提供绝对路径 `--out`。macOS/Linux 使用 `.venv/bin/python`，其他参数相同。

要让 Agent 发现此 skill，把整个 `xuexitong` 文件夹放进该 Agent 的 skill 搜索目录。Codex 常见位置是 `~/.codex/skills/xuexitong`；Claude Code 常见位置是 `~/.claude/skills/xuexitong`。其他宿主需要支持读取 `SKILL.md` 和执行本机命令，不支持自动发现时可让 Agent 读取当前项目里的 `xuexitong/SKILL.md`。安装后用本地 Python 环境安装该目录内的依赖，再使用 `$xuexitong` 或对应宿主的技能调用方式。本次没有修改任何 Agent 的全局配置，也未自动安装 skill。

会话和二维码默认存放在 `~/.xuexitong/`。通过环境变量 `XUEXITONG_HOME` 可在启动前调整；`--session` 可为一个账户指定独立会话文件。同一会话的命令互斥，冷却状态跨命令保留。不同路径不提供按真实账号去重的互斥，日常使用同一账号应固定使用同一个会话文件。

Cookie 文件为本地明文凭据；应置于仅当前用户可访问的目录。不要把个人会话、作业快照、答案或下载资料加入分发包。skill 内没有复制旧项目的用户 Cookie、答案、作业数据或服务配置。

## 这次迁移

保留了原 Python 客户端的课程、章节、作业、资料、课表、通知、学习记录、错题、考试及讨论列表能力，以及附件上传、暂存、提交与回读。新入口有 26 个子命令，详细参数以 `--help` 为准。

- 默认 `pending` 不再逐份打开作业探测题量；增加 `resolve` 单独打开目标作业。
- 默认 `todos` 只查全局作业；课堂活动、考试和全课扫描改为显式范围。
- 增加本地进程锁、至少 2 秒请求间隔、验证/限流停止和持久化冷却；跳转请求同样检查目标域名。没有自动重放写请求。
- Cookie 与二维码移出 skill 目录；扫码取消、拒绝和过期后停止自动刷新；密码备用入口支持终端隐式输入。
- 单课查询多项匹配时拒绝直接选第一门，提示使用 courseId。

原平台工程保持独立。平台的账户系统、绑定、任务队列、定时调度、邮件通知、沙箱执行与模型代理没有迁入；对话、文档处理和模型调用交给用户自己的 Agent。后续优先用真实账户手工验收登录、一个列表查询和单文件下载，再扩充有需求的能力。

## 验证

```powershell
& .\.venv\Scripts\python.exe .\tests\run.py
```

测试禁用真实 HTTP 连接，覆盖原有解析/存储/作业行为，以及轻量查询、作用域、本地互斥与请求停止。真实登录、当前接口兼容性和交卷结果需要用户在本机验收，离线通过不代表在线验证通过。
