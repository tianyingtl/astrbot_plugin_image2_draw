from __future__ import annotations

import base64
import types
import unittest
from unittest.mock import AsyncMock, patch

import image2_draw

from image2_draw import (
    DrawError,
    Image2DrawClient,
    build_draw_request,
    build_optimizer_request,
    detect_image_mime,
    extract_draw_prompt,
    extract_youhua_prompt,
    extract_image_output,
    image_bytes_to_data_url,
    parse_optimizer_response,
    _response_error_detail,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"test-image" * 20)


class _PostResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self):
        return self._text


class _PostSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, _url, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class PromptTests(unittest.TestCase):
    def test_extracts_full_prompt(self):
        self.assertEqual(
            extract_draw_prompt("/draw 一只 白色 小猫"),
            "一只 白色 小猫",
        )

    def test_accepts_command_without_slash(self):
        self.assertEqual(extract_draw_prompt("draw 改成红色"), "改成红色")

    def test_accepts_already_stripped_message(self):
        self.assertEqual(extract_draw_prompt("改成红色"), "改成红色")

    def test_extracts_youhua_prompt(self):
        self.assertEqual(
            extract_youhua_prompt("/youhua 画一只戴耳机的白猫"),
            "画一只戴耳机的白猫",
        )


class RequestTests(unittest.TestCase):
    def test_builds_text_request(self):
        payload = build_draw_request("gpt-image-2", "画一只猫")
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["messages"][0]["content"], "画一只猫")

    def test_builds_reference_image_request(self):
        payload = build_draw_request(
            "gpt-image-2",
            "改成红色",
            "data:image/png;base64,AAAA",
        )
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "改成红色"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(
            content[1]["image_url"]["url"],
            "data:image/png;base64,AAAA",
        )

    def test_optimizer_does_not_restrict_model_vendor(self):
        payload = build_optimizer_request("deepseek-chat", "画一只猫")
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertIn("不要猜测", payload["messages"][0]["content"])


class ResponseTests(unittest.TestCase):
    def test_extracts_markdown_image_url(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "完成：![image](https://example.com/result.png)"
                    }
                }
            ]
        }
        output = extract_image_output(payload)
        self.assertEqual(output.kind, "url")
        self.assertEqual(output.value, "https://example.com/result.png")

    def test_prefers_markdown_image_over_unrelated_url(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "文档 https://example.com/docs\n"
                            "![image](https://example.com/result.png)"
                        )
                    }
                }
            ]
        }
        output = extract_image_output(payload)
        self.assertEqual(output.value, "https://example.com/result.png")

    def test_extracts_message_images_url(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "images": [
                            {"image_url": {"url": "https://example.com/image.webp"}}
                        ],
                    }
                }
            ]
        }
        output = extract_image_output(payload)
        self.assertEqual(output.value, "https://example.com/image.webp")

    def test_extracts_base64_image(self):
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        payload = {"data": [{"b64_json": encoded}]}
        output = extract_image_output(payload)
        self.assertEqual(output.kind, "base64")
        self.assertEqual(output.value, encoded)

    def test_rejects_response_without_image(self):
        with self.assertRaises(DrawError):
            extract_image_output({"choices": [{"message": {"content": "完成"}}]})

    def test_parses_optimizer_text_array(self):
        payload = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "优化结果"}]}}
            ]
        }
        self.assertEqual(parse_optimizer_response(payload), "优化结果")

    def test_sanitizes_api_error_secrets(self):
        detail = _response_error_detail(
            '{"error":"Bearer secret-token sk-live123 '
            'https://api.example.com/path?token=secret"}'
        )
        self.assertNotIn("secret-token", detail)
        self.assertNotIn("sk-live123", detail)
        self.assertNotIn("token=secret", detail)

    def test_sanitizes_non_json_api_error(self):
        detail = _response_error_detail("failed with sk-live123")
        self.assertEqual(detail, "failed with sk-***")


class ImageTests(unittest.TestCase):
    def test_detects_png(self):
        self.assertEqual(detect_image_mime(PNG_BYTES), "image/png")

    def test_builds_data_url(self):
        result = image_bytes_to_data_url(PNG_BYTES)
        self.assertTrue(result.startswith("data:image/png;base64,"))


