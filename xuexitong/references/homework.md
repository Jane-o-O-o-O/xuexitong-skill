# 作业读取、暂存与提交

所有命令的入口都是 `python "SKILL_DIR/scripts/xuexitong.py"`。下文的 `URL` 是同一份作业最新解析出的 `doHomeWorkUrl`，不能从别的账户或课程复用。文件路径使用绝对路径。

## 读取与本地草稿

1. `pending --out PATH/pending.json` 列表；定位用户指定作业，检查课程、名称、状态及剩余时间。已截止或不确定的作业先在官方页面确认。
2. `resolve --task-url TASK_URL` 得到该份作业的 `doHomeWorkUrl`。
3. `fetch --dohomework-url URL --max-index 30 --out PATH/work.json` 逐页读取题目。题量上限是边界，不保证覆盖更多题；发现抓取不全时说明范围。超范围页可能重复最后一题，客户端按题号去重。
4. `template --work-json PATH/work.json --out PATH/answers.json` 生成本地 `{题号: 答案}` 映射。编辑此文件只产生本地草稿，不触发保存或交卷。

```json
{
  "10001": "A",
  "10002": "AB",
  "10003": "plain:第一段文字\n第二段文字",
  "10004": "@C:/Users/example/Documents/report.pdf"
}
```

题号必须来自当前 `work.json`；示例题号与答案无实际含义。选择题用页面真实选项 token，不能自动包装成 HTML；多选是字符串。简答题可用 `plain:`，按段落转成转义后的 HTML。原始 HTML 也可直接作为值。`@` 开头的值会在保存/提交时上传对应本地文件。相对文件路径按进程当前工作目录解释，推荐始终使用绝对路径。

空字符串或 `null` 会作为空答案写入，不会自动跳过。只操作确实要写入的题号，避免覆盖已存在答案。模板中未填写的键在暂存/交卷前要逐项核对。

## 写入与核对

已获用户对具体内容及操作的授权后：

```text
save --dohomework-url URL --work-json PATH/work.json --answers PATH/answers.json --out PATH/save-result.json
verify --dohomework-url URL --work-json PATH/work.json
submit --dohomework-url URL --work-json PATH/work.json --answers PATH/answers.json --out PATH/submit-result.json --confirm
```

`save` 暂存到学习通，会写入远端。`verify` 回读作答内容及附件信息，只读。`submit --confirm` 请求最终交卷；使用前核对所有目标题号、最终文字、附件版本和提交范围。`--confirm` 是 CLI 的操作约束，不能替代用户授权，也不要求在已有清晰授权时重复询问。

附件可独立上传：`upload --dohomework-url URL --file ABSOLUTE_PATH`，返回 `objectId` 与答案 HTML；也可让 `@文件路径` 在 `save`/`submit` 中上传。不要两条路径都执行而重复上传。

脚本按题号读取当前隐藏字段后发送写请求，不自动重试 POST。成功响应只表示对应请求被接受；根据最终作业状态和回读结果判断是否已交卷。它没有事务性保证，跨题操作可能部分成功。保存或交卷中断时保留结果文件，明确哪些题号已成功、哪些结果不确定，然后回读核实。最终交卷可能让作答入口关闭，无法回读时用作业列表或用户的官方页面确认，不再次交卷试探。

原项目的上传、暂存与提交曾有实测；本次整理仅做离线验证，没有访问任何真实账户或提交真实作业。接口行为变化时以官方页面为准。
