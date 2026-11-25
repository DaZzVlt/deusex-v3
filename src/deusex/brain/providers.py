from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import base64
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
import re
import time
from typing import Any, Literal

import httpx

from deusex.core.config import SecretsManager
from deusex.core.contracts import ToolSpec

logger = logging.getLogger(__name__)


class ModelTier(StrEnum):
    """Nivel de capacidad solicitado al cerebro IA."""

    FAST = "fast"
    DEEP = "deep"


class ProviderError(RuntimeError):
    """Error normalizado de proveedor IA con semántica de reintento y enfriamiento."""

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    provider_id: str
    display_name: str
    supports_vision: bool = False
    supports_audio: bool = False
    supports_tools: bool = True
    supports_web_search: bool = False
    context_window: int = 0
    is_deprecated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "supports_vision": self.supports_vision,
            "supports_audio": self.supports_audio,
            "supports_tools": self.supports_tools,
            "supports_web_search": self.supports_web_search,
            "context_window": self.context_window,
            "is_deprecated": self.is_deprecated,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelInfo:
        return cls(
            id=str(raw["id"]),
            provider_id=str(raw["provider_id"]),
            display_name=str(raw.get("display_name") or raw["id"]),
            supports_vision=bool(raw.get("supports_vision", False)),
            supports_audio=bool(raw.get("supports_audio", False)),
            supports_tools=bool(raw.get("supports_tools", True)),
            supports_web_search=bool(raw.get("supports_web_search", False)),
            context_window=int(raw.get("context_window", 0)),
            is_deprecated=bool(raw.get("is_deprecated", False)),
        )


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    mime_type: str
    data: bytes
    filename: str = ""


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    attachments: tuple[MediaAttachment, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]
    model: str
    system_prompt: str = ""
    temperature: float = 0.4
    max_output_tokens: int = 2048
    tools: tuple[ToolSpec, ...] = ()
    enable_web_search: bool = False
    timeout_sec: float | None = None
    tier: ModelTier = ModelTier.FAST


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    provider_id: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    sources: tuple[dict[str, str], ...] = ()
    latency_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider_id: str
    display_name: str
    env_key: str
    base_url: str
    supports_audio: bool
    supports_vision: bool
    docs_url: str


PROVIDER_METADATA: dict[str, ProviderMetadata] = {
    "gemini": ProviderMetadata(
        provider_id="gemini",
        display_name="Google Gemini",
        env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com",
        supports_audio=True,
        supports_vision=True,
        docs_url="https://aistudio.google.com/apikey",
    ),
    "openai": ProviderMetadata(
        provider_id="openai",
        display_name="OpenAI (ChatGPT)",
        env_key="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        supports_audio=False,
        supports_vision=True,
        docs_url="https://platform.openai.com/api-keys",
    ),
    "xai": ProviderMetadata(
        provider_id="xai",
        display_name="xAI (Grok)",
        env_key="XAI_API_KEY",
        base_url="https://api.x.ai/v1",
        supports_audio=False,
        supports_vision=True,
        docs_url="https://console.x.ai/",
    ),
    "anthropic": ProviderMetadata(
        provider_id="anthropic",
        display_name="Anthropic (Claude)",
        env_key="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com/v1",
        supports_audio=False,
        supports_vision=True,
        docs_url="https://console.anthropic.com/settings/keys",
    ),
    "groq": ProviderMetadata(
        provider_id="groq",
        display_name="Groq",
        env_key="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        supports_audio=False,
        supports_vision=True,
        docs_url="https://console.groq.com/keys",
    ),
    "deepseek": ProviderMetadata(
        provider_id="deepseek",
        display_name="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
        supports_audio=False,
        supports_vision=False,
        docs_url="https://platform.deepseek.com/api_keys",
    ),
    "openrouter": ProviderMetadata(
        provider_id="openrouter",
        display_name="OpenRouter",
        env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        supports_audio=False,
        supports_vision=True,
        docs_url="https://openrouter.ai/keys",
    ),
}

