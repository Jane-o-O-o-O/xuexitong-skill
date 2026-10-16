# 作业与附件

[技能入口](../SKILL.md) · [查询与下载](queries.md)

## 调用约定

下面的完整命令使用 `python "SKILL_DIR/scripts/xuexitong.py"` 作为入口。替换大写占位符，文件路径使用绝对路径，先创建 `OUT_DIR`。

`TASK_URL` 来自所选作业的 `taskUrl`；`WORK_URL` 来自该份作业最新解析出的 `doHomeWorkUrl`。所有步骤绑定同一账户和作业，不能跨账户、跨作业复用链接或题号。

## 读取题目与本地草稿

先用 `pending` 列表核对课程、作业名、状态和剩余时间，再对用户选择的那一份作业执行：

```text
python "SKILL_DIR/scripts/xuexitong.py" resolve --task-url "TASK_URL"
python "SKILL_DIR/scripts/xuexitong.py" fetch --dohomework-url "WORK_URL" --max-index 30 --out "OUT_DIR/work.json"
python "SKILL_DIR/scripts/xuexitong.py" template --work-json "OUT_DIR/work.json" --out "OUT_DIR/answers.json"
```

`resolve` 的输出用于替换后两条命令中的 `WORK_URL`。`fetch` 按题号去重；超范围页可能重复最后一题。`--max-index` 是抓取页数上限，不保证覆盖更多题，达到上限或题目缺失时说明实际范围。

`work.json` 保留题目和当前作业上下文。`template` 生成 `{题号: 答案}` 映射，只写本地文件，不会暂存或交卷。

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
python "SKILL_DIR/scripts/xuexitong.py" upload --dohomework-url "WORK_URL" --file "ABSOLUTE_FILE_PATH"
```

返回 `objectId` 和可写入答案的 HTML。可以使用这段 HTML，也可以直接在答案中用 `@文件路径`，由 `save` 或 `submit` 上传。两条路径择一，避免重复上传；单独上传不会自动把附件写入某道题的答案。

## 结果不确定时

客户端逐题读取当前隐藏字段后发送写请求，不自动重试或跟随写请求的重定向。跨题操作没有事务保证，前面的题可能成功、后面的题可能失败。

超时、响应异常或被风控中断后，保留结果文件，区分已成功、明确失败和结果不确定的题号。先回读并结合官方页面确认，不用再次提交来试探。成功响应只代表对应请求被接受，应以最终作业状态判断是否交卷。

最终交卷后作答入口可能关闭；无法回读时，可查询作业列表或由用户在官方页面确认。页面与接口可能变化，离线测试不能替代当前账户的在线结果。
