# 作业与附件

[技能入口](../SKILL.md) · [查询与下载](queries.md)

## 调用约定

下面的完整命令使用 `python "SKILL_DIR/scripts/xuexitong.py"` 作为入口。替换大写占位符，文件路径使用绝对路径，先创建 `OUT_DIR`。

`TASK_URL` 来自所选作业的 `taskUrl` 或 `works.detailUrl`；`WORK_URL` 来自该份作业最新解析出的 `doHomeWorkUrl`。所有步骤绑定同一账户和作业，不能跨账户、跨作业复用链接或题号。

## 读取题目与本地草稿

先用 `pending` 列表核对课程、作业名、状态和剩余时间，再对用户选择的那一份作业执行：

```text
python "SKILL_DIR/scripts/xuexitong.py" resolve --task-url "TASK_URL" --out "OUT_DIR/resolved.json"
python "SKILL_DIR/scripts/xuexitong.py" fetch --dohomework-url "WORK_URL" --out "OUT_DIR/work.json"
python "SKILL_DIR/scripts/xuexitong.py" template --work-json "OUT_DIR/work.json" --out "OUT_DIR/answers.json"
```

`resolved.json` 中的链接用于替换后两条命令中的 `WORK_URL`。电脑端链接会通过全局列表匹配同一作业，再解析移动端入口；找不到唯一匹配、课程或班级不符时停止，不自动选另一份作业。已交或过期的入口可能关闭。

`fetch` 默认只读一页；`--max-index` 是本批页数上限，`--start-index` 是从 0 开始的起始页。输出 `startIndex`、`nextIndex` 和每题的 `index`，需要下一页时读取 `nextIndex` 后继续，并使用不同的输出文件名保留前批内容。例如：

```text
python "SKILL_DIR/scripts/xuexitong.py" fetch --dohomework-url "WORK_URL" --start-index NEXT_INDEX --max-index 1 --out "OUT_DIR/work-next.json"
```

`nextIndex` 不保证下一页存在。遇到重复题号即停止，不连续探测重复页；不同批次仍应按题号核对。输出 `stopReason` 为 `repeated_question`、`no_question`、`max_index` 或 `request_stopped`；`limitReached: true` 时不能宣称完整读取。因请求预算或验证停止时，先写入已成功读取的题目及续读位置，再以退出码 3 结束。零题可能是入口关闭或解析失败，不表示没有作业要求。

`work.json` 保留题目和当前作业上下文。`template` 生成 `{题号: 答案}` 映射，只写本地文件，不会暂存或交卷。

## 题干图片与附件下载

每题保留 `stem`、完整嵌套的 `stemHtml`、`images`、`attachments`、`options` 和 `contentWarnings`。选项包含实际 token，能识别标签时附 `label` 和 `html`。图片 URL 可能相对当前作答页；`stemHtml` 用于检查内容，不直接当成可信脚本执行。

附件元数据包含 `objectId`、`name`、`type`、`size`。只有附件、没有文字的题目是有效内容；不能因为 `stem` 为空就跳过实验要求。无法解码的附件或未知 iframe 会写入 `contentWarnings`，此时结合 `fetch --save-html OUT_DIR/pages` 或官方页面核对。

```text
python "SKILL_DIR/scripts/xuexitong.py" download-attachments --work-json "OUT_DIR/work.json" --out "OUT_DIR/files" --result-json "OUT_DIR/downloads.json"
```

默认只接受一个附件；有多个时用 `--object-id OBJECT_ID` 选择，`--question-id QID` 可进一步限定题号。只有用户明确需要批量下载时才加 `--all-files`，仍受每条查询 8 个请求的预算限制，已成功的对象 ID 不要重复下载。命令只下载题干附件，不上传或修改作答。它按官方电脑端预览流程读取 `/mooc-ans/ueditorupload/read`，只使用预览页提供的对应文件下载链接，不探测其他对象接口。没有下载链接时报告限制。

文件名含题号及对象 ID，下载完整后原子替换。HTML/JSON 错误页、空文件或已声明长度不符不会覆盖旧文件。检查结果清单的 `success`、`failed`、`total` 和每个文件的 `bytes`；遇到验证会立即停止并保留已完成记录。签名链接不写入结果清单。

## 答案格式

