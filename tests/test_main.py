from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch


class _Logger:
    def exception(self, _message):
        pass


class _Filter:
    @staticmethod
    def command(_name):
        return lambda function: function


class _Star:
    def __init__(self, context):
        self.context = context


class _Context:
    pass


class _Image:
    def __init__(self, *, path="", url="", file=""):
        self.path = path
        self.url = url
        self.file = file


class _Reply:
    def __init__(self, *, id):
        self.id = id


class _Result:
    def __init__(self, kind, value=""):
        self.kind = kind
        self.value = value
        self.chain = []

    def base64_image(self, value):
        return _Result("base64", value)


def _install_astrbot_stubs():
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.message_components": types.ModuleType(
            "astrbot.api.message_components"
        ),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.quoted_message_parser": types.ModuleType(
            "astrbot.core.utils.quoted_message_parser"
        ),
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = _Logger()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = _Filter()
    modules["astrbot.api.message_components"].Image = _Image
    modules["astrbot.api.message_components"].Reply = _Reply
    modules["astrbot.api.star"].Context = _Context
    modules["astrbot.api.star"].Star = _Star

    async def extract_quoted_message_images(_event):
        return []

    quoted = modules["astrbot.core.utils.quoted_message_parser"]
    quoted.extract_quoted_message_images = extract_quoted_message_images
    sys.modules.update(modules)


_install_astrbot_stubs()
import main  # noqa: E402
from image2_draw import DrawError, ImageOutput  # noqa: E402


class _Event:
    def __init__(self, message_str, messages=None, *, sender_id="10001", group_id=""):
        self.message_str = message_str
        self.messages = messages or []
        self.stopped = False
        self.sender_id = sender_id
        self.group_id = group_id
        self.message_obj = types.SimpleNamespace(
            message_id="draw-123",
            group_id=group_id,
            sender=types.SimpleNamespace(user_id=sender_id),
        )

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_messages(self):
        return self.messages

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return _Result("plain", text)

    def image_result(self, url):
        return _Result("url", url)

    def make_result(self):
        return _Result("builder")


class _SuccessfulClient:
    def __init__(self, **_kwargs):
        pass

    def validate_config(self, *_args):
        pass

    def validate_optimizer_config(self):
        pass

    async def draw(self, _prompt, _image_ref):
        return ImageOutput("url", "https://example.com/result.png"), "prompt"

    async def optimize(self, _prompt):
        return "优化后的提示词"


class _FailingClient(_SuccessfulClient):
    async def draw(self, _prompt, _image_ref):
        raise DrawError("上游失败")


