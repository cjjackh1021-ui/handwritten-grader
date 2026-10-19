# 配套的多模型 Flask 网页版

本 skill 是「Agent 原生评分」路线：由当前 Codex/Claude 模型用自身阅读与视觉能力评分，不调用外部评分 API。

如果需要**无人值守、通过 API 批量评分并在网页上一键切换模型**，请使用同仓库根目录的 Flask 网页版：

- 入口：`python app.py`，浏览器访问 `http://127.0.0.1:5000`。
- 网页可填 API Key、选择 provider（DeepSeek / OpenAI / Anthropic / 自定义 OpenAI 兼容）与模型，实时切换。
- 图片密集页对支持视觉的模型发送页面截图做多模态评分，不支持时回退 Tesseract OCR/文本。
- 产出与本 skill 一致：学生分组 `批改结果.xlsx`、盖分 PDF 的 `已批改实验报告.zip`。
- 网页后端复用了与本 skill 等价的提取、盖分、汇总逻辑（`graders/` 包）。

两条路线互补：
- skill 适合在 Codex/Claude 环境里以模型自身能力精评、图多报告质量最高。
- 网页版适合固定用某个 API 模型批量跑、非 Agent 环境部署。
