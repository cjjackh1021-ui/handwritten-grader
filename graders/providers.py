"""多 provider 统一评分客户端。

对外暴露单一入口 :func:`grade`，屏蔽各家 API 差异：

- DeepSeek / OpenAI / 自定义: OpenAI 兼容 ``/chat/completions``，强制 JSON 输出。
- Anthropic: ``/v1/messages``，在 prompt 中注入 JSON 约束。
- 视觉: 图片密集页把渲染好的 PNG 作为 image 块随请求发送。

返回统一结构::

    {"score": int, "comment": str, "reason": str, "raw": str}

任何异常都会抛出 :class:`ProviderError`，由上层决定是否回退到 OCR 文本重试。
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import config

REQUEST_TIMEOUT = 120

# 限流/瞬时故障重试
MAX_RETRIES = int(os.getenv("GRADE_MAX_RETRIES", "4"))
RETRY_BASE = float(os.getenv("GRADE_RETRY_BASE", "1.5"))
RETRY_STATUS = {429, 500, 502, 503, 504}

# 视觉图片压缩 (发送前)
VISION_MAX_EDGE = int(os.getenv("VISION_MAX_EDGE", "1400"))  # 最长边像素
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "80"))


class ProviderError(RuntimeError):
    """provider 调用失败 (网络/鉴权/解析)。"""


class RateLimitError(ProviderError):
    """限流或瞬时服务端错误 (可重试后仍失败)。"""


def _post_with_retry(url: str, headers: dict, payload: dict) -> requests.Response:
    """带指数退避的 POST。对 429/5xx/超时/连接错误重试, 尊重 Retry-After。"""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            resp = None
        if resp is not None:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRY_STATUS:
                raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            last_exc = ProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        # 计算退避时间
        if attempt == MAX_RETRIES - 1:
            break
        delay = RETRY_BASE * (2 ** attempt) + random.uniform(0, 0.6)
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        time.sleep(min(delay, 30))
    raise RateLimitError(f"重试 {MAX_RETRIES} 次后仍失败: {last_exc}")


@dataclass
class ModelChoice:
    """一次评分请求的模型选择。"""

    provider: str
    api_key: str
    model: str = ""
    base_url: str = ""
    use_vision: bool = True
    extra_headers: dict = field(default_factory=dict)

    @property
    def supports_vision(self) -> bool:
        return self.use_vision and config.model_supports_vision(self.provider, self.model)

    def resolved(self) -> "ModelChoice":
        """填充缺省的 base_url / model。"""
        preset = config.PROVIDERS.get(self.provider)
        if not preset:
            raise ProviderError(f"未知 provider: {self.provider}")
        base_url = (self.base_url or preset["base_url"]).rstrip("/")
        model = self.model.strip() or preset["default_model"]
        if not base_url:
            raise ProviderError("缺少 base_url，请在自定义模式下填写服务地址")
        if not model:
            raise ProviderError("缺少模型名称")
        return ModelChoice(
            provider=self.provider,
            api_key=self.api_key,
            model=model,
            base_url=base_url,
            use_vision=self.supports_vision,
            extra_headers=self.extra_headers,
        )


# ---------------------------------------------------------------------------
# 文本清洗与 JSON 解析
# ---------------------------------------------------------------------------
def clean_text_for_api(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[\ud800-\udfff]", "\ufffd", text)
    return text.encode("utf-8", errors="ignore").decode("utf-8")


def _parse_dimensions(data: dict) -> list[dict]:
    """解析可选的分维度评分: [{"name","score","full","comment"}]。"""
    raw = data.get("dimensions")
    if not isinstance(raw, list):
        return []
    dims = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue

        def _num(key):
            try:
                return round(float(item.get(key)), 1)
            except (TypeError, ValueError):
                return None

        dims.append(
            {
                "name": name,
                "score": _num("score"),
                "full": _num("full"),
                "comment": str(item.get("comment", "") or "").strip(),
            }
        )
    return dims


def parse_grade_json(content: str) -> dict:
    """从模型输出中鲁棒地解析出 {score, comment, reason, dimensions}。"""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ProviderError("模型未返回可解析的 JSON")
    data = json.loads(match.group())
    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    comment = str(data.get("comment", "") or "").strip()
    reason = str(data.get("reason", "") or "").strip()
    return {
        "score": score,
        "comment": comment,
        "reason": reason,
        "dimensions": _parse_dimensions(data),
        "raw": content,
    }


def _encode_image(path: str | Path) -> tuple[str, str]:
    """压缩并返回 (media_type, base64) 供视觉请求使用。

    发送前降采样到最长边 <= VISION_MAX_EDGE 并转 JPEG, 显著降低体积与 token 成本。
    压缩失败时回退为原始字节。
    """
    p = Path(path)
    try:
        from PIL import Image

        with Image.open(p) as im:
            im = im.convert("RGB")
            w, h = im.size
            longest = max(w, h)
            if longest > VISION_MAX_EDGE:
                scale = VISION_MAX_EDGE / longest
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
            return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        ext = p.suffix.lower().lstrip(".")
        media = "image/png" if ext in ("png", "") else f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        return media, base64.b64encode(p.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# OpenAI 兼容
# ---------------------------------------------------------------------------
def _complete_openai(choice: ModelChoice, system: str, user: str, images: list[str],
                     max_tokens: int | None = None) -> str:
    url = f"{choice.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {choice.api_key}",
        "Content-Type": "application/json",
        **choice.extra_headers,
    }

    if images and choice.use_vision:
        content: list[dict] = [{"type": "text", "text": user}]
        for img in images:
            media, b64 = _encode_image(img)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{b64}"},
                }
            )
        user_message = {"role": "user", "content": content}
    else:
        user_message = {"role": "user", "content": user}

    payload = {
        "model": choice.model,
        "messages": [
            {"role": "system", "content": system},
            user_message,
        ],
        "temperature": 0.3,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if choice.provider == "deepseek":
        # 新模型默认思考模式; 沿用原有非思考批改，避免推理占满既定输出预算。
        payload["thinking"] = {"type": "disabled"}
    # 纯文本时可安全启用 json_object；带图时部分服务不支持, 交由 prompt 约束。
    if not (images and choice.use_vision):
        payload["response_format"] = {"type": "json_object"}

    resp = _post_with_retry(url, headers, payload)
    result = resp.json()
    try:
        return result["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"响应结构异常: {exc}") from exc


# ---------------------------------------------------------------------------
# Anthropic messages
# ---------------------------------------------------------------------------
def _complete_anthropic(choice: ModelChoice, system: str, user: str, images: list[str],
                        max_tokens: int = 2048) -> str:
    url = f"{choice.base_url}/messages"
    headers = {
        "x-api-key": choice.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        **choice.extra_headers,
    }

    content: list[dict] = [{"type": "text", "text": user}]
    if images and choice.use_vision:
        for img in images:
            media, b64 = _encode_image(img)
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": b64},
                }
            )

    payload = {
        "model": choice.model,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "system": system + "\n\n只输出一个 JSON 对象，不要包含任何额外文字或代码块标记。",
        "messages": [{"role": "user", "content": content}],
    }
    resp = _post_with_retry(url, headers, payload)
    result = resp.json()
    try:
        blocks = result["content"]
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise ProviderError(f"响应结构异常: {exc}") from exc


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def complete(choice: ModelChoice, system: str, user: str, images: list[str] | None = None,
             max_tokens: int | None = None) -> str:
    """调用所选 provider, 返回模型原始文本(要求输出 JSON, 但不在此解析)。

    images: 需要视觉复核的页面图片绝对路径列表 (可为空)。
    max_tokens: 输出上限; 整卷拆题这类长输出需要调大, 默认沿用服务端/2048。
    """
    choice = choice.resolved()
    system = clean_text_for_api(system)
    user = clean_text_for_api(user)
    images = images or []

    kind = config.PROVIDERS[choice.provider]["kind"]
    if kind == "anthropic":
        return _complete_anthropic(choice, system, user, images, max_tokens or 2048)
    return _complete_openai(choice, system, user, images, max_tokens)


def grade(choice: ModelChoice, system: str, user: str, images: list[str] | None = None) -> dict:
    """调用所选 provider 完成一次评分, 返回解析后的 {score, comment, reason, dimensions, raw}。"""
    try:
        return parse_grade_json(complete(choice, system, user, images))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"模型返回的 JSON 无法解析: {exc}") from exc


def verify_key(choice: ModelChoice) -> tuple[bool, str]:
    """轻量校验 key/模型是否可用。返回 (ok, message)。"""
    try:
        result = grade(
            choice,
            system="你是一个测试助手。",
            user='请只返回 JSON: {"score": 1, "comment": "ok", "reason": "connectivity test"}',
        )
        return True, f"连接成功 (score={result['score']})"
    except Exception as exc:  # noqa: BLE001 - 汇报给用户
        return False, str(exc)


# ---------------------------------------------------------------------------
# Tool calling (OpenAI 兼容: DeepSeek / OpenAI / 自定义)
# ---------------------------------------------------------------------------
def image_content_blocks(paths: list[str]) -> list[dict]:
    """把图片路径列表转成 OpenAI 兼容的 image_url 内容块 (含压缩)。"""
    blocks = []
    for p in paths:
        try:
            media, b64 = _encode_image(p)
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}})
        except Exception:
            continue
    return blocks


def chat(
    choice: ModelChoice,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> dict:
    """OpenAI 兼容的一轮对话 (支持 function/tool calling)。

    返回原始 assistant message dict: {role, content, tool_calls?}。
    仅支持 openai 协议族 (DeepSeek/OpenAI/自定义); Anthropic 暂不在此路径。
    """
    choice = choice.resolved()
    if config.PROVIDERS[choice.provider]["kind"] != "openai":
        raise ProviderError("当前 agent 循环仅支持 OpenAI 兼容的 tool calling (DeepSeek/OpenAI/自定义)")
    url = f"{choice.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {choice.api_key}",
        "Content-Type": "application/json",
        **choice.extra_headers,
    }
    payload: dict = {
        "model": choice.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    if choice.provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    resp = _post_with_retry(url, headers, payload)
    result = resp.json()
    try:
        return result["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"响应结构异常: {exc}") from exc
