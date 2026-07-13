from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

try:
    import aiohttp
except ImportError:  # AstrBot includes aiohttp; this keeps pure unit tests importable.
    aiohttp = None


MAX_IMAGE_BYTES = 20 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 240


class DrawError(Exception):
    pass


@dataclass(frozen=True)
class ImageOutput:
    kind: str
    value: str


def extract_draw_prompt(message: str | None) -> str:
    text = (message or "").strip()
    match = re.match(r"^[／/]?draw(?:\s+|$)(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def build_draw_request(
    model: str,
    prompt: str,
    image_data_url: str | None = None,
) -> dict[str, Any]:
    if image_data_url:
        content: str | list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": image_data_url},
            },
        ]
    else:
        content = prompt

    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }


def build_optimizer_request(model: str, prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是绘图提示词优化器。保留用户的主体、动作、文字、构图和修改要求，"
                    "补充有助于图像模型理解的视觉细节。如果用户提到参考图，不要猜测图中"
                    "没有明说的内容。只输出优化后的提示词，不要解释。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }


def parse_optimizer_response(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DrawError("提示词优化接口没有返回可用文本。") from exc

    if isinstance(content, str):
        result = content.strip()
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        result = "\n".join(parts).strip()
    else:
        result = ""

    if not result:
        raise DrawError("提示词优化接口返回了空内容。")
    return result


def extract_image_output(payload: dict[str, Any]) -> ImageOutput:
    candidates: list[Any] = []

    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict):
            candidates.extend(message.get("images") or [])
            candidates.append(message.get("content"))

    candidates.extend(payload.get("data") or [])
    candidates.extend(payload.get("output") or [])

    for candidate in candidates:
        output = _find_image_output(candidate)
        if output:
            return output

    raise DrawError("绘图接口返回成功，但响应中没有找到图片。")


def image_bytes_to_data_url(data: bytes, source: str = "") -> str:
    if not data:
        raise DrawError("参考图片是空文件。")
    if len(data) > MAX_IMAGE_BYTES:
        raise DrawError("参考图片不能超过 20 MB。")
    mime = detect_image_mime(data, source)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def detect_image_mime(data: bytes, source: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    suffix = Path(urlparse(source).path).suffix.lower()
    suffix_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    if suffix in suffix_types:
        return suffix_types[suffix]
    raise DrawError("无法识别参考图片格式，请使用 PNG、JPEG、GIF 或 WebP。")


class Image2DrawClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        optimize_prompt: bool = False,
        optimizer_api_url: str = "",
        optimizer_api_key: str = "",
        optimizer_model: str = "",
    ) -> None:
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.optimize_prompt_enabled = optimize_prompt
        self.optimizer_api_url = optimizer_api_url.strip()
        self.optimizer_api_key = optimizer_api_key.strip()
        self.optimizer_model = optimizer_model.strip()

    async def draw(
        self,
        prompt: str,
        image_ref: str | None = None,
    ) -> tuple[ImageOutput, str]:
        self._validate_config()
        if aiohttp is None:
            raise DrawError("运行环境缺少 aiohttp，无法调用绘图接口。")

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            final_prompt = prompt
            if self.optimize_prompt_enabled:
                final_prompt = await self._optimize_prompt(session, prompt)

            image_data_url = None
            if image_ref:
                image_data_url = await self._load_image_data_url(session, image_ref)

            payload = build_draw_request(self.model, final_prompt, image_data_url)
            response = await self._post_json(
                session,
                self.api_url,
                self.api_key,
                payload,
                "绘图",
            )

        return extract_image_output(response), final_prompt

    def _validate_config(self) -> None:
        if not self.api_url:
            raise DrawError("请先在 WebUI 中填写绘图 API 地址。")
        if not self.api_key:
            raise DrawError("请先在 WebUI 中填写绘图 API Key。")
        if not self.model:
            raise DrawError("请先在 WebUI 中填写绘图模型。")
        if self.optimize_prompt_enabled and not all(
            (
                self.optimizer_api_url,
                self.optimizer_model,
            )
        ):
            raise DrawError("已开启提示词优化，请完整填写优化接口地址和模型。")

    async def _optimize_prompt(self, session: Any, prompt: str) -> str:
        payload = build_optimizer_request(self.optimizer_model, prompt)
        response = await self._post_json(
            session,
            self.optimizer_api_url,
            self.optimizer_api_key,
            payload,
            "提示词优化",
        )
        return parse_optimizer_response(response)

    async def _load_image_data_url(self, session: Any, image_ref: str) -> str:
        ref = str(image_ref or "").strip()
        if not ref:
            raise DrawError("没有找到可读取的参考图片。")

        data_match = re.fullmatch(
            r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)",
            ref,
            re.IGNORECASE | re.DOTALL,
        )
        if data_match:
            data = _decode_base64(data_match.group(2), "参考图片 base64 无效。")
            if len(data) > MAX_IMAGE_BYTES:
                raise DrawError("参考图片不能超过 20 MB。")
            return f"data:{data_match.group(1).lower()};base64,{base64.b64encode(data).decode('ascii')}"

        if ref.startswith("base64://"):
            data = _decode_base64(
                ref.removeprefix("base64://"), "参考图片 base64 无效。"
            )
            return image_bytes_to_data_url(data)

        if ref.startswith(("http://", "https://")):
            async with session.get(ref) as response:
                if response.status < 200 or response.status >= 300:
                    raise DrawError(f"下载参考图片失败：HTTP {response.status}。")
                if (
                    response.content_length
                    and response.content_length > MAX_IMAGE_BYTES
                ):
                    raise DrawError("参考图片不能超过 20 MB。")
                data = await response.content.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise DrawError("参考图片不能超过 20 MB。")
            return image_bytes_to_data_url(data, ref)

        path = _local_path_from_ref(ref)
        if not path.is_file():
            raise DrawError("参考图片不存在或已经失效。")
        data = await asyncio.to_thread(path.read_bytes)
        return image_bytes_to_data_url(data, str(path))

    async def _post_json(
        self,
        session: Any,
        url: str,
        api_key: str,
        payload: dict[str, Any],
        action: str,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    detail = _response_error_detail(text)
                    raise DrawError(
                        f"{action}接口返回 HTTP {response.status}"
                        + (f"：{detail}" if detail else "。")
                    )
        except asyncio.TimeoutError as exc:
            raise DrawError(f"{action}接口请求超时。") from exc
        except DrawError:
            raise
        except Exception as exc:
            if aiohttp is not None and isinstance(exc, aiohttp.ClientError):
                raise DrawError(f"{action}接口连接失败。") from exc
            raise

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            if "<html" in text.lower():
                raise DrawError(
                    f"{action}地址返回了网页，不是 API；地址应以 /v1/chat/completions 结尾。"
                ) from exc
            raise DrawError(f"{action}接口返回的不是有效 JSON。") from exc

        if not isinstance(parsed, dict):
            raise DrawError(f"{action}接口返回格式不正确。")
        return parsed


def _find_image_output(value: Any) -> ImageOutput | None:
    if isinstance(value, str):
        return _image_output_from_string(value)
    if isinstance(value, list):
        for item in value:
            output = _find_image_output(item)
            if output:
                return output
        return None
    if not isinstance(value, dict):
        return None

    for key in ("b64_json", "result"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            output = _image_output_from_string(candidate, allow_raw_base64=True)
            if output:
                return output

    for key in ("image_url", "url", "images", "content", "data"):
        if key in value:
            output = _find_image_output(value[key])
            if output:
                return output
    return None


def _image_output_from_string(
    value: str,
    *,
    allow_raw_base64: bool = False,
) -> ImageOutput | None:
    text = html.unescape(value.strip())
    data_match = re.search(
        r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=\r\n]+)",
        text,
        re.IGNORECASE,
    )
    if data_match:
        return ImageOutput("base64", re.sub(r"\s+", "", data_match.group(1)))

    markdown_match = re.search(
        r"!\[[^\]]*\]\((https?://[^\s<>\"]+)\)",
        text,
        re.IGNORECASE,
    )
    if markdown_match:
        url = markdown_match.group(1).rstrip(".,;，。；")
        return ImageOutput("url", url)

    if re.fullmatch(r"https?://[^\s<>\"]+", text, re.IGNORECASE):
        url = text.rstrip(".,;，。；")
        return ImageOutput("url", url)

    if allow_raw_base64 and _is_image_base64(text):
        return ImageOutput("base64", re.sub(r"\s+", "", text))
    return None


def _is_image_base64(value: str) -> bool:
    if len(value) < 128:
        return False
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError):
        return False
    return data.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
    ) or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def _decode_base64(value: str, error_message: str) -> bytes:
    try:
        return base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DrawError(error_message) from exc


def _local_path_from_ref(ref: str) -> Path:
    if not ref.startswith("file:"):
        return Path(ref).expanduser()

    parsed = urlparse(ref)
    path_text = url2pathname(unquote(parsed.path))
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    return Path(path_text)


def _response_error_detail(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _sanitize_error_detail(text)

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error.get("code")
    if error is None and isinstance(payload, dict):
        error = payload.get("message") or payload.get("detail")
    return _sanitize_error_detail(str(error or ""))


def _sanitize_error_detail(value: str) -> str:
    detail = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
    detail = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", detail)
    detail = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<hidden>", detail)
    return re.sub(r"\s+", " ", detail).strip()[:300]
