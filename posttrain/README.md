# 后训练 / Agent RL

批改任务有**可计算奖励**（贴近人工分、格式合法、按需看图），适合做后训练，不必先上 7B GPU。

```
人工标注 / 自校验
        │
        ▼
  score_rollout()     ← 结果奖 + 过程奖 + 格式奖
        │
        ├─ 高奖励轨迹 ──► SFT / RFT jsonl
        ├─ 同题优劣对 ──► DPO jsonl（含「校验失败 vs 修正提交」）
        └─ GradeEnv ──► GRPO（组内相对优势, 无 critic）
```

## 和「真 LLM-GRPO」的关系

| 本仓库已实现 | 换成大模型时 |
| --- | --- |
| `GradeEnv.reset/step` 调同一套 `agent_tools` | vLLM / TRL 里每步 tool call 接这个 env |
| `score_rollout` 标量奖励 | GRPO 的 outcome reward，无需单独训 RM |
| 工具 softmax + `group_advantages` | TRL `GRPOTrainer`，算法相同、策略换成 LoRA |
| `sft.jsonl` / `dpo.jsonl` | Axolotl / LLaMA-Factory / TRL SFT·DPO |

CPU 上的工具 GRPO 用来验证：**奖励能塑造「先取证再打分」**；不是冒充 7B 已训完。

## 命令

```bash
# 奖励 / env / GRPO 公式自测
python -m posttrain.run --self-test

# 随机策略采集 → 写出 SFT / RFT / DPO
python -m posttrain.run collect --dataset eval/sample_dataset --out posttrain/runs

# 工具选择 GRPO（数十秒, 无 GPU）
python -m posttrain.run grpo --dataset eval/sample_dataset --epochs 20 --out posttrain/runs

# 真实 agent 轨迹（网页批改后落在 output/<job>/agent_rollouts.json）
python -m posttrain.run from-agent --jobs output --dataset eval/sample_dataset --out posttrain/runs/agent

# 或现场再跑 agent（同题 --repeats 2 才能做组内 DPO）
python -m posttrain.run from-agent --live --dataset eval/sample_dataset \
    --provider deepseek --api-key sk-xxx --repeats 2 --out posttrain/runs/agent
```

`from-agent` 用模型真实的 tool-calling `messages`（不是玩具策略），调用同一个 `export_all`。网页选 Agent / 多智能体批改结束时会自动写 `agent_rollouts.json`；`--dataset` 里的 `labels.json` 按文件名对齐后重算奖励。

## 真要训本地模型时（可选, 需 GPU）

1. `collect` 或真实 agent 轨迹得到 `posttrain/runs/rft.jsonl`
2. LoRA SFT（Qwen2.5-7B 等）对齐 tool calling
3. 同一 `GradeEnv` + `score_rollout` 接 [TRL GRPO](https://huggingface.co/docs/trl/grpo_trainer)，组大小 G=4~8
4. 用 `eval.run_eval`（真人标注, 不要 mock）看 MAE 是否下降

没有 20+ 份真人标注就不要训 LLM，否则奖励会过拟合样例集。
