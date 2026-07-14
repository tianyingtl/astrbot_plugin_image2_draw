from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images

try:
    from .image2_draw import DrawError, Image2DrawClient, extract_draw_prompt
except ImportError:
    from image2_draw import DrawError, Image2DrawClient, extract_draw_prompt


class Image2DrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.command("draw")
    async def draw(self, event: AstrMessageEvent):
        prompt = extract_draw_prompt(getattr(event, "message_str", ""))
        if not prompt:
            yield event.plain_result(
                "用法：/draw <提示词>。可以在同一条消息附图，或回复一张图片后发送指令。"
            )
            event.stop_event()
            return

        client = Image2DrawClient(
            api_url=_config_text(self.config, "image_api_url"),
            api_key=_config_text(self.config, "image_api_key"),
            model=_config_text(self.config, "image_model"),
            request_timeout_seconds=_config_int(
                self.config, "request_timeout_seconds", 240
            ),
            draw_retry_count=_config_int(self.config, "draw_retry_count", 0),
            optimize_prompt=_config_bool(self.config, "optimize_prompt"),
            optimizer_max_prompt_length=_config_int(
                self.config, "optimizer_max_prompt_length", 50
            ),
            optimizer_api_url=_config_text(self.config, "optimizer_api_url"),
            optimizer_api_key=_config_text(self.config, "optimizer_api_key"),
            optimizer_model=_config_text(self.config, "optimizer_model"),
        )

        try:
            image_ref = await _find_reference_image(event)
            client.validate_config(prompt)
            yield event.plain_result("开始绘画喵")
            output, _ = await client.draw(prompt, image_ref)
        except DrawError as exc:
            yield _reply_to_draw_message(event, event.plain_result(f"绘图失败：{exc}"))
            event.stop_event()
            return
        except Exception:
            logger.exception("Image2 绘图插件处理请求失败")
            yield _reply_to_draw_message(
                event,
                event.plain_result(
                    "绘图失败：插件处理请求时发生异常，请查看 AstrBot 日志。"
                ),
            )
            event.stop_event()
            return

        if output.kind == "base64":
            result = event.make_result().base64_image(output.value)
        else:
            result = event.image_result(output.value)
        yield _reply_to_draw_message(event, result)
        event.stop_event()


async def _find_reference_image(event: AstrMessageEvent) -> str | None:
    for component in event.get_messages():
        if not isinstance(component, Image):
            continue
        for attr in ("path", "url", "file"):
            value = getattr(component, attr, None)
            if attr == "path" and value and not Path(str(value)).is_file():
                continue
            if value:
                return str(value)

    quoted_images = await extract_quoted_message_images(event)
    if quoted_images:
        return str(quoted_images[0])
    return None


def _reply_to_draw_message(event: AstrMessageEvent, result):
    message_obj = getattr(event, "message_obj", None)
    message_id = getattr(message_obj, "message_id", None)
    if message_id:
        result.chain.insert(0, Reply(id=message_id))
    return result


def _config_text(config: AstrBotConfig, key: str) -> str:
    return str(config.get(key, "") or "").strip()


def _config_bool(config: AstrBotConfig, key: str) -> bool:
    value = config.get(key, False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}


def _config_int(config: AstrBotConfig, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default
