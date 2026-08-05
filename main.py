from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images

try:
    from .image2_draw import (
        DrawError,
        Image2DrawClient,
        extract_draw_prompt,
        extract_youhua_prompt,
    )
except ImportError:
    from image2_draw import (
        DrawError,
        Image2DrawClient,
        extract_draw_prompt,
        extract_youhua_prompt,
    )


class DailyUsageStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    async def reserve(self, user_id: str, limit: int) -> tuple[bool, int]:
        async with self._lock:
            data = self._load_today()
            users = data["users"]
            used = self._count(users.get(user_id, 0))
            if used >= limit:
                return False, used
            users[user_id] = used + 1
            self._save(data)
            return True, used + 1

    async def refund(self, user_id: str) -> None:
        async with self._lock:
            data = self._load_today()
            users = data["users"]
            used = self._count(users.get(user_id, 0))
            if used <= 0:
                return
            if used == 1:
                users.pop(user_id, None)
            else:
                users[user_id] = used - 1
            self._save(data)

    def _load_today(self) -> dict:
        today = date.today().isoformat()
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            data = {}

        if not isinstance(data, dict) or data.get("date") != today:
            return {"date": today, "users": {}}
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
        return {"date": today, "users": users}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temporary_path.replace(self.path)

    @staticmethod
    def _count(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0


class Image2DrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.usage_store = DailyUsageStore(
            Path.home() / ".astrbot_plugin_image2_draw" / "daily_usage.json"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        command = _parse_image_command(getattr(event, "message_str", ""))
        if command == "draw":
            async for result in self.draw(event):
                yield result
        elif command == "youhua":
            async for result in self.youhua(event):
                yield result

    async def draw(self, event: AstrMessageEvent):
        if not self._draw_group_allowed(event):
            yield _reply_to_command_message(
                event,
                event.plain_result("本群未加入 Image2 绘图插件白名单。"),
            )
            event.stop_event()
            return

        prompt = extract_draw_prompt(getattr(event, "message_str", ""))
        if not prompt:
            yield event.plain_result(
                "用法：/draw <提示词>。可以在同一条消息附图，或回复一张图片后发送指令。"
            )
            event.stop_event()
            return

        client = _create_client(self.config)

        reserved_sender_id = ""
        try:
            image_ref = await _find_reference_image(event)
            client.validate_config(prompt, bool(image_ref))

            sender_id = _get_sender_id(event)
            unlimited_users = _normalize_id_list(
                self.config.get("unlimited_users", [])
            )
            if sender_id not in unlimited_users:
                if not sender_id:
                    raise DrawError("无法识别当前用户，不能检查每日绘图次数。")
                daily_limit = max(
                    1, _config_int(self.config, "daily_draw_limit", 1)
                )
                allowed, used = await self.usage_store.reserve(sender_id, daily_limit)
                if not allowed:
                    yield _reply_to_command_message(
                        event,
                        event.plain_result(
                            f"你今天的绘图次数已用完（{used}/{daily_limit}）。"
                        ),
                    )
                    event.stop_event()
                    return
                reserved_sender_id = sender_id

            yield event.plain_result("开始绘画喵")
            output, _ = await client.draw(prompt, image_ref)
        except DrawError as exc:
            if reserved_sender_id:
                await self.usage_store.refund(reserved_sender_id)
            yield _reply_to_command_message(
                event, event.plain_result(f"绘图失败：{exc}")
            )
            event.stop_event()
            return
        except Exception:
            if reserved_sender_id:
                await self.usage_store.refund(reserved_sender_id)
            logger.exception("Image2 绘图插件处理请求失败")
            yield _reply_to_command_message(
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
        yield _reply_to_command_message(event, result)
        event.stop_event()

    async def youhua(self, event: AstrMessageEvent):
        prompt = extract_youhua_prompt(getattr(event, "message_str", ""))
        if not prompt:
            yield event.plain_result("用法：/youhua <想优化的提示词>。")
            event.stop_event()
            return

        client = _create_client(self.config)
        try:
            client.validate_optimizer_config()
            yield event.plain_result("开始优化喵")
            optimized_prompt = await client.optimize(prompt)
        except DrawError as exc:
            yield _reply_to_command_message(
                event, event.plain_result(f"提示词优化失败：{exc}")
            )
            event.stop_event()
            return
        except Exception:
            logger.exception("Image2 绘图插件优化提示词时发生异常")
            yield _reply_to_command_message(
                event,
                event.plain_result(
                    "提示词优化失败：插件处理请求时发生异常，请查看 AstrBot 日志。"
                ),
            )
            event.stop_event()
            return

        yield _reply_to_command_message(
            event, event.plain_result(f"优化后的提示词：\n{optimized_prompt}")
        )
        event.stop_event()

    def _draw_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = _get_group_id(event)
        if not group_id:
            return True
        whitelist = _normalize_id_list(self.config.get("whitelist_groups", []))
        return not whitelist or group_id in whitelist


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


def _reply_to_command_message(event: AstrMessageEvent, result):
    message_obj = getattr(event, "message_obj", None)
    message_id = getattr(message_obj, "message_id", None)
    if message_id:
        result.chain.insert(0, Reply(id=message_id))
    return result


def _create_client(config: AstrBotConfig) -> Image2DrawClient:
    return Image2DrawClient(
        api_url=_config_text(config, "image_api_url"),
        edit_api_url=_config_text(config, "image_edit_api_url"),
        api_key=_config_text(config, "image_api_key"),
        model=_config_text(config, "image_model"),
        draw_protocol=_config_text(config, "image_api_protocol"),
        image_resolution=_config_text(config, "image_resolution"),
        request_timeout_seconds=_config_int(config, "request_timeout_seconds", 240),
        draw_retry_count=_config_int(config, "draw_retry_count", 0),
        optimize_prompt=_config_bool(config, "optimize_prompt"),
        optimizer_max_prompt_length=_config_int(
            config, "optimizer_max_prompt_length", 50
        ),
        optimizer_api_url=_config_text(config, "optimizer_api_url"),
        optimizer_api_key=_config_text(config, "optimizer_api_key"),
        optimizer_model=_config_text(config, "optimizer_model"),
    )


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


def _get_sender_id(event: AstrMessageEvent) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        sender_id = getter()
        if sender_id:
            return str(sender_id)

    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    for attr in ("user_id", "sender_id", "id"):
        value = getattr(sender, attr, None)
        if value:
            return str(value)
    return ""


def _get_group_id(event: AstrMessageEvent) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        group_id = getter()
        if group_id:
            return str(group_id)

    message_obj = getattr(event, "message_obj", None)
    group_id = getattr(message_obj, "group_id", "")
    return str(group_id) if group_id else ""


def _normalize_id_list(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        text = value
        for separator in (",", "，", ";", "；"):
            text = text.replace(separator, " ")
        items = text.split()
    else:
        items = value
    return {str(item).strip() for item in items if str(item).strip()}


def _parse_image_command(message: str) -> str | None:
    text = (message or "").strip()
    text = re.sub(r"^(?:\[CQ:at,[^\]]+\]|@\S+)\s*", "", text)
    match = re.match(r"^[／/](draw|youhua)(?:\s|$)", text, re.IGNORECASE)
    return match.group(1).lower() if match else None
