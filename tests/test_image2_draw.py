from __future__ import annotations

import base64
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import image2_draw

from image2_draw import (
    DrawError,
    Image2DrawClient,
    ImageOutput,
    build_draw_request,
    build_openai_images_request,
    build_optimizer_request,
    detect_image_mime,
    extract_draw_prompt,
    extract_youhua_prompt,
    extract_image_output,
    image_bytes_to_data_url,
    parse_draw_options,
    resolve_draw_options,
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
        self.requests = []

    def post(self, url, **kwargs):
        self.calls += 1
        self.requests.append((url, kwargs))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _GetContent:
    def __init__(self, data):
        self.data = data

    async def read(self, _limit):
        return self.data


class _GetResponse:
    def __init__(self, data, status=200):
        self.status = status
        self.content_length = len(data)
        self.content = _GetContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _GetSession:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self.response


class _FormData:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


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

    def test_parses_draw_options_from_suffix(self):
        prompt, options = parse_draw_options(
            '海边城市 --ar 916 --size 2K --quality high --negative "文字 水印"'
        )
        self.assertEqual(prompt, "海边城市")
        self.assertEqual(options.aspect_ratio, "916")
        self.assertEqual(options.size, "2K")
        self.assertEqual(options.quality, "high")
        self.assertEqual(options.negative_prompt, "文字 水印")

    def test_resolves_portrait_ratio_to_exact_dimensions(self):
        _prompt, options = parse_draw_options("猫 --ar 916 --size 2K")
        resolved = resolve_draw_options(options)
        self.assertEqual(resolved.aspect_ratio, "9:16")
        self.assertEqual(resolved.size, "1152x2048")

    def test_accepts_custom_dimensions(self):
        _prompt, options = parse_draw_options("猫 --size 1536x2048")
        resolved = resolve_draw_options(options)
        self.assertEqual(resolved.aspect_ratio, "3:4")
        self.assertEqual(resolved.size, "1536x2048")

    def test_rejects_conflicting_ratio_and_custom_dimensions(self):
        _prompt, options = parse_draw_options("猫 --ar 916 --size 1536x2048")
        with self.assertRaisesRegex(DrawError, "冲突"):
            resolve_draw_options(options)

    def test_all_ratio_aliases_keep_exact_ratio_and_aligned_dimensions(self):
        for alias, ratio in image2_draw.ASPECT_RATIO_ALIASES.items():
            for resolution in ("1K", "2K", "4K"):
                with self.subTest(alias=alias, resolution=resolution):
                    prompt, options = parse_draw_options(f"城市 {alias} {resolution}")
                    resolved = resolve_draw_options(options)
                    width, height = map(int, resolved.size.split("x"))
                    rw, rh = map(int, ratio.split(":"))
                    self.assertEqual(prompt, "城市")
                    self.assertEqual(width * rh, height * rw)
                    self.assertEqual(width % 16, 0)
                    self.assertEqual(height % 16, 0)
                    self.assertLessEqual(max(width, height), int(resolution[:-1]) * 1024)

    def test_shorthands_quotes_and_aliases(self):
        for suffix in ("--916 --size 2k", "--ratio=9:16 --resolution=2K", "916 2K", "9:16 2K", "--9:16 --size 2K"):
            with self.subTest(suffix=suffix):
                _, options = parse_draw_options(f"海边 {suffix}")
                self.assertEqual(resolve_draw_options(options).size, "1152x2048")
        prompt, options = parse_draw_options("海边 --style '柔和 水彩' --negative \"字 水印\"")
        self.assertEqual(prompt, "海边")
        self.assertEqual(options.style, "柔和 水彩")
        self.assertEqual(options.negative_prompt, "字 水印")

    def test_plain_prompt_and_literal_escape_are_preserved(self):
        for prompt in ("画一张 don't panic 海报", '写上 "--ar 916" 这段文字', "第一行\n第二行"):
            with self.subTest(prompt=prompt):
                self.assertEqual(parse_draw_options(prompt), (prompt, image2_draw.DrawOptions()))
        self.assertEqual(
            parse_draw_options("-- 画号码 11 --style 这也是原文"),
            ("画号码 11 --style 这也是原文", image2_draw.DrawOptions()),
        )

    def test_invalid_or_duplicate_flags_report_before_any_request(self):
        for suffix in (
            "--ar", "--ar --size 2K", "--style ''", "--style \"未闭合",
            "--unknown 1", "--ar 916 --ratio 916", "--size 2K stray",
            "--quality=", "--style '   '", "--no-optimize=yes",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(DrawError):
                parse_draw_options("城市 " + suffix)

    def test_invalid_values_and_custom_size_limits(self):
        for suffix in (
            "--ar 5:4", "--size 0x1024", "--size 255x1024", "--size 9000x1024",
            "--size 8K", "--quality best",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(DrawError):
                _, options = parse_draw_options("城市 " + suffix)
                resolve_draw_options(options)

    def test_ratio_uses_configured_resolution_and_explicit_size_overrides_it(self):
        _, options = parse_draw_options("城市 916")
        self.assertEqual(resolve_draw_options(options, "4K").size, "2304x4096")
        _, options = parse_draw_options("城市 --size 1080x1920 --ar 916")
        self.assertEqual(resolve_draw_options(options, "4K").size, "1080x1920")
        _, options = parse_draw_options("城市")
        self.assertEqual(resolve_draw_options(options, "1080x1920").size, "1080x1920")
        _, options = parse_draw_options("城市 43")
        self.assertEqual(resolve_draw_options(options, "1080x1920").size, "1920x1440")

    def test_style_and_negative_do_not_inject_square_dimensions(self):
        _, options = parse_draw_options("改成夜景 --style 水彩 --negative 水印")
        prompt = image2_draw.apply_draw_options(
            "改成夜景", resolve_draw_options(options), include_size=False
        )
        self.assertIn("水彩", prompt)
        self.assertIn("水印", prompt)
        self.assertNotIn("尺寸", prompt)


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

    def test_builds_openai_images_request(self):
        payload = build_openai_images_request("gpt-image-2", "画一只猫", "4K")
        self.assertEqual(
            payload,
            {
                "model": "gpt-image-2",
                "prompt": "画一只猫",
                "size": "4096x4096",
                "response_format": "b64_json",
            },
        )

    def test_maps_resolution_labels_to_api_dimensions(self):
        cases = {
            "1K": "1024x1024",
            "2K": "2048x2048",
            "4K": "4096x4096",
        }
        for resolution, expected_size in cases.items():
            with self.subTest(resolution=resolution):
                payload = build_openai_images_request(
                    "gpt-image-2",
                    "测试",
                    resolution,
                )
                self.assertEqual(payload["size"], expected_size)

    def test_builds_custom_size_and_quality(self):
        payload = build_openai_images_request(
            "gpt-image-2",
            "测试",
            "4K",
            image_size="1152x2048",
            quality="high",
        )
        self.assertEqual(payload["size"], "1152x2048")
        self.assertEqual(payload["quality"], "high")

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

    def test_extracts_image_from_nested_result(self):
        payload = {
            "result": {
                "images": [{"url": "https://example.com/nested-result.png"}]
            }
        }
        output = extract_image_output(payload)
        self.assertEqual(output.kind, "url")
        self.assertEqual(output.value, "https://example.com/nested-result.png")

    def test_extracts_top_level_image(self):
        payload = {"image": {"url": "https://example.com/top-level.webp"}}
        output = extract_image_output(payload)
        self.assertEqual(output.kind, "url")
        self.assertEqual(output.value, "https://example.com/top-level.webp")

    def test_extracts_image_from_object_data(self):
        payload = {
            "data": {
                "result": {"url": "https://example.com/object-data.png"}
            }
        }
        output = extract_image_output(payload)
        self.assertEqual(output.kind, "url")
        self.assertEqual(output.value, "https://example.com/object-data.png")

    def test_missing_image_reports_safe_response_shape(self):
        payload = {
            "status": "processing",
            "task_id": "private-task-id",
            "prompt": "private prompt",
            "api_key": "sk-private-key",
        }
        with self.assertRaises(DrawError) as context:
            extract_image_output(payload)

        message = str(context.exception)
        self.assertIn("响应字段", message)
        self.assertIn("status=processing", message)
        self.assertNotIn("private-task-id", message)
        self.assertNotIn("private prompt", message)
        self.assertNotIn("sk-private-key", message)

    def test_http_200_error_payload_reports_sanitized_reason(self):
        payload = {
            "error": {
                "message": (
                    "upstream rejected sk-private-key "
                    "https://api.example.com/path?token=private"
                )
            }
        }
        with self.assertRaises(DrawError) as context:
            extract_image_output(payload)

        message = str(context.exception)
        self.assertIn("绘图接口返回错误：upstream rejected", message)
        self.assertNotIn("sk-private-key", message)
        self.assertNotIn("token=private", message)

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

    def test_rejects_non_image_even_when_url_has_image_suffix(self):
        with self.assertRaisesRegex(DrawError, "无法识别参考图片内容"):
            detect_image_mime(b"<html>not an image</html>", "https://example.com/error.jpg")

    def test_builds_data_url(self):
        result = image_bytes_to_data_url(PNG_BYTES)
        self.assertTrue(result.startswith("data:image/png;base64,"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_result_is_downloaded_before_sending(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            api_key="test-key",
            model="gpt-image-2",
        )
        session = _GetSession(_GetResponse(PNG_BYTES))

        output = await client._materialize_output(
            session,
            ImageOutput("url", "https://cdn.example.com/result.png"),
        )

        self.assertEqual(output.kind, "base64")
        self.assertEqual(base64.b64decode(output.value), PNG_BYTES)
        self.assertEqual(session.urls, ["https://cdn.example.com/result.png"])

    async def test_image_edit_uses_multipart_form(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            edit_api_url="https://gateway.example.com/v1/images/edits",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
            image_resolution="4K",
        )
        session = _PostSession([_PostResponse(200, '{"data": []}')])
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        aiohttp_stub = types.SimpleNamespace(FormData=_FormData)

        with patch.object(image2_draw, "aiohttp", aiohttp_stub):
            await client._post_image_edit(
                session,
                "https://gateway.example.com/v1/images/edits",
                f"data:image/png;base64,{encoded}",
                "改成红色",
            )

        url, request = session.requests[0]
        fields = {name: (value, kwargs) for name, value, kwargs in request["data"].fields}
        self.assertEqual(url, "https://gateway.example.com/v1/images/edits")
        self.assertEqual(fields["model"][0], "gpt-image-2")
        self.assertEqual(fields["prompt"][0], "改成红色")
        self.assertEqual(fields["n"][0], "1")
        self.assertNotIn("size", fields)
        self.assertEqual(fields["response_format"][0], "b64_json")
        self.assertEqual(fields["image"][0], PNG_BYTES)
        self.assertEqual(fields["image"][1]["content_type"], "image/png")

    async def test_image_edit_rejects_gif_before_request(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            edit_api_url="https://gateway.example.com/v1/images/edits",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
        )
        session = _PostSession([])
        encoded = base64.b64encode(b"GIF89a" + (b"0" * 100)).decode("ascii")

        with self.assertRaisesRegex(DrawError, "PNG、JPEG 或 WebP"):
            await client._post_image_edit(
                session,
                "https://gateway.example.com/v1/images/edits",
                f"data:image/gif;base64,{encoded}",
                "改成红色",
            )

        self.assertEqual(session.calls, 0)

    async def test_openai_images_reference_uses_edit_endpoint(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            edit_api_url="https://gateway.example.com/v1/images/edits",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
            image_resolution="4K",
        )
        session = _PostSession([])
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        aiohttp_stub = types.SimpleNamespace(
            ClientTimeout=lambda **_kwargs: object(),
            ClientSession=lambda **_kwargs: session,
        )

        with (
            patch.object(image2_draw, "aiohttp", aiohttp_stub),
            patch.object(
                client,
                "_load_image_data_url",
                AsyncMock(return_value=f"data:image/png;base64,{encoded}"),
            ),
            patch.object(
                client,
                "_post_image_edit",
                AsyncMock(return_value={"data": [{"b64_json": encoded}]}),
            ) as edit_request,
        ):
            output, final_prompt = await client.draw("改成红色", "source.png")

        self.assertEqual(output.kind, "base64")
        self.assertEqual(final_prompt, "改成红色")
        edit_request.assert_awaited_once_with(
            session,
            "https://gateway.example.com/v1/images/edits",
            f"data:image/png;base64,{encoded}",
            "改成红色",
            image_size="4096x4096",
        )


class OptionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def call_draw(self, prompt, *, protocol="openai_images", image_ref=None, **kwargs):
        client = Image2DrawClient(
            api_url="https://example.com/generate",
            edit_api_url="https://example.com/edit",
            api_key="test-key",
            model="image-model",
            draw_protocol=protocol,
            **kwargs,
        )
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        session = _PostSession([_PostResponse(200, json.dumps({"data": [{"b64_json": encoded}]}))])
        stub = types.SimpleNamespace(
            ClientTimeout=lambda **kw: object(), ClientSession=lambda **kw: session,
            FormData=_FormData,
        )
        with (
            patch.object(image2_draw, "aiohttp", stub),
            patch.object(client, "_load_image_data_url", AsyncMock(return_value=f"data:image/png;base64,{encoded}")),
            patch.object(client, "_optimize_prompt", AsyncMock(return_value="优化完成")) as optimizer,
        ):
            output, final_prompt = await client.draw(prompt, image_ref)
        self.assertEqual(output.kind, "base64")
        return session.requests[0], final_prompt, optimizer

    async def test_generation_uses_size_quality_and_post_optimization_constraints(self):
        request, prompt, optimizer = await self.call_draw(
            '海边 --ar 916 --size 2K --quality high --style 水彩 --negative "文字 水印"',
            optimize_prompt=True, optimizer_api_url="https://example.com/chat", optimizer_model="text",
        )
        payload = request[1]["json"]
        self.assertEqual(payload["size"], "1152x2048")
        self.assertEqual(payload["quality"], "high")
        self.assertIn("9:16", payload["prompt"])
        self.assertIn("文字 水印", prompt)
        self.assertIn("水彩", prompt)
        self.assertNotIn("--ar", prompt)
        optimizer.assert_awaited_once()
        self.assertEqual(optimizer.call_args.args[1], "海边")

    async def test_edit_has_explicit_size_and_quality_in_form(self):
        request, _, _ = await self.call_draw(
            "换背景 --ar 916 --size 2K --quality low", image_ref="ref.png"
        )
        self.assertEqual(request[0], "https://example.com/edit")
        fields = {name: value for name, value, _ in request[1]["data"].fields}
        self.assertEqual(fields["size"], "1152x2048")
        self.assertEqual(fields["quality"], "low")
        self.assertEqual(fields["image"], PNG_BYTES)

    async def test_edit_style_alone_uses_default_size(self):
        request, prompt, _ = await self.call_draw("换背景 --style 水彩", image_ref="ref.png")
        fields = {name: value for name, value, _ in request[1]["data"].fields}
        self.assertEqual(fields["size"], "4096x4096")
        self.assertNotIn("quality", fields)
        self.assertNotIn("4096", prompt)
        self.assertIn("水彩", prompt)

    async def test_edit_default_size_matches_generation(self):
        for resolution, size in (("1K", "1024x1024"), ("2K", "2048x2048"), ("4K", "4096x4096"), ("1080x1920", "1080x1920")):
            with self.subTest(resolution=resolution):
                generation, _, _ = await self.call_draw("海边", image_resolution=resolution)
                edit, _, _ = await self.call_draw("换背景", image_ref="ref.png", image_resolution=resolution)
                fields = {name: value for name, value, _ in edit[1]["data"].fields}
                self.assertEqual(fields["size"], size)
                self.assertEqual(fields["size"], generation[1]["json"]["size"])
                self.assertNotIn("quality", fields)

    async def test_edit_colon_ratio_uses_default_resolution(self):
        for suffix, expected in (("9:16", "2304x4096"), ("9:16 2K", "1152x2048")):
            with self.subTest(suffix=suffix):
                request, _, _ = await self.call_draw(f"换背景 {suffix}", image_ref="ref.png")
                fields = {name: value for name, value, _ in request[1]["data"].fields}
                self.assertEqual(fields["size"], expected)

    async def test_chat_keeps_standard_request_and_adds_text_constraints(self):
        request, prompt, _ = await self.call_draw(
            "海边 --ar 916 --size 2K --quality high", protocol="openai_chat"
        )
        self.assertEqual(set(request[1]["json"]), {"model", "messages"})
        self.assertEqual(request[1]["json"]["messages"][0]["content"], prompt)
        self.assertIn("1152x2048", prompt)
        self.assertIn("high", prompt)

    async def test_no_optimize_works_without_optimizer_credentials(self):
        request, prompt, optimizer = await self.call_draw(
            "海边 --no-optimize", optimize_prompt=True
        )
        optimizer.assert_not_awaited()
        self.assertEqual(prompt, "海边")
        self.assertEqual(request[1]["json"]["prompt"], "海边")

    async def test_configured_custom_size_is_sent(self):
        request, _, _ = await self.call_draw("海边", image_resolution="1080x1920")
        self.assertEqual(request[1]["json"]["size"], "1080x1920")

    async def test_preview_cannot_accidentally_make_api_request(self):
        client = Image2DrawClient(api_url="", api_key="", model="")
        with self.assertRaisesRegex(DrawError, "预览"):
            await client.draw("海边 --preview")


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

    def test_api_urls_are_used_without_rewrite(self):
        client = Image2DrawClient(
            api_url="https://api.example.com/",
            api_key="test-key",
            model="image-model",
            optimizer_api_url="https://optimizer.example.com/custom/chat",
            optimizer_model="text-model",
        )
        self.assertEqual(client.api_url, "https://api.example.com/")
        self.assertEqual(
            client.optimizer_api_url,
            "https://optimizer.example.com/custom/chat",
        )
        client.validate_config()
        client.validate_optimizer_config()

    def test_openai_images_protocol_accepts_images_generation_endpoint(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            edit_api_url="https://gateway.example.com/v1/images/edits",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
            image_resolution="2K",
        )
        self.assertEqual(
            client.api_url,
            "https://gateway.example.com/v1/images/generations",
        )
        self.assertEqual(
            client.edit_api_url,
            "https://gateway.example.com/v1/images/edits",
        )
        self.assertEqual(client.image_resolution, "2K")
        client.validate_config()

    def test_openai_images_reference_requires_edit_endpoint(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
        )
        with self.assertRaisesRegex(DrawError, "编辑 API 地址"):
            client.validate_config("改图", True)

    def test_openai_images_reference_accepts_edit_endpoint(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            edit_api_url="https://gateway.example.com/v1/images/edits",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
        )
        client.validate_config("改图", True)

    def test_rejects_unknown_image_resolution(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/v1/images/generations",
            api_key="test-key",
            model="gpt-image-2",
            draw_protocol="openai_images",
            image_resolution="8K",
        )
        with self.assertRaisesRegex(DrawError, "清晰度"):
            client.validate_config()

    def test_rejects_unknown_draw_protocol(self):
        client = Image2DrawClient(
            api_url="https://gateway.example.com/draw",
            api_key="test-key",
            model="image-model",
            draw_protocol="unknown",
        )
        with self.assertRaisesRegex(DrawError, "绘图接口协议"):
            client.validate_config()

    def test_rejects_non_http_api_url(self):
        client = Image2DrawClient(
            api_url="api.example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
        )
        with self.assertRaisesRegex(DrawError, "地址格式不正确"):
            client.validate_config()

    def test_parameters_do_not_hide_a_short_prompt_from_optimizer_validation(self):
        client = Image2DrawClient(
            api_url="https://example.com/generate", api_key="test-key", model="image",
            optimize_prompt=True,
        )
        with self.assertRaisesRegex(DrawError, "优化接口"):
            client.validate_config("海边 --negative \"" + "水印" * 60 + "\"")


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