_CLIPBOARD_KEY_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("gemini", "GEMINI_API_KEY", re.compile(r"^AIza[0-9A-Za-z\-_]{30,50}$")),
    ("anthropic", "ANTHROPIC_API_KEY", re.compile(r"^sk-ant-[0-9A-Za-z\-_]{24,}$")),
    ("openrouter", "OPENROUTER_API_KEY", re.compile(r"^sk-or-v1-[0-9A-Za-z]{32,}$")),
    ("xai", "XAI_API_KEY", re.compile(r"^xai-[0-9A-Za-z\-_]{24,}$")),
    ("groq", "GROQ_API_KEY", re.compile(r"^gsk_[0-9A-Za-z]{24,}$")),
    ("openai", "OPENAI_API_KEY", re.compile(r"^sk-(?:proj-)?[0-9A-Za-z\-_]{24,}$")),
)

_NON_CHAT_MODEL_TOKENS: tuple[str, ...] = (
    "embedding",
    "embed",
    "aqa",
    "imagen",
    "veo",
    "whisper",
    "tts",
    "transcribe",
    "dall-e",
    "moderation",
    "babbage",
    "davinci",
    "rerank",
    "realtime",
    "audio-preview",
    "robotics",
    "computer-use",
    "guard",
    "prompt-guard",
)


_GROQ_VISION_TOKENS: tuple[str, ...] = (
    "vision",
    "llama-4-scout",
    "llama-4-maverick",
    "llava",
)
_OPENROUTER_VISION_TOKENS: tuple[str, ...] = (
    "gemini",
    "claude",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-5",
    "vision",
    "-vl",
    "vl-",
    "pixtral",
    "llama-4",
    "multimodal",
    "grok-2-vision",
    "grok-4",
)
_OPENROUTER_TEXT_ONLY_TOKENS: tuple[str, ...] = (
    "llama-3.1",
    "llama-3.3",
    "llama-3-",
    "llama3",
    "mixtral",
    "gemma-2",
    "gemma2",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-r1",
    "deepseek-v3",
    "qwen3",
    "qwen-2.5",
    "qwen2.5",
    "qwen-qwq",
    "qwq-",
)


def is_usable_chat_model(model_id: str) -> bool:
    """Descarta embeddings, generadores de imagen/video y modelos no aptos para chat."""
    lowered = model_id.lower()
    return not any(token in lowered for token in _NON_CHAT_MODEL_TOKENS)


def is_vision_model_id(provider_id: str, model_id: str) -> bool:
    """Determina si el modelo tiene soporte visual real para imágenes adjuntas."""
    pid = provider_id.strip().lower()
    mid = model_id.strip().lower()
    if not mid:
        return False
    if pid == "deepseek":
        return False
    if pid == "gemini":
        return True
    if pid == "anthropic":
        return "claude" in mid
    if pid == "openai":
        return not (
            mid.startswith("gpt-3.5")
            or mid.startswith("o1-mini")
            or mid.startswith("o3-mini")
        )
    if pid == "xai":
        return "vision" in mid or "grok-4" in mid or "grok-2-image" in mid
    if pid == "groq":
        return any(tok in mid for tok in _GROQ_VISION_TOKENS)
    if pid == "openrouter":
        if any(tok in mid for tok in _OPENROUTER_VISION_TOKENS):
            return True
        return not any(tok in mid for tok in _OPENROUTER_TEXT_ONLY_TOKENS)
    meta = PROVIDER_METADATA.get(pid)
    return bool(meta.supports_vision) if meta is not None else False


def detect_api_key_from_text(text: str) -> tuple[str, str, str] | None:
    """Smart Clipboard Catcher: detecta (provider_id, env_key, clean_token) desde texto copiado."""
    candidate = text.strip()
    if not candidate or "\n" in candidate or " " in candidate:
        return None
    for provider_id, env_key, pattern in _CLIPBOARD_KEY_PATTERNS:
        if pattern.match(candidate):
            return provider_id, env_key, candidate
    return None


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    match = re.search(r"\b(400|401|403|404|429|500|502|503|504)\b", str(exc))
    if match:
        return int(match.group(1))
    return None


