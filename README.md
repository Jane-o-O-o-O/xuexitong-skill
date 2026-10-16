# Xuexitong Skill

**让你的本地 AI Agent 使用学习通。**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Agent Skill](https://img.shields.io/badge/Agent-Skill-167D72)](xuexitong/SKILL.md)
[![CLI](https://img.shields.io/badge/CLI-26_commands-555555)](xuexitong/references/queries.md)

把超星学习通的课程、作业、课表和资料接入你正在使用的 Agent。扫码登录后，可以直接用自然语言查询未交作业、查找课件、整理学习信息，也可以在明确授权后上传附件、暂存或提交作答。

Python 客户端在本机执行，Cookie 保存在本地。对话、模型调用和文件处理由宿主 Agent 提供，无需部署管理平台或为本项目单独配置模型密钥。

[快速开始](#快速开始) · [接入你的 Agent](#接入你的-agent) · [命令示例](#命令示例) · [常见问题](#常见问题) · [参与贡献](CONTRIBUTING.md)

## 可以做什么

| 场景 | 能力 | 可以这样说 |
| --- | --- | --- |
| 作业与待办 | 未交作业、提交状态、指定课程的活动和考试待办 | 帮我看看现在有哪些作业没交。 |
| 课程与进度 | 课程列表、章节进度、学习统计、错题、考试和讨论列表 | 看一下数据结构的章节完成情况。 |
| 课表与通知 | 当前周或指定周课表、通知筛选 | 查一下第 3 周的课表和未读作业通知。 |
| 资料与下载 | 课程资料目录、按需展开文件夹、下载指定课件 | 找到这门课的实验指导，下载到本地。 |
| 作业处理 | 读取题目、生成本地答案模板、上传附件、暂存、回读和交卷 | 把我确认过的实验报告上传并暂存，先不要交卷。 |

默认先读列表，再按需打开目标内容。`pending` 不会逐份探测题量，`todos` 不会默认扫描所有课程的活动和考试。

## 快速开始

需要 **Python 3.10+**、Git，以及用于扫码确认的学习通 App。CLI 可独立运行，不要求先安装 Agent。

<details open>
<summary><strong>Windows / PowerShell</strong></summary>

```powershell
git clone https://github.com/Jane-o-O-o-O/xuexitong-skill.git
cd xuexitong-skill
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\xuexitong\scripts\requirements.txt

& .\.venv\Scripts\python.exe .\xuexitong\scripts\xuexitong.py qr-login
& .\.venv\Scripts\python.exe .\xuexitong\scripts\xuexitong.py pending
```

</details>

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
git clone https://github.com/Jane-o-O-o-O/xuexitong-skill.git
cd xuexitong-skill
python3 -m venv .venv
.venv/bin/python -m pip install -r xuexitong/scripts/requirements.txt

.venv/bin/python xuexitong/scripts/xuexitong.py qr-login
.venv/bin/python xuexitong/scripts/xuexitong.py pending
```

</details>

运行 `qr-login` 后，保持命令运行，打开终端输出位置的二维码图片，用学习通 App 扫码并确认。登录成功后再执行 `pending`，查看未交作业；结果同时写入当前目录的 `pending.json`。

二维码取消、过期或被服务端拒绝时，本次登录会结束。需要重新登录时，再次发起命令。

## 接入你的 Agent

可安装的技能是仓库中的 **[`xuexitong/`](xuexitong) 整个文件夹**，入口是其中的 `SKILL.md`，保留 `scripts/`、`references/` 和 `agents/` 的相对位置。

如果宿主支持从 GitHub 安装技能，可以直接把这段话发给 Agent：

```text
从 https://github.com/Jane-o-O-o-O/xuexitong-skill/tree/main/xuexitong
安装 xuexitong 技能，并在本机独立 Python 环境安装 scripts/requirements.txt 的依赖。
```

手动安装时，把 `xuexitong/` 放入宿主支持的技能目录，并让 Agent 使用已安装依赖的 Python 解释器。不同宿主的发现方式见 [Codex 技能文档](https://developers.openai.com/codex/skills)和 [Claude Code 技能文档](https://code.claude.com/docs/en/skills)。不支持技能自动发现的 Agent，也可以直接读取本仓库的 `xuexitong/SKILL.md`，再执行其中的本地命令。

安装后，在 Codex 中可以这样开始：

```text
$xuexitong 帮我查一下未提交的作业，列出课程、作业名称和剩余时间。
```

后续可以继续说：“只看数据结构”“下载这份实验指导”“先生成草稿，暂时不要提交”。宿主需要能够执行本机命令，并向你展示登录二维码。

## 命令示例

下面的 PowerShell 示例在仓库根目录运行；macOS / Linux 使用 `.venv/bin/python` 调用同一入口。`COURSE_ID`、`DATA_ID` 和文件名需要替换为查询结果中的实际值。

```powershell
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$cx = (Resolve-Path .\xuexitong\scripts\xuexitong.py).Path
New-Item -ItemType Directory -Force .\outputs | Out-Null

# 课程列表、未交作业和本周课表
& $python $cx courses --out .\outputs\courses.json
& $python $cx pending --out .\outputs\pending.json
& $python $cx schedule --out .\outputs\schedule.json

# 指定课程的作业和资料目录
& $python $cx works --course COURSE_ID --out .\outputs\works.json
& $python $cx materials --course COURSE_ID --out .\outputs\materials.json

# 下载选中的一个文件
& $python $cx download --course COURSE_ID --data-id DATA_ID --name "实验指导.pdf" --out .\outputs\materials

# 指定课程的活动待办；考试待办使用 --deep
& $python $cx todos --include-activities --course COURSE_ID --out .\outputs\todos.json

& $python $cx --help
& $python $cx materials --help
```

需要解析结果时，读取 `--out` 导出的 JSON；标准输出可能同时包含日志。自动化调用建议使用绝对路径，避免宿主工作目录变化影响文件位置。`--session` 和 `--sleep` 等全局参数放在子命令之前。

更多参数见 [查询与下载](xuexitong/references/queries.md)。读取题目、生成答案、上传、暂存及交卷见 [作业操作](xuexitong/references/homework.md)。

## 会话与访问策略

| 项目 | 默认行为 |
| --- | --- |
| 会话位置 | `~/.xuexitong/session_cookies.json`，其中 `~` 是当前用户主目录 |
| 自定义目录 | 在启动前设置 `XUEXITONG_HOME` |
| 指定会话 | `--session /absolute/path/session.json`；同一账号建议固定使用同一个文件 |
| 请求间隔 | 至少 2 秒，同一会话文件的命令串行执行 |
| 验证与限流 | 检测到验证后停止并冷却 15 分钟；HTTP 429 冷却至少 60 秒 |
| 写操作 | 上传、暂存和交卷按用户授权执行；交卷需 `--confirm`，写请求不自动重放 |

Cookie 是本地明文凭据，应保存在仅当前用户可访问的目录。不要将 Cookie、带鉴权参数的作答链接、作业快照或个人资料提交到仓库和公开 Issue。

本地运行仍受学习通的登录、权限和风控规则约束。遇到验证时，在官方页面完成验证并等待冷却；请求结果不确定时先回读确认。这里的“本地”指客户端与会话，交给云端模型的对话或文件仍受宿主 Agent 的数据处理方式影响。

## 常见问题

<details>
<summary><strong>需要配置 OpenAI API Key，或部署后端吗？</strong></summary>

本项目不需要单独的模型密钥，也没有必须部署的服务端。已有 Agent 负责模型和对话；Python 客户端负责访问学习通。直接使用 CLI 时无需模型。

</details>

<details>
<summary><strong>查待办为什么没有显示活动和考试？</strong></summary>

默认 `todos` 只查全局未交作业。活动需要 `--include-activities`，考试需要 `--deep`，同时用 `--course` 指定课程。只有确实需要扫描全部课程时，才使用 `--all-courses`。未扫描板块返回 `null`，不表示没有待办。

</details>

<details>
<summary><strong>能自动签到、刷课、完成视频任务或答考试吗？</strong></summary>

当前没有实现这些接口。章节、考试和讨论能力用于查看列表与状态；作业写入能力用于处理用户指定并授权的内容。

</details>

<details>
<summary><strong>为什么目录、题量或查询结果可能不完整？</strong></summary>

资料目录当前不自动翻页，递归有深度上限，抓题也有页数上限。页面改版、课程权限和部分接口失败同样可能影响结果。请检查结果中的错误与未展开标记；详细限制见查询和作业文档。

</details>

## 项目结构与开发

```text
xuexitong/
  SKILL.md                 Agent 执行入口
  agents/openai.yaml       技能展示信息
  references/              按需读取的查询和作业说明
  scripts/
    xuexitong.py            CLI 入口，支持 26 个子命令
    qr_login.py             扫码登录
    chaoxing/              会话、课程、作业、资料和学习信息
    chaoxing_hw.py          历史 Python 导入兼容层
    requirements.txt       运行依赖
tests/                     禁用真实 HTTP 的离线回归测试
```

在已安装运行依赖的环境中执行 `python tests/run.py`。测试覆盖解析、会话存储、作业写入流程、查询范围、请求停止和本地互斥；离线通过不代表真实账号和当前在线接口已经验收。

开发环境、验证方式和变更约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。发现问题或有功能建议，请使用 [Issues](https://github.com/Jane-o-O-o-O/xuexitong-skill/issues/new/choose)，附上脱敏后的复现信息。
