# 批改 Agent 质量评测

本模块根据用户提供的 `claude-eval-learning.md` 设计说明实现；该目录原先只有方案文档，没有可直接导入的 Python 模块。实现把“批改学生答案”和“判断批改是否可靠”分开，接入现有真实评分函数，不另造一个模拟批改器替代产品。

## 能评什么

- `short`：简答题；`math`：数学步骤；`essay`：作文；分别调用 `graders.assignments.grade_submission`。
- `lab`：实验报告，调用 `graders.agent.grade_report_agent` 的工具循环，或 `graders.grading.grade_report` 单次模式。
- 原有 `multi/framework`、自动拆题和手写 OCR 的专用评测入口仍保留；新质量比较器不宣称覆盖这些额外引擎。
- 输入支持原文、TXT/MD、PDF、DOCX、照片和同一案例的多个附件。图像/扫描件要显式选 `--vision` 和适用的 `--ocr-mode`；Word 需要可用的转换器。OCR 与转换也计入端到端耗时和超时。

评测不在每次学生批改时自动运行，也不改写教师分值或默认评分策略。实验报告仍按现有 100 分制；不同分值量表放到独立 suite。

## 一次离线验证

在项目根目录运行；Windows 可将 `python` 替换为 `.\.venv\Scripts\python.exe`：

```powershell
python -m eval.quality audit --suite eval/examples/lab --out eval/runs/lab-audit.json
python -m eval.quality split --suite eval/examples/lab --out eval/runs/lab-split.json
python -m eval.quality run --suite eval/examples/lab --split-file eval/runs/lab-split.json --candidate eval/examples/baseline.json --replay eval/examples/lab/replay.json --out eval/runs/baseline
python -m eval.quality run --suite eval/examples/lab --split-file eval/runs/lab-split.json --candidate eval/examples/evidence.json --replay eval/examples/lab/replay.json --out eval/runs/candidate
```

每次 `run` 打印独立运行目录。将实际路径传入：

```powershell
python -m eval.quality compare --baseline <基线运行目录> --candidate <候选运行目录> --out eval/runs/comparison.json --hypothesis "每项扣分附原文证据可减少无依据扣分" --history eval/runs/history.json --max-rounds 5
```

`eval/examples/` 有四类任务、36 个合成案例，每类 3 个题目族，每族含正确、部分得分和需复核的变体。它们的 `human_verified` 都是 `false`。`replay.json` 是人工编写的服务响应夹具，通过真实 HTTP 解析层、评分校验和工具循环执行，完全不访问模型服务。它只验证功能连通，不是模型预测，不能据此报告真实准确率或改进。

回放仅支持 OpenAI 兼容协议；lab 夹具是 agent 的工具调用消息，请沿用默认 `--engine agent`。本模块也不测量当前 Codex 对话模型本身。

## 真实评测数据

真实样本建议存放在被 Git 忽略的 `eval/private_datasets/<suite>/`。一个 suite 包含：

1. `suite.json`：`id`、`task_type`、`subject`、`rubric_version`、`judge_version`、`input_mode`，均为非空文本。版本变化会改变指纹。
2. `cases.jsonl`：每行只有 `id/group_id/stratum/input`。`id` 是安全的唯一名称；同一题的变体、同一学生的相关材料等按评测目的归到同一 `group_id`；`stratum` 用于题型难度/输入类型等分层，同一组只能属于一层。
3. `labels.private.jsonl`：独立人工标注，ID 与全部案例一一对应。**不传给批改模型。**
4. 可选公开附件，路径相对于 suite，必须位于该目录内。

`input` 只允许：

```json
{
  "student_id": "anonymous-001",
  "student_name": "匿名样本",
  "answer": "学生原始作答，不按参考答案修正",
  "files": [],
  "task": {
    "task_type": "short",
    "question": "公开题干",
    "reference": "公开参考答案",
    "full_score": 10,
    "points_text": "要点A | 10"
  },
  "rules": [
    {"id": "A", "name": "要点A", "full": 10, "reason_codes": ["wrong", "missing"]}
  ]
}
```

非 lab 的 `task` 使用已有 `TaskSpec`，公开规则的名称、顺序及分值须与其得分点一致。lab 则使用 `criteria` 文本和总分 100 的 `rules`，不需要 `task`。公开评分规则属于批改必需输入；教师给这个学生打几分、扣分证据锚点和复核答案属于隐藏标注。

隐藏标注示例：

```json
{
  "id": "case-001",
  "score": 6,
  "dimensions": {"A": 6},
  "review_required": false,
  "human_verified": true,
  "tolerance": 0,
  "dimension_tolerance": 0,
  "deductions": [
    {"rule_id": "A", "reason_code": "wrong", "evidence_any": ["学生写错的原文片段"]}
  ]
}
```

每个扣分项一条标注，可以提供多个可接受的引用锚点。缺失内容用 `reason_code="missing"`、`evidence_any=[""]`，并在模型反馈中说明缺少什么。非复核案例的分项和必须等于总分。复核案例仍须有合法的占位分数和分项用于结构检查，但不用于明确可评分案例的 MAE，也不得当成最终分数交付。

