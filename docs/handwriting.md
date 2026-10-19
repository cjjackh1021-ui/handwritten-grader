# 本地手写识别与逐题批注

图片、扫描 PDF 默认 `hybrid`。可靠文本层的 PDF 使用原文和原生文字框；所有识别结果保存来源、文本框和阅读顺序。PaddleOCR 低置信度、两引擎差异随原图传给所选视觉模型确认。模型不可用或不能确认时保留待复核状态，不把无法识别当作空白。

## 初始化与启动

Windows Python 3.12：

```powershell
.\.venv\Scripts\python.exe scripts\setup_handwriting.py --components handwriting paddle chandra
.\.venv\Scripts\python.exe app.py
```

三个组件分别在 `.venv-handwriting`、`.venv-paddle`、`.venv-chandra` 运行，Flask 不加载 Paddle/PyTorch。模型与字体仅初始化时下载，批改请求禁止模型联网下载；不会新增 OCR 云服务。初始化下载较大文件时采用可续传分块下载，完成后核验 SHA-256。

- PaddleOCR 3.7.0 / PaddlePaddle 3.2.0 / PaddleX 3.7.2，`PP-OCRv6_medium`，默认 CPU。
- Chandra 0.2.0，`chandra-ocr-2`，本地 HF 推理、CUDA、bf16，单页串行，默认空闲 60 秒释放工作进程。显存不足、权重缺失、超时会返回明确错误并保留 Paddle 结果。12GB 显卡不能保证所有页面都可处理。
- handrightbeta 8.7.0 / Pillow 10.4.0，李国夫手写体优先、神韵英子楷书备用。实际字形检查发现李国夫的 ✓ 是空白字形，渲染时使用同项目备用字体；不能只用 cmap 判断字形存在。

固定上游提交、模型 SHA-256、字体 SHA-256 位于 `workers/components.lock.json`。各环境安装清单位于 `workers/locks/`，初始化成功标志位于 `models/<engine>/ready.json`。`/config` 返回是否初始化及本进程最近一次运行状态；初始化不等于 GPU 已通过单页验证。

可选环境变量见 `.env.example`：`HANDWRITING_OCR_MODE`、`PADDLE_PYTHON`、`CHANDRA_PYTHON`、`HANDWRITING_PYTHON`、各组件 `*_TIMEOUT`、`LOCAL_WORKER_IDLE_SECONDS`、`CHANDRA_MIN_FREE_MB`。不要把 API Key 写入任务数据。

## 批改与复核

自动识别模式将 `17(1)`、`17(2)` 分别保存，题目关联 OCR 块并计算区域；视觉补充区域须在页面内，检查相邻作答重叠和明确题号冲突。每题使用自身区域裁图；跨页题只在末段作答旁落一个标记。

- 得分等于满分：✓；部分得分：内核生成的勾与小叉组合；确认零分（含确认空白）：×。
- 识别、评分、位置存在疑问：不落笔；确定题正常输出。
- 标记优先放作答右侧空白，检查 OCR 框、其他作答区域及原图深色像素。不安全时保留位置待复核。
- 网页「待复核 / 预览」可对照原图裁片与两份 OCR 文本。修改原文后仅重批当前题；人工改分和调整位置不调用模型。
- 区域可在原图拖动重画；本页同类区域被替换，跨页区域保留。标记可框选空白位置或拖动已有标记。
- 未解决识别/评分问题时总分显示「暂计」。仅位置待确认时分数不重复计算。草稿 PDF 汇总页列出待复核题号。

图片按 EXIF 方向生成规范化页面；PDF 使用 CropBox 与页面旋转；数据保存规范化坐标到原图像素/PDF 坐标的仿射映射。首版不做页面展平，模型须将严重模糊、缺边、弯曲和冲突未解决的题目交给人工复核。

手写图层为透明 PNG。种子由任务作答 ID、小题 ID、内容确定，不使用白色矩形遮盖原文。重新生成从原稿开始，保留原始 PDF 内容、文本层和 `/Ink` 批注；预览直接使用导出 PDF 渲染结果。

## 接口与持久化

`POST /grade` 保留原参数，新增：

- `ocr_mode=hybrid|paddle|tesseract|off`
- `annotate_questions=true|false`
- `handwriting_style=liguofu|kaishu`

`GET /review/<job_id>/<report_id>` 返回作答、页面与逐题状态。

`GET /preview/<job_id>/<report_id>/<page>` 返回原图；加 `?marked=true` 返回导出页；加 `?question=<qid>` 返回原图小题裁片。

`POST /review/<job_id>/<report_id>/<qid>` JSON：

