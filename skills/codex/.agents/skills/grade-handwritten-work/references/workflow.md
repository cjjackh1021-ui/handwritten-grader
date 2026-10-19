# 命令与数据格式

命令中的 `HW` 代表本技能绝对路径 `scripts/handwritten.py`。可用任意 Python 启动包装器，包装器自动切换项目主环境。Windows 示例：

```powershell
& 'D:\自动批改程序\.venv\Scripts\python.exe' -X utf8 'C:\Users\Administrator\.codex\skills\grade-handwritten-work\scripts\handwritten.py' doctor
```

其他机器请从技能 `local.json` 读取路径，不照抄机器示例。以下缩写 `python HW` 同理。所有路径建议绝对路径；JSON 用 UTF-8；每个 `--output` 是新的空目录。命令输出 JSON，退出码 1 表示保留了部分失败项，2 表示输入无效；须检查并反馈。

## 1. 提取与页面规范化

`input.json`：

```json
{
  "reference_notes": "参考答案路径及评分标准说明；此字段只是记录，Codex 需另行读取答案",
  "submissions": [
    {"student_name":"张三","student_id":"001","files":["D:/作业/张三/1.jpg","D:/作业/张三/2.jpg"]},
    {"student_name":"李四","student_id":"002","files":["D:/作业/李四.pdf"]}
  ]
}
```

```text
python HW prepare --manifest input.json --output run/prepare --ocr-mode hybrid
```

图片支持 JPG/PNG/BMP/WebP；PDF 保留文本层及原始页面映射。`files` 顺序即页序，路径相对于 input.json，或使用绝对路径。严格排除参考答案；程序不猜学生分组。

输出 `packet.json`：`reports[].report_id` 为作答 ID；`pages[].render_path` 是已按 EXIF/PDF 旋转规范化的图像，`evidence[].sources` 保留不同 OCR 转写，`evidence[].blocks[].id` 含 `p1:` 页前缀。`status=error` 的学生保留失败原因，不能默默忽略。

## 2. 题目关联与裁图

`questions.json` 每个提取成功的 report_id 一项：

```json
{"reports":[{"report_id":"从 packet 原样复制","questions":[{
  "no":"17(1)","type":"math","question":"求 2+3 的值","full":3,
  "student_answer":"2+3=5","answer_state":"present","transcription_confirmed":true,
  "reference":"5","reference_source":"teacher","rubric":"结果正确得 3 分",
  "question_block_ids":[],"answer_block_ids":[],
  "location_confident":true,
  "regions":[{"page":1,"role":"question","bbox":[0.1,0.1,0.6,0.16]},
             {"page":1,"role":"answer","bbox":[0.1,0.18,0.6,0.27]}],
  "review_reasons":[]
}]}]}
```

```text
python HW locate --packet run/prepare/packet.json --questions questions.json --output run/layout
```

坐标均基于 `render_path` 图像：左上角为 `(0,0)`，右下角为 `(1,1)`，bbox 为 `[左,上,右,下]`。优先提供真实 OCR block IDs，程序计算区域；无法覆盖的视觉区域用 `regions` 补充，只有实际看过页面确认后才设 `location_confident=true`。同页同角色已有 OCR 区域时优先采用 OCR 区域，因此不完整的 OCR 块不要强行关联，改用完整视觉区域。

`answer_state` 必填：`present` 有作答、`blank` 确认空白（student_answer 为空）、`unreadable` 无法辨认。无法确认时 `transcription_confirmed=false`，并写具体原因。`recognition_review=true` 或 `position_review=true` 可显式保留疑问。题干也不完整时应保留 recognition_review；不要猜缺失条件。

输出 `layout.json` 中每题有 `id`、`crop_paths`、复核状态；`grades.template.json` 是评分模板。裁图为同题题干和作答区域的联合范围，需核对没有遗漏；多栏时尽量使用紧凑区域，邻题内容不作为本题证据。

## 3. 评分和导出

复制模板成 `grades.json`。保持 `report_id`、`question_id`、`question_digest` 原样；只填写：

```json
{"grades":[{
  "report_id":"...","question_id":"...","question_digest":"...",
  "score":3,"grading_confirmed":true,"comment":"计算正确","reason":"原图作答 2+3=5，与参考答案一致，得 3 分"
}]}
```

满分必须来自卷面或已明确的标准，缺少标准需说明拟采用规则；无法确认规则时该题保留待复核。不能凭空造 100 分。分数必须在 `[0, full]` 内，可含小数。未能确认的题写 `score:null, grading_confirmed:false`，reason 记录原因。首次导出要求所有题都在 grades 中，防止漏题。

```text
python HW export --batch run/layout/layout.json --grades grades.json --output run/export
```

默认 `--style liguofu`，可选 `kaishu`，均来自 handwriting-web。输出包含 `marked/`（PDF、逐页 PNG）、`批改结果.xlsx`、`batch_results.json`、`review_pending.json`、`grades.template.json`（待复核题）和 `已批改作业.zip`。ZIP 包含批改文件、成绩表、状态和复核资料；包含学生信息，只交付用户指定位置。

`score_pending=true` 的零分只是暂计占位；`grading_review` / `recognition_review` / `position_review` 任一个未解决均不落笔。位置疑问不使已确认分数失效。标记由程序计算，不在 JSON 自定 mark。

## 4. 单题复核

`changes.json`：

```json
{"changes":[
  {"report_id":"...","question_id":"...","action":"score","score":2,"confirm_answer":false},
  {"report_id":"...","question_id":"...","action":"transcript","student_answer":"x=2","answer_state":"present"},
  {"report_id":"...","question_id":"...","action":"location",
   "regions":[{"page":1,"role":"answer","bbox":[0.1,0.2,0.6,0.3]}],
   "placement":{"page":1,"bbox":[0.8,0.25,0.87,0.29]}}
]}
```

三行表示三种操作示例，实际仅列用户要求修改的题；按顺序执行。location 的 regions 为该题完整区域列表，修改一页时保留其他页。placement 可省略，让程序重新寻找空白位置。

```text
python HW review --batch run/export/batch_results.json --changes changes.json --output run/review-1
python HW apply-grades --batch run/review-1/batch_results.json --grades regraded.json --output run/review-2
```

transcript 只修改转写并使旧分失效，不调用 API。读取新 `grades.template.json`，仅保留要重批题，查看对应裁图并重新评分再 apply-grades。其他题不得重复判分。成绩摘要与题干、转写、分值、参考、区域绑定，旧评分将被拒绝。

## 5. Excel 改分

```text
python HW regenerate --batch run/export/batch_results.json --xlsx edited.xlsx --output run/excel-revised
```

只修改「逐题明细」分数列，保留两种 ID。改总分属于独立人工调整；同时修改总分和逐题分数须一致。改分不会清除字迹疑问；需要通过 review 明确确认转写。原输出与人工编辑文件均保留。