当前裁判是**确定性的人工标注匹配器**：检查身份、总分/分项、复核决策、规则 ID、原因代码和原文引用。它不是能独立判断任意开放答案的语义模型；标注者应覆盖合理的证据锚点。图片中无法提取的引用由隐藏人工锚点验证；可选证据候选策略对没有可核对原文的扣分会转复核，比较时由覆盖率护栏约束。

## 健康检查与数据隔离

先用正例检查裁判稳定性，再测试全满分、全零分、分数正确但理由错误、错学生、编造原文和一律复核。各 suite 须包含清晰可批改、真实需复核、扣分案例，反例未被识别时禁止运行。

`split` 按相关组做分层三划分：search / validation / locked-test。小样本不拆散题目族来凑数量。数据、标注和附件均进入 SHA-256 指纹，修改后不能沿用旧比较。

模型子进程只接收公开输入、候选策略、模型配置；真实运行不包含回放响应。隐藏分数和裁判在父进程评测阶段使用，不进入批改提示词或工具上下文。子进程使用独立工作目录，**这不是操作系统权限沙箱**，也不声称能阻止操作者手工查看测试标签。

## 配置候选策略

候选 JSON 只允许：

```json
{"name": "evidence-v1", "prompt_addendum": "关键文字不清时说明原因并请求复核。", "require_evidence": true}
```

`require_evidence` 要求每个扣分项提供 `rule_id/evidence/reason_code/reason`。程序核对公开规则、学生引用和扣分维度，缺失或不合法时将分数标为暂计并转复核，不修改分数。策略通过 ContextVar 仅作用于当前评测调用，普通批改不自动启用。

真实运行需对应 provider 的 API Key 环境变量（沿用项目配置），去掉 `--replay`，显式指定 `--provider/--model`。不要把密钥写到 candidate、数据集或命令行参数中。数据发送范围就是所选公开案例及必要图像，隐藏标注不发送。

## 报告和比较

每次运行保存 `config.json`、`audit.json`、`records.jsonl`、`report.json`、可展开的 `report.html`，以及逐次 `input.json/output.json/calls.jsonl`。HTML 包含原文/附件、评分、扣分证据、工具轨迹和判定；内容已转义。

- 所有请求都计入交付率、正确交付率、自动批改覆盖率和错误分类。超时、服务错误、无效输出、截断和执行错误分别保留；裁判失败单独计数。
- MAE 只用于已完成、无需复核且分数合法的样本，同时报告测量数。全失败时是 `null`，不是 0。版本比较另外用“失败/不当弃答按满分量表惩罚”的误差护栏，避免缩小成功样本分母获利。
- 每次 HTTP 尝试在开始时落盘，结束时更新实际模型名、返回的 token 用量、结束原因和耗时。重试不丢弃；超时中断的在途请求保留未知用量。真实调用无用量时不能通过 token 护栏。
- 端到端耗时含转换、识别、模型调用、退避和工具处理。未配置价格，货币成本明确为 `null`；不能把 token 数当实际账单。
- 比较先校验明细哈希并重算指标。两个版本必须有完全相同的案例和重复编号、数据、代码、划分及运行配置。默认配置/model 会解析后保存；服务实际返回的版本记录在各次 calls 中。
- 重复调用先按案例平均，再按相关组做 2,000 次 bootstrap，给出配对差值和 95% 区间，不把重复调用当新样本。
- 主要目标是减少无依据扣分，同时限制覆盖率、交付率、正确交付率、分数误差、延迟和 token 退化。延迟/token 采用总量或均值比护栏，证据与质量指标采用分组区间。默认至少每份划分 5 个独立组且全部经人工标注；此门槛仅是最低检查，不保证统计功效。

`--guards` 可以在实验开始前配置护栏；`--history` 锁定数据、实现、基线和护栏，要求写下假设，保存候选 JSON 差异，默认最多 5 轮，连续三轮无可采纳结果停止。search 用于探索；只有 validation 满足样本要求并通过所有护栏才推荐候选。回放、无真实标签或不足样本始终不会得到采纳结论。

最终确认：使用已通过的 validation 比较 JSON 作为 `--final-selection`，分别用冻结的基线和候选执行 `--partition locked-test`，再 compare。配置、重复数、代码和数据须一致；划分旁的 `.locked-test.json` 登记每个版本一次使用，防止在同一实验中反复查看。失败也消耗这次确认机会；需要修复实验时建立新实验并说明原因，不能把失败删除后当首次确认。ledger 和标签都在本机，这是一项可审计流程约束，不是抗篡改隔离。

## Agent 与技能入口

- MCP：`prepare_grading_eval`、`run_grading_eval`、`compare_grading_eval`。
- 已安装 `grade-lab-reports`：`python <技能目录>/scripts/run.py eval ...`，使用链接项目的 Python 环境；不会把日常 Codex 原生批改切换成 API 调用。
- 原有 `python -m eval.run_eval` 保留，适合旧数据的演示；已补上失败明细和交付率，发生失败不再按幸存样本 MAE 宣称最佳引擎。新版本选择请用 `eval.quality`。
- 测试：`python scripts/run_tests.py`。测试只用合成夹具和临时目录，无需 API Key，不上传学生材料。

当前未提供真实人工标注样本，也未配置评测 API Key。本次接入验证覆盖功能和回归，不构成真实批改准确率提升、OCR 效果提升或真实 token 成本的验收。