- `{"action":"score","score":1.5,"confirm_answer":false}`：人工改分，可选确认现有转写。
- `{"action":"transcript","student_answer":"...","answer_state":"present","provider":"deepseek","api_key":"..."}`：确认原文并重批；确认空白使用 `answer_state=blank` 与空字符串；无法识别的状态不能当空白提交。
- `{"action":"location","regions":[{"page":1,"role":"answer","bbox":[0.1,0.2,0.6,0.4]}],"placement":{"page":1,"bbox":[0.7,0.3,0.8,0.4]}}`：调整区域与标记。所有坐标均为规范化页面左上角起的 0..1 坐标。

`schema_version=2` 保存 `report_snapshot`、`pages[].metadata/ocr`、逐题稳定 ID、区域、三类复核标志、标记位置及人工修正历史。旧任务使用缺省值；缺少题目数据的旧任务仍可汇总重生成，需要重新批改才能逐题复核。JSON 不保存 API Key。

Excel 的「逐题明细」只编辑分数列；识别文本与位置在网页修改。作答 ID、题目 ID 防止同名文件串改。旧 Excel 的同名文件存在歧义时拒绝合并。总分与逐题分数同时修改且矛盾时明确报错。

## 验证与真实样本评测

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
.\.venv\Scripts\python.exe scripts\handwriting_demo.py
.\.venv\Scripts\python.exe -m eval.handwriting_eval --labels D:\samples\labels.jsonl --out eval\handwriting-results --provider deepseek --model deepseek-flash
```

测试使用独立临时输出目录，保留原有 28 项测试。新增回归覆盖真实 PNG HTTP 上传、未知/空白区分、部分分标记、跨页题、EXIF、PDF 四种旋转及 CropBox、Ink 保留、透明图层与确定性、重复重生成、Excel 稳定 ID、逐题复核和重启恢复。`handwriting_demo.py` 生成的是合成展示样例，不能作为识别准确率数据。

人工样本 JSONL 格式见 `eval/handwriting-labels.schema.json`。每页须标记 `human_verified=true`，原文保留学生错误；作答及公式框标在 EXIF 规范化页面上。评测分别跑 Tesseract / Paddle / 双引擎，输出 CER、公式裁图严格匹配率、题目定位 IoU≥0.5 命中率及每页耗时。定位指标需要调用拆题模型或使用 `--predictions` 输入已有预测；没有数据时为 null，不伪造指标。模型 Key 从环境读取，不写入报告。

**目前没有用户提供的人工标注样本。30 页、200 小题的真实准确率验收尚未完成，不能据此宣称双引擎识别效果提升。**

## 本机验证记录（2026-10-19）

- 原有 28 项及新增 21 项回归测试，共 49 项通过；另有浏览器桌面/窄屏检查、人工改分预览更新和两个命令行入口的产物校验。
- RTX 4070 SUPER 上 Chandra 首次简单单页用时 17.656 秒，接着处理 1240×1754 合成测试页用时 17.125 秒。峰值分配显存分别为 10489 MiB、10189 MiB；这些是两次功能探测，不能外推平均吞吐量。
- 同一合成页双引擎流程约 45.359 秒，成功保留两份结果与 18 个文本块，Paddle 的低置信度项被记录。
- 已验证显存门槛不足时返回“增强识别未完成”，不提交新增 OCR 云服务。正常退出后工作进程释放显存。
- 真实 API 评分的回归测试使用响应桩；没有使用用户 API Key 发起付费评分调用。单页 OCR 探测使用实际本地模型。
- 合成页只能检查转写、坐标、符号及管线；简单探测中题号与算式仍可能被连写，不能当作准确率达标证据。

## Codex 原生技能

`scripts/install_codex_skill.py` 安装 `grade-handwritten-work`；具体入口见 [技能说明](../skills/codex/.agents/skills/grade-handwritten-work/SKILL.md)。当前会话模型负责逐题评分，命令行桥接器不会调用外部评分 API。它支持提取、定位裁图、评分校验与导出、转写后单题重批、位置调整和 Excel 重生成。使用新输出目录保留每次修订，原稿发生变化会被 SHA-256 校验拒绝。

2026-10-19 增补验证：总计 56 项测试通过（原 49 项及技能流程 7 项）。安装后的入口从项目外目录可用，实际完成 PaddleOCR + Chandra 双引擎提取及四种题目状态的合成导出检查。合成测试依然不是人工手写准确率验收。

## 上游与许可记录

- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)：Apache-2.0，许可副本 `assets/third_party/PADDLEOCR_LICENSE`。
- [Chandra](https://github.com/datalab-to/chandra)：代码 Apache-2.0；模型使用单独的修改版 OpenRAIL 许可。分别保留 `CHANDRA_CODE_LICENSE` 与 `CHANDRA_MODEL_LICENSE`，模型目录保留权重原始 LICENSE。
- [handwriting-web](https://github.com/14790897/handwriting-web)：保存项目 MIT 许可、Handright 内核许可、字体来源与校验值于 `assets/handwriting/`。仓库代码许可不自动替代字体自身的许可条件。
