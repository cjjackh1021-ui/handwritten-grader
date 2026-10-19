"""集中式配置与运行时探测。

- provider 默认值 (base_url / 默认模型 / 是否视觉能力)
- Tesseract 与 soffice 可执行文件的自动探测
- 输出目录管理
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
WORK_DIR = BASE_DIR / "output" / "_work"
REFERENCES_DIR = BASE_DIR / "references"
DEFAULT_CRITERIA_FILE = REFERENCES_DIR / "default-grading-criteria.md"


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Provider 预设
#   base_url 指向各家的 chat/completions 或 messages 根地址。
#   vision: 该 provider 是否提供视觉模型; vision_models 可限定支持图片的模型名。
#   kind:   请求协议族, openai 兼容 或 anthropic messages。
# ---------------------------------------------------------------------------
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "vision": True,
        # 官方文档 https://api-docs.deepseek.com/quick_start/pricing/ (2026-10-19)
        # 两个旧 Flash 名称仍由最新 Flash 提供服务; V4 Pro 不支持图片。
        "vision_models": ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"],
        "env_key": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "vision_model": "gpt-4o",
        "vision": True,
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-latest",
        "vision_model": "claude-3-5-sonnet-latest",
        "vision": True,
        "env_key": "ANTHROPIC_API_KEY",
    },
    "custom": {
        "label": "自定义 (OpenAI 兼容)",
        "kind": "openai",
        "base_url": "",  # 由用户在网页填写
        "default_model": "",
        "vision_model": "",
        "vision": True,  # 允许尝试, 失败回退 OCR
        "env_key": "CUSTOM_API_KEY",
    },
}


def model_supports_vision(provider: str, model: str = "") -> bool:
    """按具体模型判断图片能力; 未列出模型清单的服务商保留原有行为。"""
    preset = PROVIDERS.get(provider, {})
    if not preset.get("vision", False):
        return False
    models = preset.get("vision_models")
    selected = model.strip() or preset.get("default_model", "")
    return models is None or selected in models


def provider_public_config() -> list[dict]:
    """返回给前端使用的 provider 列表 (不含任何密钥)。"""
    items = []
    for key, cfg in PROVIDERS.items():
        items.append(
            {
                "id": key,
                "label": cfg["label"],
                "base_url": cfg["base_url"],
                "default_model": cfg["default_model"],
                "vision_model": cfg["vision_model"],
                "vision": cfg["vision"],
                "vision_models": cfg.get("vision_models"),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 可执行文件探测
# ---------------------------------------------------------------------------
def find_tesseract() -> str | None:
    env = os.getenv("TESSERACT_CMD")
    if env and Path(env).is_file():
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def find_soffice() -> str | None:
    env = os.getenv("SOFFICE_CMD")
    if env and Path(env).is_file():
        return env
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None