async def _collect(generator):
    return [result async for result in generator]


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_dir = TemporaryDirectory()
        self.addCleanup(self.temporary_dir.cleanup)
        self.plugin = main.Image2DrawPlugin(_Context(), {})
        self.plugin.usage_store = main.DailyUsageStore(
            Path(self.temporary_dir.name) / "daily_usage.json"
        )

    async def test_usage_result_is_yielded_before_event_stops(self):
        event = _Event("draw")
        generator = self.plugin.draw(event)

        result = await anext(generator)
        self.assertEqual(result.kind, "plain")
        self.assertFalse(event.stopped)

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_image_result_is_yielded_before_event_stops(self):
        event = _Event("draw 画一只猫", group_id="99999")
        with patch.object(main, "Image2DrawClient", _SuccessfulClient):
            generator = self.plugin.draw(event)
            started = await anext(generator)
            self.assertEqual(started.kind, "plain")
            self.assertEqual(started.value, "开始绘画喵")
            self.assertEqual(started.chain, [])
            self.assertFalse(event.stopped)

            result = await anext(generator)
            self.assertEqual(result.kind, "url")
            self.assertIsInstance(result.chain[0], _Reply)
            self.assertEqual(result.chain[0].id, "draw-123")
            self.assertFalse(event.stopped)

            with self.assertRaises(StopAsyncIteration):
                await anext(generator)
        self.assertTrue(event.stopped)

    async def test_invalid_config_does_not_send_started_message(self):
        event = _Event("draw 画一只猫")
        generator = self.plugin.draw(event)

        result = await anext(generator)
        self.assertEqual(result.kind, "plain")
        self.assertTrue(result.value.startswith("绘图失败："))
        self.assertNotEqual(result.value, "开始绘画喵")
        self.assertIsInstance(result.chain[0], _Reply)
        self.assertEqual(result.chain[0].id, "draw-123")

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_group_whitelist_blocks_draw(self):
        plugin = main.Image2DrawPlugin(
            _Context(),
            {"whitelist_groups": ["12345"]},
        )
        results = await _collect(
            plugin.draw(_Event("draw 画一只猫", group_id="99999"))
        )
        self.assertEqual(len(results), 1)
        self.assertIn("本群未加入", results[0].value)
        self.assertNotEqual(results[0].value, "开始绘画喵")

    async def test_daily_limit_defaults_to_one_and_survives_reload(self):
        with TemporaryDirectory() as temporary_dir:
            usage_path = Path(temporary_dir) / "daily_usage.json"
            first_plugin = main.Image2DrawPlugin(_Context(), {})
            first_plugin.usage_store = main.DailyUsageStore(usage_path)
            with patch.object(main, "Image2DrawClient", _SuccessfulClient):
                first_results = await _collect(
                    first_plugin.draw(_Event("draw 第一次", sender_id="20001"))
                )

            second_plugin = main.Image2DrawPlugin(_Context(), {})
            second_plugin.usage_store = main.DailyUsageStore(usage_path)
            with patch.object(main, "Image2DrawClient", _SuccessfulClient):
                second_results = await _collect(
                    second_plugin.draw(_Event("draw 第二次", sender_id="20001"))
                )

        self.assertEqual(first_results[0].value, "开始绘画喵")
        self.assertEqual(len(second_results), 1)
        self.assertIn("次数已用完（1/1）", second_results[0].value)

    async def test_unlimited_user_bypasses_daily_limit(self):
        with TemporaryDirectory() as temporary_dir:
            plugin = main.Image2DrawPlugin(
                _Context(),
                {
                    "daily_draw_limit": 1,
                    "unlimited_users": ["30001"],
                },
            )
            plugin.usage_store = main.DailyUsageStore(
                Path(temporary_dir) / "daily_usage.json"
            )
            with patch.object(main, "Image2DrawClient", _SuccessfulClient):
                first_results = await _collect(
                    plugin.draw(_Event("draw 第一次", sender_id="30001"))
                )
                second_results = await _collect(
                    plugin.draw(_Event("draw 第二次", sender_id="30001"))
                )

        self.assertEqual(first_results[0].value, "开始绘画喵")
        self.assertEqual(second_results[0].value, "开始绘画喵")

    async def test_failed_draw_refunds_daily_usage(self):
        with TemporaryDirectory() as temporary_dir:
            plugin = main.Image2DrawPlugin(_Context(), {"daily_draw_limit": 1})
            plugin.usage_store = main.DailyUsageStore(
                Path(temporary_dir) / "daily_usage.json"
            )
            with patch.object(main, "Image2DrawClient", _FailingClient):
                failed_results = await _collect(
                    plugin.draw(_Event("draw 失败", sender_id="40001"))
                )
            with patch.object(main, "Image2DrawClient", _SuccessfulClient):
                retry_results = await _collect(
                    plugin.draw(_Event("draw 重试", sender_id="40001"))
                )

        self.assertIn("绘图失败", failed_results[-1].value)
        self.assertEqual(retry_results[0].value, "开始绘画喵")

    async def test_youhua_is_not_restricted_by_group_whitelist(self):
        plugin = main.Image2DrawPlugin(
            _Context(),
            {"whitelist_groups": ["12345"]},
        )
        with patch.object(main, "Image2DrawClient", _SuccessfulClient):
            results = await _collect(
                plugin.youhua(_Event("youhua 优化它", group_id="99999"))
            )
        self.assertEqual(results[0].value, "开始优化喵")
        self.assertIn("优化后的提示词", results[1].value)

    async def test_openai_images_reference_does_not_send_started_message(self):
        plugin = main.Image2DrawPlugin(
            _Context(),
            {
                "image_api_url": "https://example.com/v1/images/generations",
                "image_api_protocol": "openai_images",
                "image_api_key": "test-key",
                "image_model": "gpt-image-2",
            },
        )
        event = _Event("draw 改图", [_Image(url="https://example.com/source.png")])
        generator = plugin.draw(event)

        result = await anext(generator)
        self.assertEqual(result.kind, "plain")
        self.assertIn("不支持参考图", result.value)
        self.assertNotEqual(result.value, "开始绘画喵")

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_youhua_result_replies_to_the_command(self):
        event = _Event("youhua 画一只猫")
        with patch.object(main, "Image2DrawClient", _SuccessfulClient):
            generator = self.plugin.youhua(event)
            started = await anext(generator)
            self.assertEqual(started.kind, "plain")
            self.assertEqual(started.value, "开始优化喵")
            self.assertEqual(started.chain, [])

            result = await anext(generator)

        self.assertEqual(result.kind, "plain")
        self.assertEqual(result.value, "优化后的提示词：\n优化后的提示词")
        self.assertIsInstance(result.chain[0], _Reply)
        self.assertEqual(result.chain[0].id, "draw-123")

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_youhua_error_replies_to_the_command(self):
        event = _Event("youhua 画一只猫")
        generator = self.plugin.youhua(event)

        result = await anext(generator)
        self.assertEqual(result.kind, "plain")
        self.assertTrue(result.value.startswith("提示词优化失败："))
        self.assertIsInstance(result.chain[0], _Reply)
        self.assertEqual(result.chain[0].id, "draw-123")

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_youhua_usage_stops_after_the_result(self):
        event = _Event("youhua")
        generator = self.plugin.youhua(event)

        result = await anext(generator)
        self.assertEqual(result.value, "用法：/youhua <想优化的提示词>。")

        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertTrue(event.stopped)

    async def test_direct_image_falls_back_from_stale_path_to_url(self):
        event = _Event(
            "draw 改图",
            [_Image(path="missing.png", url="https://example.com/source.png")],
        )
        result = await main._find_reference_image(event)
        self.assertEqual(result, "https://example.com/source.png")

    async def test_quoted_image_is_used_when_message_has_no_image(self):
        event = _Event("draw 改图")
        with patch.object(
            main,
            "extract_quoted_message_images",
            AsyncMock(return_value=["https://example.com/quoted.png"]),
        ):
            result = await main._find_reference_image(event)
        self.assertEqual(result, "https://example.com/quoted.png")


class ConfigTests(unittest.TestCase):
    def test_invalid_integer_config_uses_the_field_default(self):
        self.assertEqual(
            main._config_int(
                {"optimizer_max_prompt_length": ""}, "optimizer_max_prompt_length", 50
            ),
            50,
        )


if __name__ == "__main__":
    unittest.main()