class ConfigTests(unittest.TestCase):
    def test_optimizer_requires_complete_config(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            optimize_prompt=True,
        )
        with self.assertRaises(DrawError):
            client.validate_config()

    def test_optimizer_key_can_be_empty(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            optimize_prompt=True,
            optimizer_api_url="http://127.0.0.1:11434/v1/chat/completions",
            optimizer_model="local-model",
        )
        client.validate_config()

    def test_timeout_must_be_in_range(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            request_timeout_seconds=0,
        )
        with self.assertRaises(DrawError):
            client.validate_config()

    def test_long_prompt_skips_optimizer(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            optimize_prompt=True,
            optimizer_max_prompt_length=50,
        )
        client.validate_config("猫" * 51)
        self.assertFalse(client._should_optimize_prompt("猫" * 51))
        self.assertTrue(client._should_optimize_prompt("猫" * 50))

    def test_prompt_length_ignores_whitespace_and_zero_disables_skip(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            optimize_prompt=True,
            optimizer_max_prompt_length=50,
            optimizer_api_url="https://example.com/v1/chat/completions",
            optimizer_model="text-model",
        )
        self.assertTrue(client._should_optimize_prompt("猫" * 50 + " \n\t"))
        client.optimizer_max_prompt_length = 0
        self.assertTrue(client._should_optimize_prompt("猫" * 51))

    def test_retry_count_must_be_in_range(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            draw_retry_count=4,
        )
        with self.assertRaises(DrawError):
            client.validate_config()

    def test_explicit_optimizer_does_not_require_drawing_config(self):
        client = Image2DrawClient(
            api_url="",
            api_key="",
            model="",
            optimizer_api_url="https://example.com/v1/chat/completions",
            optimizer_model="text-model",
        )
        client.validate_optimizer_config()

    def test_explicit_optimizer_requires_its_own_config(self):
        client = Image2DrawClient(api_url="", api_key="", model="")
        with self.assertRaises(DrawError):
            client.validate_optimizer_config()

    def test_optimizer_requires_a_complete_chat_endpoint(self):
        for api_url in (
            "https://api.example.com/",
            "https://api.example.com//v1/chat/completions",
        ):
            with self.subTest(api_url=api_url):
                client = Image2DrawClient(
                    api_url="",
                    api_key="",
                    model="",
                    optimizer_api_url=api_url,
                    optimizer_model="text-model",
                )
                with self.assertRaisesRegex(DrawError, "v1/chat/completions"):
                    client.validate_optimizer_config()

    def test_draw_requires_a_complete_chat_endpoint(self):
        client = Image2DrawClient(
            api_url="https://api.example.com//v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        with self.assertRaisesRegex(DrawError, "v1/chat/completions"):
            client.validate_config()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_502_for_draw(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        session = _PostSession(
            [
                _PostResponse(502, "channel circuit breaker open"),
                _PostResponse(200, '{"data": []}'),
            ]
        )

        with patch.object(image2_draw.asyncio, "sleep", AsyncMock()) as sleep:
            result = await client._post_json(
                session,
                "https://example.com/v1/chat/completions",
                "test-key",
                {},
                "绘图",
                retry_count=1,
            )

        self.assertEqual(result, {"data": []})
        self.assertEqual(session.calls, 2)
        sleep.assert_awaited_once_with(2)

    async def test_does_not_retry_other_status_codes(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        session = _PostSession([_PostResponse(500, "upstream error")])

        with self.assertRaisesRegex(DrawError, "HTTP 500"):
            await client._post_json(
                session,
                "https://example.com/v1/chat/completions",
                "test-key",
                {},
                "绘图",
                retry_count=3,
            )

        self.assertEqual(session.calls, 1)

    async def test_524_warns_about_possible_duplicate_generation(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        session = _PostSession([_PostResponse(524, "upstream returned 524")])

        with self.assertRaisesRegex(DrawError, "可能仍在生成"):
            await client._post_json(
                session,
                "https://example.com/v1/chat/completions",
                "test-key",
                {},
                "绘图",
            )

    async def test_retries_524_when_enabled(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        session = _PostSession(
            [
                _PostResponse(524, "upstream returned 524"),
                _PostResponse(200, '{"data": []}'),
            ]
        )

        with patch.object(image2_draw.asyncio, "sleep", AsyncMock()) as sleep:
            result = await client._post_json(
                session,
                "https://example.com/v1/chat/completions",
                "test-key",
                {},
                "绘图",
                retry_count=1,
            )

        self.assertEqual(result, {"data": []})
        self.assertEqual(session.calls, 2)
        sleep.assert_awaited_once_with(2)

    async def test_stops_after_the_configured_retry_count(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        session = _PostSession(
            [
                _PostResponse(502, "channel circuit breaker open"),
                _PostResponse(502, "channel circuit breaker open"),
            ]
        )

        with patch.object(image2_draw.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(DrawError, "已自动重试 1 次"):
                await client._post_json(
                    session,
                    "https://example.com/v1/chat/completions",
                    "test-key",
                    {},
                    "绘图",
                    retry_count=1,
                )

        self.assertEqual(session.calls, 2)


class OptimizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_optimizer_ignores_draw_settings_and_length_limit(self):
        client = Image2DrawClient(
            api_url="",
            api_key="",
            model="",
            optimize_prompt=False,
            optimizer_max_prompt_length=50,
            optimizer_api_url="https://example.com/v1/chat/completions",
            optimizer_model="text-model",
        )
        session = _PostSession(
            [_PostResponse(200, '{"choices": [{"message": {"content": "优化结果"}}]}')]
        )
        aiohttp_stub = types.SimpleNamespace(
            ClientTimeout=lambda **_kwargs: object(),
            ClientSession=lambda **_kwargs: session,
        )

        with patch.object(image2_draw, "aiohttp", aiohttp_stub):
            result = await client.optimize("猫" * 51)

        self.assertEqual(result, "优化结果")
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
