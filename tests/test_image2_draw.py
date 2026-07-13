from __future__ import annotations

import base64
import unittest

from image2_draw import (
    DrawError,
    Image2DrawClient,
    build_draw_request,
    build_optimizer_request,
    detect_image_mime,
    extract_draw_prompt,
    extract_image_output,
    image_bytes_to_data_url,
    parse_optimizer_response,
    _response_error_detail,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"test-image" * 20)


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
            client._validate_config()

    def test_optimizer_key_can_be_empty(self):
        client = Image2DrawClient(
            api_url="https://example.com/v1/chat/completions",
            api_key="test-key",
            model="image-model",
            optimize_prompt=True,
            optimizer_api_url="http://127.0.0.1:11434/v1/chat/completions",
            optimizer_model="local-model",
        )
        client._validate_config()


if __name__ == "__main__":
    unittest.main()
