import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_gif_generation import gateway, image_bytes, response


class CxGifGenerationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = self.root / "local_config.json"
        self.write_config()
        self.stack.enter_context(patch.object(gateway, "CONFIG_PATH", self.config))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.post = self.stack.enter_context(patch.object(
            gateway.requests, "post", side_effect=AssertionError("Unexpected POST")
        ))
        self.get = self.stack.enter_context(patch.object(
            gateway.requests, "get", side_effect=AssertionError("Unexpected GET")
        ))
        self.stack.enter_context(patch.object(gateway.time, "sleep"))
        self.gif = image_bytes("GIF", animated=True)
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(image_bytes("PNG"))
        self.out = self.root / "animation.gif"

    def write_config(self, **overrides):
        data = {
            "base_url": "https://gateway.example/v1", "api_key": "offline-placeholder",
            "model": "gpt-image-2", "endpoint_mode": "images",
        }
        data.update(overrides)
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def args(self, *extra):
        return gateway.build_parser().parse_args([
            "generate", "--prompt", "Bounce the blue ball and keep the background",
            "--model", "gpt-image-2.6-flare", "--out", str(self.out), *extra,
        ])

    def success(self):
        return response({"data": [{"b64_json": base64.b64encode(self.gif).decode("ascii")}]})

    def prepare_success(self):
        self.post.side_effect = None
        self.post.return_value = self.success()

    def test_cx_text_gif_sends_documented_defaults_as_json(self):
        self.prepare_success()
        gateway.command_generate(self.args("--gif-profile", "cx"))
        self.assertEqual(self.post.call_args.args[0], "https://gateway.example/v1/images/generations")
        self.assertEqual(self.post.call_args.kwargs["json"], {
            "model": "gpt-image-2.6-flare", "prompt": "Bounce the blue ball and keep the background",
            "n": 1, "quality": "low", "output_format": "gif", "size": "auto",
            "stream": False, "response_format": "url", "frame_count": 6, "frame_duration_ms": 200,
        })
        self.assertEqual(self.out.read_bytes(), self.gif)
        self.get.assert_not_called()

    def test_reference_gif_uploads_image_array_with_lowercase_false(self):
        observed = {}

        def post(url, **kwargs):
            observed.update(url=url, **kwargs)
            observed["reference_bytes"] = kwargs["files"]["image[]"][1].read()
            return self.success()

        self.post.side_effect = post
        gateway.command_generate(self.args(
            "--gif-profile", "cx", "--image", str(self.reference),
            "--frame-count", "4", "--frame-duration-ms", "240",
        ))
        self.assertEqual(observed["url"], "https://gateway.example/v1/images/edits")
        self.assertEqual(set(observed["files"]), {"image[]"})
        self.assertEqual(observed["files"]["image[]"][2], "image/png")
        self.assertEqual(observed["reference_bytes"], self.reference.read_bytes())
        self.assertEqual(observed["data"], {
            "model": "gpt-image-2.6-flare", "prompt": "Bounce the blue ball and keep the background",
            "n": "1", "quality": "low", "output_format": "gif", "size": "auto",
            "stream": "false", "response_format": "url", "frame_count": "4", "frame_duration_ms": "240",
        })
        self.assertNotIn("Content-Type", observed["headers"])
        self.assertEqual(self.out.read_bytes(), self.gif)

    def test_cx_profile_rejects_more_than_16_frames_before_network(self):
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--gif-profile", "cx", "--frame-count", "17"))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_cx_frame_bounds_are_accepted(self):
        self.prepare_success()
        for count in [2, 16]:
            with self.subTest(count=count):
                gateway.command_generate(self.args("--gif-profile", "cx", "--frame-count", str(count)))
                self.assertEqual(self.post.call_args.kwargs["json"]["frame_count"], count)

    def test_documented_gateway_host_automatically_selects_cx(self):
        self.write_config(base_url="https://api.www-cx-api.cn.mt/v1")
        self.prepare_success()
        gateway.command_generate(self.args())
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["frame_count"], 6)
        self.assertEqual(payload["response_format"], "url")

    def test_hostname_suffix_lookalike_does_not_select_cx(self):
        self.write_config(base_url="https://api.www-cx-api.cn.mt.example/v1")
        self.prepare_success()
        gateway.command_generate(self.args())
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["frame_count"], 9)
        self.assertNotIn("response_format", payload)

    def test_saved_profile_and_cli_override_take_precedence(self):
        self.write_config(base_url="https://api.www-cx-api.cn.mt/v1", gif_profile="generic")
        self.prepare_success()
        gateway.command_generate(self.args())
        self.assertEqual(self.post.call_args.kwargs["json"]["frame_count"], 9)
        gateway.command_generate(self.args("--gif-profile", "cx"))
        self.assertEqual(self.post.call_args.kwargs["json"]["frame_count"], 6)

    def test_cx_reference_url_is_rejected_before_network(self):
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--gif-profile", "cx", "--image", "https://cdn.example/ref.png"))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_missing_local_reference_is_rejected_before_network(self):
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--gif-profile", "cx", "--image", str(self.root / "missing.png")))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_url_response_download_keeps_gateway_token_off_cdn(self):
        self.post.side_effect = None
        self.post.return_value = response({"data": [{"url": "https://cdn.example/result.gif"}]})
        self.get.side_effect = None
        self.get.return_value = response(content=self.gif)
        gateway.command_generate(self.args("--gif-profile", "cx", "--image", str(self.reference)))
        self.assertEqual(self.out.read_bytes(), self.gif)
        self.assertNotIn("Authorization", self.get.call_args.kwargs.get("headers", {}))

    def test_explicit_base64_response_format_overrides_cx_default(self):
        self.prepare_success()
        gateway.command_generate(self.args("--gif-profile", "cx", "--response-format", "b64_json"))
        self.assertEqual(self.post.call_args.kwargs["json"]["response_format"], "b64_json")
        self.assertEqual(self.out.read_bytes(), self.gif)

    def test_response_format_is_supported_for_static_images(self):
        self.out = self.root / "result.png"
        raw = image_bytes("PNG")
        self.post.side_effect = None
        self.post.return_value = response({"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]})
        gateway.command_generate(self.args("--response-format", "url"))
        self.assertEqual(self.post.call_args.kwargs["json"]["response_format"], "url")
        self.assertEqual(self.out.read_bytes(), raw)

    def test_response_format_is_rejected_on_responses_endpoint(self):
        self.write_config(endpoint_mode="responses")
        self.out = self.root / "result.png"
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--response-format", "url"))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_cx_still_rejects_streaming_compression_and_resolution_tier(self):
        for flags in [["--stream"], ["--compression", "20"], ["--image-size", "2K"]]:
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                gateway.command_generate(self.args("--gif-profile", "cx", *flags))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_static_local_edits_keep_singular_image_field(self):
        self.out = self.root / "result.png"
        captured = {}

        def post(url, **kwargs):
            captured.update(kwargs)
            return response({"data": [{"b64_json": base64.b64encode(image_bytes("PNG")).decode("ascii")}]})

        self.post.side_effect = post
        gateway.command_generate(self.args("--image", str(self.reference)))
        self.assertEqual(set(captured["files"]), {"image"})
        self.assertNotIn("frame_count", captured["data"])

    def test_async_reference_retry_reopens_file_and_reuses_same_body_and_key(self):
        sent = []

        def post(url, **kwargs):
            sent.append({
                "url": url, "data": dict(kwargs["data"]), "key": kwargs["headers"]["Idempotency-Key"],
                "bytes": kwargs["files"]["image[]"][1].read(),
            })
            return response(status=503) if len(sent) == 1 else self.success()

        self.post.side_effect = post
        gateway.command_generate(self.args("--gif-profile", "cx", "--image", str(self.reference), "--background"))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[0]["bytes"], self.reference.read_bytes())
        self.assertEqual(self.out.read_bytes(), self.gif)

    def test_config_command_saves_profile_and_later_updates_preserve_it(self):
        args = gateway.build_parser().parse_args([
            "config", "--base", "https://gateway.example/v1", "--key", "offline-placeholder",
            "--endpoint-mode", "images", "--gif-profile", "cx",
        ])
        gateway.command_config(args)
        self.assertEqual(gateway.load_config()["gif_profile"], "cx")
        gateway.save_config("https://gateway.example/v1", "offline-placeholder", "auto", None, "images")
        self.assertEqual(gateway.load_config()["gif_profile"], "cx")

    def test_invalid_saved_profile_does_not_trigger_generation(self):
        self.write_config(gif_profile="unsupported-provider")
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args())
        self.post.assert_not_called()

    def test_explicit_gif_profile_is_rejected_for_static_images(self):
        self.out = self.root / "result.png"
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--gif-profile", "cx"))
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
