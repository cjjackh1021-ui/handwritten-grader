"""实验报告智能批改系统 - 核心批改模块包。

子模块:
- providers: 多 provider 统一 LLM 客户端 (DeepSeek/OpenAI/Anthropic/自定义) + 视觉。
- extract:   PDF/DOCX/TXT 文本提取、OCR、需视觉复核页面渲染。
- convert:   DOCX -> PDF 转换 (LibreOffice, 缺失时降级)。
- grading:   评分编排与 prompt。
- stamp:     在 PDF 上盖红色手写分数与末尾评语, 打包 ZIP。
- summary:   学生分组 Excel 汇总与回读重生成。
"""

__all__ = [
    "providers",
    "extract",
    "convert",
    "grading",
    "stamp",
    "summary",
]
