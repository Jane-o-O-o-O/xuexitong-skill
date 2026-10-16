# 参与贡献

欢迎改进学习通接口适配、解析、命令体验和文档。提交问题前，先查看 [现有 Issues](https://github.com/Jane-o-O-o-O/xuexitong-skill/issues)，并确认使用的是当前 `main` 分支。

## 开发环境

Python 3.10+。在仓库根目录创建虚拟环境并安装运行依赖，步骤见 [README](README.md#快速开始)。开发检查额外使用 Ruff：

```text
python -m pip install -r xuexitong/scripts/requirements.txt
python -m pip install ruff
python tests/run.py
python -m ruff check .
```

以上 `python` 均指所创建的虚拟环境解释器。Windows 可使用 `.venv/Scripts/python.exe`，macOS / Linux 可使用 `.venv/bin/python`，无需激活环境。

`tests/run.py` 禁用真实 HTTP 连接，并用临时目录隔离测试会话。测试通过不代表学习通当前在线接口兼容；如另做了手工验证，请注明验证了哪些行为。

## 修改位置

| 变更 | 主要位置 |
| --- | --- |
| 用户安装与使用说明 | `README.md` |
| Agent 的任务选择和执行规则 | `xuexitong/SKILL.md` |
| 详细参数与操作流程 | `xuexitong/references/` |
| CLI 参数与命令分派 | `xuexitong/scripts/chaoxing/cli.py` |
| 登录、会话、请求策略 | `qr_login.py`、`chaoxing/session.py`、`chaoxing/local.py`，均位于 `xuexitong/scripts/` |
| 课程、作业、资料、学习信息 | `xuexitong/scripts/chaoxing/` 中的对应业务模块 |
| 离线回归 | `tests/` |

尽量沿用现有模块边界，保持 CLI 可脱离 Agent 独立使用。更改参数、输出格式或默认请求范围时，同时更新使用文档和相关测试。解析问题优先补充脱敏或合成页面样本；请求策略、作业写入和会话行为需覆盖失败及中断场景。

## 提交 Issue

使用 [问题反馈或功能建议模板](https://github.com/Jane-o-O-o-O/xuexitong-skill/issues/new/choose)。问题报告中提供操作系统、Python 版本、复现步骤，以及预期结果和实际结果；命令和错误文本通常比只写“不能用”更有帮助。

请去掉 Cookie、密码、手机号、学号、课程鉴权参数和个人文件内容。不要上传原始会话、二维码或未经脱敏的作业页面。无法安全提供页面样本时，描述缺失的字段或结构即可。

## 提交变更

PR 说明应包含解决的问题、变更后的行为和实际执行的验证。尚未验证的在线行为直接注明；无需为修正文案运行真实账户流程。涉及上传、暂存或交卷的变更，不通过重复发送真实写请求来验证重试行为。

文档变更需核对相对链接、代码块和 CLI 参数。不要把虚拟环境、缓存、会话、答案或 `outputs/` 中的个人产物加入提交。