class LLMProvider(ABC):
    """Contrato abstracto para todos los proveedores IA de DeusEx v3."""

    def __init__(self, metadata: ProviderMetadata, secrets: SecretsManager) -> None:
        self._meta = metadata
        self._secrets = secrets

    @property
    def metadata(self) -> ProviderMetadata:
        return self._meta

    @property
    def provider_id(self) -> str:
        return self._meta.provider_id

    @property
    def display_name(self) -> str:
        return self._meta.display_name

    @property
    def env_key(self) -> str:
        return self._meta.env_key

    def get_api_key(self) -> str | None:
        key = self._secrets.get(self.env_key)
        return key.strip() if key and key.strip() else None

    def is_configured(self) -> bool:
        return self.get_api_key() is not None

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """Consulta en vivo los modelos utilizables disponibles para la clave configurada."""

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Ejecuta una inferencia de chat/multimodal contra el proveedor."""

    async def close(self) -> None:  # noqa: B027
        """Libera clientes HTTP persistentes."""


class GeminiProvider(LLMProvider):
    """Motor principal sobre el SDK oficial `google-genai` (sin Google ADK)."""

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(PROVIDER_METADATA["gemini"], secrets)

    def _require_api_key(self) -> str:
        api_key = self.get_api_key()
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY no está configurada.",
                provider_id=self.provider_id,
                status_code=401,
                retryable=False,
            )
        return api_key

    def _list_models_sync(self, api_key: str) -> list[ModelInfo]:
        from google import genai

        client = genai.Client(api_key=api_key)
        models: list[ModelInfo] = []
        for raw_model in client.models.list():
            raw_name = str(getattr(raw_model, "name", "") or "")
            if not raw_name:
                continue
            model_id = raw_name.removeprefix("models/")
            if not model_id.lower().startswith("gemini"):
                continue
            if not is_usable_chat_model(model_id):
                continue

            actions = getattr(raw_model, "supported_actions", None)
            if isinstance(actions, (list, tuple, set)) and actions:
                action_names = {str(a) for a in actions}
                if "generateContent" not in action_names:
                    continue

            display_name = str(getattr(raw_model, "display_name", "") or model_id)
            input_limit = int(getattr(raw_model, "input_token_limit", 0) or 0)
            lowered = model_id.lower()
            is_deprecated = (
                "1.0" in lowered
                or "1.5" in lowered
                or "2.0" in lowered
                or "2.5" in lowered
                or "legacy" in lowered
            )

            models.append(
                ModelInfo(
                    id=model_id,
                    provider_id=self.provider_id,
                    display_name=display_name,
                    supports_vision=True,
                    supports_audio=True,
                    supports_tools=True,
                    supports_web_search=True,
                    context_window=input_limit,
                    is_deprecated=is_deprecated,
                )
            )
        return models

    async def list_models(self) -> list[ModelInfo]:
        api_key = self._require_api_key()
        try:
            return await asyncio.to_thread(self._list_models_sync, api_key)
        except ProviderError:
            raise
        except Exception as exc:
            status = _extract_status_code(exc)
            retryable = status in (429, 500, 502, 503, 504)
            raise ProviderError(
                f"Error listando modelos de Gemini: {exc}",
                provider_id=self.provider_id,
                status_code=status,
                retryable=retryable,
            ) from exc

    def _generate_sync(self, api_key: str, request: LLMRequest) -> LLMResponse:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents: list[Any] = []
        for msg in request.messages:
            if msg.role == "system":
                continue
            role = "model" if msg.role == "assistant" else "user"
            parts: list[Any] = []
            if msg.content:
                parts.append(types.Part.from_text(text=msg.content))
            for att in msg.attachments:
                parts.append(types.Part.from_bytes(data=att.data, mime_type=att.mime_type))
            if parts:
                contents.append(types.Content(role=role, parts=parts))

        config_kwargs: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
        }
        if request.system_prompt:
            config_kwargs["system_instruction"] = request.system_prompt

        is_fast_or_flash = (
            request.tier == ModelTier.FAST
            or "flash" in request.model.lower()
        )
        if is_fast_or_flash and hasattr(types, "ThinkingConfig"):
            with contextlib.suppress(Exception):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        gemini_tools: list[Any] = []
        if request.tools:
            declarations = [
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.parameters_schema,
                )
                for tool in request.tools
            ]
            gemini_tools.append(types.Tool(function_declarations=declarations))
        elif request.enable_web_search:
            gemini_tools.append(types.Tool(google_search=types.GoogleSearch()))

        if gemini_tools:
            config_kwargs["tools"] = gemini_tools

        config = types.GenerateContentConfig(**config_kwargs)
        started = time.perf_counter()
        try:
            response = client.models.generate_content(
                model=request.model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            if "thinking_config" in config_kwargs and (
                "thinking" in str(exc).lower() or _extract_status_code(exc) == 400
            ):
                logger.debug("Modelo rechazó thinking_budget=0 (%s); reintentando sin thinking_config", exc)
                config_kwargs.pop("thinking_config", None)
                config = types.GenerateContentConfig(**config_kwargs)
                response = client.models.generate_content(
                    model=request.model,
                    contents=contents,
                    config=config,
                )
            else:
                raise
        latency_ms = (time.perf_counter() - started) * 1000.0

        text = ""
        try:
            text = str(response.text or "")
        except Exception:
            text = ""

        tool_calls: list[ToolCall] = []
        raw_calls = getattr(response, "function_calls", None) or []
        for idx, fc in enumerate(raw_calls):
            fc_name = str(getattr(fc, "name", "") or "")
            fc_args = getattr(fc, "args", None)
            if fc_name:
                tool_calls.append(
                    ToolCall(
                        id=str(getattr(fc, "id", None) or f"gemini_tc_{idx}"),
                        name=fc_name,
                        arguments=dict(fc_args) if isinstance(fc_args, dict) else {},
                    )
                )

        sources: list[dict[str, str]] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            grounding = getattr(candidates[0], "grounding_metadata", None)
            chunks = getattr(grounding, "grounding_chunks", None) or []
            seen_urls: set[str] = set()
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                uri = str(getattr(web, "uri", "") or "")
                title = str(getattr(web, "title", "") or uri)
                if uri and uri not in seen_urls:
                    seen_urls.add(uri)
                    sources.append({"title": title, "url": uri})

        usage = getattr(response, "usage_metadata", None)
        tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)

        return LLMResponse(
            text=text.strip(),
            provider_id=self.provider_id,
            model=request.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tool_calls=tuple(tool_calls),
            sources=tuple(sources),
            latency_ms=latency_ms,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        api_key = self._require_api_key()
        default_timeout = 25.0 if request.tier == ModelTier.DEEP else 18.0
        timeout_sec = request.timeout_sec if request.timeout_sec is not None else default_timeout
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, api_key, request),
                timeout=timeout_sec,
            )
        except TimeoutError as exc:
            logger.warning("Timeout (%.1fs) llamando a Gemini", timeout_sec)
            raise ProviderError(
                f"Timeout ({timeout_sec:.1f}s) con Gemini: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            status = _extract_status_code(exc)
            retryable = status in (429, 500, 502, 503, 504)
            raise ProviderError(
                f"Error generando respuesta con Gemini: {exc}",
                provider_id=self.provider_id,
                status_code=status,
                retryable=retryable,
            ) from exc


class OpenAICompatProvider(LLMProvider):
    """Adaptador liviano sobre `httpx` para OpenAI, xAI/Grok, Groq, DeepSeek y OpenRouter."""

    def __init__(self, provider_id: str, secrets: SecretsManager) -> None:
        if provider_id not in PROVIDER_METADATA or provider_id in ("gemini", "anthropic"):
            raise ValueError(f"Proveedor OpenAI-compatible no válido: {provider_id!r}")
        super().__init__(PROVIDER_METADATA[provider_id], secrets)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._meta.base_url,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        api_key = self.get_api_key()
        if not api_key:
            raise ProviderError(
                f"{self.env_key} no está configurada.",
                provider_id=self.provider_id,
                status_code=401,
                retryable=False,
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self.provider_id == "openrouter":
            headers["HTTP-Referer"] = "https://dazzvlt.github.io/deusex/"
            headers["X-Title"] = "DeusEx v3"
        return headers

    async def list_models(self) -> list[ModelInfo]:
        client = self._get_client()
        try:
            resp = await client.get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Error de red consultando modelos en {self.display_name}: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.display_name} devolvió HTTP {resp.status_code}: {resp.text[:240]}",
                provider_id=self.provider_id,
                status_code=resp.status_code,
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )

        payload = resp.json()
        raw_items = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[ModelInfo] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip()
            if not model_id or not is_usable_chat_model(model_id):
                continue
            arch = item.get("architecture")
            supports_vision = is_vision_model_id(self.provider_id, model_id)
            if isinstance(arch, dict):
                input_mods = arch.get("input_modalities")
                modality = str(arch.get("modality") or "").lower()
                if isinstance(input_mods, list):
                    supports_vision = "image" in [str(x).lower() for x in input_mods]
                elif modality:
                    supports_vision = "image" in modality
            models.append(
                ModelInfo(
                    id=model_id,
                    provider_id=self.provider_id,
                    display_name=str(item.get("name") or model_id),
                    supports_vision=supports_vision,
                    supports_audio=False,
                    supports_tools=True,
                    context_window=int(item.get("context_length") or 0),
                )
            )
        return models

    async def generate(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        messages_payload: list[dict[str, Any]] = []
        if request.system_prompt:
            messages_payload.append({"role": "system", "content": request.system_prompt})

        for msg in request.messages:
            if msg.role == "system":
                continue
            if msg.attachments and self._meta.supports_vision:
                parts: list[dict[str, Any]] = []
                if msg.content:
                    parts.append({"type": "text", "text": msg.content})
                for att in msg.attachments:
                    if att.mime_type.startswith("image/"):
                        b64 = base64.b64encode(att.data).decode("ascii")
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{att.mime_type};base64,{b64}"},
                            }
                        )
                messages_payload.append({"role": msg.role, "content": parts})
            else:
                messages_payload.append({"role": msg.role, "content": msg.content})

        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages_payload,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema,
                    },
                }
                for tool in request.tools
            ]

        timeout_sec = request.timeout_sec if request.timeout_sec is not None else 12.0
        started = time.perf_counter()
        try:
            resp = await client.post(
                "/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(connect=5.0, read=timeout_sec, write=10.0, pool=5.0),
            )
        except httpx.TimeoutException as exc:
            logger.warning("Timeout (%.1fs) llamando a %s: %s", timeout_sec, self.display_name, exc)
            raise ProviderError(
                f"Timeout ({timeout_sec:.1f}s) con {self.display_name}: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Error de red con {self.display_name}: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.display_name} devolvió HTTP {resp.status_code}: {resp.text[:240]}",
                provider_id=self.provider_id,
                status_code=resp.status_code,
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )

        data = resp.json()
        choices = data.get("choices", []) if isinstance(data, dict) else []
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        text = str(message.get("content") or "").strip()

        tool_calls: list[ToolCall] = []
        for idx, tc in enumerate(message.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            fn_name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
            parsed_args: dict[str, Any] = {}
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    loaded = json.loads(raw_args)
                    if isinstance(loaded, dict):
                        parsed_args = loaded
                except ValueError:
                    parsed_args = {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            if fn_name:
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id") or f"{self.provider_id}_tc_{idx}"),
                        name=fn_name,
                        arguments=parsed_args,
                    )
                )

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider_id=self.provider_id,
            model=str(data.get("model") or request.model),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            tool_calls=tuple(tool_calls),
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


class AnthropicProvider(LLMProvider):
    """Adaptador liviano sobre `httpx` para Anthropic (Claude)."""

    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(PROVIDER_METADATA["anthropic"], secrets)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._meta.base_url,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        api_key = self.get_api_key()
        if not api_key:
            raise ProviderError(
                "ANTHROPIC_API_KEY no está configurada.",
                provider_id=self.provider_id,
                status_code=401,
                retryable=False,
            )
        return {
            "x-api-key": api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    async def list_models(self) -> list[ModelInfo]:
        client = self._get_client()
        try:
            resp = await client.get("/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Error de red consultando modelos de Claude: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic devolvió HTTP {resp.status_code}: {resp.text[:240]}",
                provider_id=self.provider_id,
                status_code=resp.status_code,
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )

        payload = resp.json()
        raw_items = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[ModelInfo] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id", "")).strip()
            if not model_id or not is_usable_chat_model(model_id):
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    provider_id=self.provider_id,
                    display_name=str(item.get("display_name") or model_id),
                    supports_vision=True,
                    supports_audio=False,
                    supports_tools=True,
                )
            )
        return models

    async def generate(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        messages_payload: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == "system":
                continue
            role = "assistant" if msg.role == "assistant" else "user"
            blocks: list[dict[str, Any]] = []
            for att in msg.attachments:
                if att.mime_type.startswith("image/"):
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": att.mime_type,
                                "data": base64.b64encode(att.data).decode("ascii"),
                            },
                        }
                    )
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            if blocks:
                messages_payload.append({"role": role, "content": blocks})

        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": messages_payload,
        }
        if request.system_prompt:
            body["system"] = request.system_prompt
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters_schema,
                }
                for tool in request.tools
            ]

        timeout_sec = request.timeout_sec if request.timeout_sec is not None else 12.0
        started = time.perf_counter()
        try:
            resp = await client.post(
                "/messages",
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(connect=5.0, read=timeout_sec, write=10.0, pool=5.0),
            )
        except httpx.TimeoutException as exc:
            logger.warning("Timeout (%.1fs) llamando a Anthropic: %s", timeout_sec, exc)
            raise ProviderError(
                f"Timeout ({timeout_sec:.1f}s) con Anthropic: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Error de red con Anthropic: {exc}",
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic devolvió HTTP {resp.status_code}: {resp.text[:240]}",
                provider_id=self.provider_id,
                status_code=resp.status_code,
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )

        data = resp.json()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for idx, block in enumerate(data.get("content") or []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                name = str(block.get("name") or "")
                raw_input = block.get("input")
                if name:
                    tool_calls.append(
                        ToolCall(
                            id=str(block.get("id") or f"claude_tc_{idx}"),
                            name=name,
                            arguments=dict(raw_input) if isinstance(raw_input, dict) else {},
                        )
                    )

        usage = data.get("usage") or {}
        return LLMResponse(
            text="\n".join(p for p in text_parts if p).strip(),
            provider_id=self.provider_id,
            model=str(data.get("model") or request.model),
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            tool_calls=tuple(tool_calls),
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def create_default_providers(secrets: SecretsManager) -> dict[str, LLMProvider]:
    """Instancia el conjunto oficial de proveedores soportados en DeusEx v3 (sin Ollama/ADK)."""
    return {
        "gemini": GeminiProvider(secrets),
        "openai": OpenAICompatProvider("openai", secrets),
        "xai": OpenAICompatProvider("xai", secrets),
        "anthropic": AnthropicProvider(secrets),
        "groq": OpenAICompatProvider("groq", secrets),
        "deepseek": OpenAICompatProvider("deepseek", secrets),
        "openrouter": OpenAICompatProvider("openrouter", secrets),
    }
