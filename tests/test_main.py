from __future__ import annotations

import sys
import types
import unittest
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
from image2_draw import ImageOutput  # noqa: E402


class _Event:
    def __init__(self, message_str, messages=None):
        self.message_str = message_str
        self.messages = messages or []
        self.stopped = False
        self.message_obj = types.SimpleNamespace(message_id="draw-123")

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


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.Image2DrawPlugin(_Context(), {})

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
        event = _Event("draw 画一只猫")
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
