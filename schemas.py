"""模型表、别名与请求体定义。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None