```json
{
  "10001": "A",
  "10002": "AB",
  "10003": "plain:第一段文字\n第二段文字",
  "10004": "@C:/Users/example/Documents/report.pdf"
}
```

示例题号和答案无实际含义，实际题号必须来自当前 `work.json`。

| 值 | 处理方式 |
| --- | --- |
| `"A"`、`"AB"` | 按原字符串发送；选择题使用页面真实选项 token |
| `"plain:文字"` | 转为转义后的段落 HTML，适合纯文本简答 |
| 原始 HTML 字符串 | 原样发送，需核对格式和内容 |
| `"@绝对文件路径"` | 保存或提交时上传文件，再填入附件答案 |
| `""` 或 `null` | 写入空答案，不会自动跳过 |

相对附件路径按进程当前工作目录解释，因此优先使用绝对路径。模板中没有填写的键在写入前逐项核对；只保留确实要写入的题号，避免覆盖已有答案。

## 暂存、回读与交卷

只有用户授权了具体作业和对应内容后，才执行远端写入：

```text
python "SKILL_DIR/scripts/xuexitong.py" save --dohomework-url "WORK_URL" --work-json "OUT_DIR/work.json" --answers "OUT_DIR/answers.json" --out "OUT_DIR/save-result.json"
python "SKILL_DIR/scripts/xuexitong.py" verify --dohomework-url "WORK_URL" --work-json "OUT_DIR/work.json"
```

`save` 写入学习通草稿；`verify` 只读服务器上存储的作答和附件信息，输出文本。暂存成功不等于已经交卷。

最终交卷还需核对作业、所有目标题号、最终文字和附件版本：

```text
python "SKILL_DIR/scripts/xuexitong.py" submit --dohomework-url "WORK_URL" --work-json "OUT_DIR/work.json" --answers "OUT_DIR/answers.json" --out "OUT_DIR/submit-result.json" --confirm
```

`--confirm` 是 CLI 的操作约束，不替代用户授权。用户仅要求查看、生成草稿或暂存时，不执行交卷；已有清晰授权时也不必重复询问。

## 单独上传附件

```text
python "SKILL_DIR/scripts/xuexitong.py" upload --dohomework-url "WORK_URL" --file "ABSOLUTE_FILE_PATH" --out "OUT_DIR/upload.json"
```

返回 `objectId` 和可写入答案的 `html`。指定 `--out` 时保存完整结果，控制台只显示简短确认；不指定时仍输出 JSON。结果文件已存在时停止，先检查是否已有成功的附件记录；明确需要再次上传时选择新的结果路径。失败记录区分 `uncertain`，不表示上传一定没有生效。

可以使用成功记录中的 HTML，也可以直接在答案中用 `@文件路径`，由 `save` 或 `submit` 上传。两条路径择一，避免重复上传；单独上传不会自动把附件写入某道题的答案。

已持有作业列表返回的电脑端 `detailUrl` 时，也支持显式选择电脑端附件表单：

```text
python "SKILL_DIR/scripts/xuexitong.py" upload --pc-work-url "PC_WORK_URL" --file "ABSOLUTE_FILE_PATH" --out "OUT_DIR/upload-pc.json"
```

两种 URL 参数互斥。电脑端流程核对跳转前后的课程、班级、作业编号及可编辑区域，使用页面的上传签名和时间，验证响应的文件名、大小、类型与附件编号。不自动从另一种上传失败切换到此流程，也不重新上传已成功的附件。签名字段缺失、页面改版或响应异常时停止并说明。

## 结果不确定时

客户端逐题读取当前隐藏字段后发送写请求，不自动重试或跟随写请求的重定向。跨题操作没有事务保证，前面的题可能成功、后面的题可能失败。

超时、响应异常或被风控中断后，保留结果文件，区分已成功、明确失败和结果不确定的题号。先回读并结合官方页面确认，不用再次提交来试探。成功响应只代表对应请求被接受，应以最终作业状态判断是否交卷。

结果中的 `stage` 区分准备、上传、暂存和交卷；`uncertain` 针对答案写入，`attachmentUncertain` 针对附件上传。若附件成功而暂存/提交失败，`uploadedAttachment.html` 可供核对后复用。更多处理方式见 [异常排查](troubleshooting.md)。

最终交卷后作答入口可能关闭；无法回读时，可查询作业列表或由用户在官方页面确认。页面与接口可能变化，离线测试不能替代当前账户的在线结果。
