import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gif_gateway", ROOT / "skills/openai-image-gateway/scripts/openai_image_gateway.py"
)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


def image_bytes(fmt, animated=False):
    output = io.BytesIO()
    first = Image.new("RGB", (16, 16), "red")
    options = {}
    if animated:
        options = {
            "save_all": True,
            "append_images": [Image.new("RGB", (16, 16), "blue")],
            "duration": 200,
            "loop": 0,
        }
    first.save(output, format=fmt, **options)
    return output.getvalue()


def response(payload=None, status=200, content=b""):
    payload = payload or {}
    return Mock(
        status_code=status, json=Mock(return_value=payload),
        text=json.dumps(payload), content=content, headers={},
    )


class GifGenerationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.config = self.root / "local_config.json"
        self.write_config()
        self.stack.enter_context(patch.object(gateway, "CONFIG_PATH", self.config))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.post = self.stack.enter_context(patch.object(
            gateway.requests, "post", side_effect=AssertionError("Unexpected generation POST")
        ))
        self.get = self.stack.enter_context(patch.object(
            gateway.requests, "get", side_effect=AssertionError("Unexpected GET")
        ))
        self.stack.enter_context(patch.object(gateway.time, "sleep"))
        self.gif = image_bytes("GIF", animated=True)
        self.out = self.root / "result.gif"

    def write_config(self, **overrides):
        payload = {
            "base_url": "https://gateway.example/v1",
            "api_key": "offline-placeholder",
            "model": "gpt-image-2",
            "endpoint_mode": "images",
        }
        payload.update(overrides)
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def args(self, *extra):
        return gateway.build_parser().parse_args([
            "generate", "--prompt", "A red ball bounces in a seamless loop",
            "--out", str(self.out), *extra,
        ])

    def set_success(self, raw=None):
        raw = self.gif if raw is None else raw
        self.post.side_effect = None
        self.post.return_value = response({
            "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}],
        })

    def test_gif_suffix_requests_animation_and_preserves_all_frames(self):
        self.set_success()
        gateway.command_generate(self.args("--model", "gpt-image-2.6-sunburst"))
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "model": "gpt-image-2.6-sunburst", "prompt": "A red ball bounces in a seamless loop",
            "n": 1, "quality": "low", "output_format": "gif",
            "frame_count": 9, "frame_duration_ms": 200,
        })
        self.assertEqual(self.out.read_bytes(), self.gif)
        with Image.open(self.out) as image:
            self.assertEqual(image.format, "GIF")
            self.assertEqual(image.n_frames, 2)
        self.assertEqual(json.loads(self.config.read_text())["resolved_model"], "gpt-image-2.6-sunburst")
        self.get.assert_not_called()

    def test_explicit_gif_parameters_reach_gateway(self):
        self.out = self.root / "animation"
        self.set_success()
        gateway.command_generate(self.args(
            "--format", "gif", "--frame-count", "16", "--frame-duration-ms", "120",
            "--size", "512x512", "--model", "gpt-image-2.6-flare",
        ))
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(payload["output_format"], "gif")
        self.assertEqual(payload["frame_count"], 16)
        self.assertEqual(payload["frame_duration_ms"], 120)
        self.assertEqual(payload["size"], "512x512")
        self.assertTrue(self.out.exists())

    def test_default_output_path_uses_gif_extension(self):
        self.set_success()
        args = gateway.build_parser().parse_args([
            "generate", "--prompt", "Bounce", "--format", "gif",
            "--model", "gpt-image-2.6-flare",
        ])
        with patch.object(gateway.Path, "cwd", return_value=self.root):
            gateway.command_generate(args)
        self.assertEqual(len(list((self.root / "generated").glob("*.gif"))), 1)

    def test_invalid_combinations_fail_before_network_or_file_replacement(self):
        for extra in [
            ["--image", "reference.png"], ["--stream"], ["--compression", "50"],
            ["--image-size", "2K"], ["--size", "511x512"], ["--size", "1:1"],
            ["--size", "0x512"], ["--format", "png"],
        ]:
            with self.subTest(extra=extra):
                self.out.write_bytes(b"existing output")
                with self.assertRaises(SystemExit):
                    gateway.command_generate(self.args(*extra))
                self.assertEqual(self.out.read_bytes(), b"existing output")
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_explicit_gif_with_png_filename_is_rejected(self):
        self.out = self.root / "wrong.png"
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--format", "gif"))
        self.post.assert_not_called()

    def test_animation_parameters_are_rejected_for_static_images(self):
        self.out = self.root / "still.png"
        for extra in [["--frame-count", "9"], ["--frame-duration-ms", "200"]]:
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                gateway.command_generate(self.args(*extra))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_animation_parameter_ranges_are_validated(self):
        for flag, values in [
            ("--frame-count", ["1", "65", "abc"]),
            ("--frame-duration-ms", ["19", "2001", "abc"]),
        ]:
            for value in values:
                with self.subTest(flag=flag, value=value), self.assertRaises(SystemExit):
                    self.args(flag, value)
        args = self.args("--frame-count", "64", "--frame-duration-ms", "2000")
        self.assertEqual((args.frame_count, args.frame_duration_ms), (64, 2000))

    def test_responses_mode_is_rejected_before_any_request(self):
        self.write_config(endpoint_mode="responses")
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--model", "gpt-image-2.6-sunburst"))
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_auto_mode_sends_gif_to_images_without_generation_probe(self):
        self.write_config(endpoint_mode="auto")
        self.set_success()
        gateway.command_generate(self.args("--model", "gpt-image-2.6-sunburst"))
        self.assertEqual(self.post.call_args.args[0], "https://gateway.example/v1/images/generations")
        self.get.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text())["endpoint_mode"], "auto")

    def test_png_or_single_frame_gif_is_not_saved_as_animation_or_cached(self):
        for raw in [
            image_bytes("PNG"), image_bytes("GIF"), b"GIF89a broken", self.gif[:40],
            self.gif[:-1], self.gif[:-2], self.gif[:-2] + self.gif[-1:],
        ]:
            with self.subTest(raw=raw[:10]):
                self.write_config()
                self.out.write_bytes(b"existing output")
                self.set_success(raw)
                with self.assertRaises(SystemExit):
                    gateway.command_generate(self.args("--model", "gpt-image-2.6-sunburst"))
                self.assertEqual(self.out.read_bytes(), b"existing output")
                self.assertNotIn("resolved_model", json.loads(self.config.read_text()))

    def test_gif_download_preserves_bytes_without_forwarding_gateway_key(self):
        self.post.side_effect = None
        self.post.return_value = response({"data": [{"url": "https://cdn.example/animation.gif"}]})
        self.get.side_effect = None
        self.get.return_value = response(content=self.gif)
        gateway.command_generate(self.args("--model", "gpt-image-2.6-flare"))
        self.assertEqual(self.out.read_bytes(), self.gif)
        self.assertNotIn("Authorization", self.get.call_args.kwargs.get("headers", {}))

    def test_automatic_gif_selection_skips_static_configured_model(self):
        self.set_success()
        self.get.side_effect = None
        self.get.return_value = response({"data": [
            {"id": "gpt-image-2"}, {"id": "gpt-image-2.6-flare"},
        ]})
        gateway.command_generate(self.args())
        self.assertEqual(self.post.call_args.kwargs["json"]["model"], "gpt-image-2.6-flare")

    def test_explicit_gif_model_is_not_replaced_after_model_rejection(self):
        self.post.side_effect = None
        self.post.return_value = response({"error": {"code": "model_not_found"}}, status=400)
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--model", "my-gif-alias"))
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs["json"]["model"], "my-gif-alias")

    def test_automatic_selection_falls_back_only_after_definitive_model_rejection(self):
        self.post.side_effect = [
            response({"error": {"code": "model_not_found"}}, status=400),
            response({"data": [{"b64_json": base64.b64encode(self.gif).decode("ascii")}]}),
        ]
        self.get.side_effect = None
        self.get.return_value = response({"data": [
            {"id": "gpt-image-2.6-sunburst"}, {"id": "gpt-image-2.6-flare"},
        ]})
        gateway.command_generate(self.args())
        models = [call.kwargs["json"]["model"] for call in self.post.call_args_list]
        self.assertEqual(models, ["gpt-image-2.6-sunburst", "gpt-image-2.6-flare"])

    def test_gif_timeout_does_not_submit_another_model(self):
        self.post.side_effect = gateway.requests.Timeout("ambiguous upstream outcome")
        self.get.side_effect = None
        self.get.return_value = response({"data": [
            {"id": "gpt-image-2.6-sunburst"}, {"id": "gpt-image-2.6-flare"},
        ]})
        with self.assertRaises(gateway.requests.Timeout):
            gateway.command_generate(self.args())
        self.assertEqual(self.post.call_count, 1)

    def test_gif_http_errors_do_not_submit_another_model(self):
        for status in [400, 429, 500, 503]:
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.side_effect = None
                self.post.return_value = response({"error": {"message": "upstream failure"}}, status=status)
                self.get.side_effect = None
                self.get.return_value = response({"data": [
                    {"id": "gpt-image-2.6-sunburst"}, {"id": "gpt-image-2.6-flare"},
                ]})
                with self.assertRaises(SystemExit):
                    gateway.command_generate(self.args())
                self.assertEqual(self.post.call_count, 1)

    def test_background_gif_uses_existing_task_polling(self):
        self.post.side_effect = None
        self.post.return_value = response({"id": "task-123", "status": "queued"}, status=202)
        self.get.side_effect = None
        self.get.return_value = response({
            "id": "task-123", "status": "completed",
            "result": {"data": [{"b64_json": base64.b64encode(self.gif).decode("ascii")}]},
        })
        gateway.command_generate(self.args("--model", "gpt-image-2.6-flare", "--background"))
        self.assertTrue(self.post.call_args.args[0].endswith("/images/generations?async=true"))
        self.assertTrue(self.get.call_args.args[0].endswith("/images/tasks/task-123"))
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.out.read_bytes(), self.gif)

    def test_static_png_request_keeps_existing_defaults_without_animation_fields(self):
        self.out = self.root / "still.png"
        self.set_success(image_bytes("PNG"))
        gateway.command_generate(self.args("--model", "gpt-image-2"))
        self.assertEqual(self.post.call_args.kwargs["json"], {
            "model": "gpt-image-2", "prompt": "A red ball bounces in a seamless loop",
            "n": 1, "quality": "low", "output_format": "png", "size": "1024x1024",
        })

    def test_missing_pillow_stops_gif_before_any_request(self):
        with patch.dict(sys.modules, {"PIL": None}), self.assertRaises(SystemExit):
            gateway.command_generate(self.args("--model", "gpt-image-2.6-sunburst"))
        self.post.assert_not_called()
        self.get.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_static_images_do_not_require_pillow(self):
        self.out = self.root / "still.png"
        self.set_success(image_bytes("PNG"))
        with patch.dict(sys.modules, {"PIL": None}):
            gateway.command_generate(self.args("--model", "gpt-image-2"))
        self.assertTrue(self.out.exists())

    def test_uncreatable_output_directory_fails_before_generation_or_cache_update(self):
        ancestor = self.root / "regular-file"
        ancestor.write_text("not a directory")
        self.set_success()
        for suffix in ["gif", "png"]:
            with self.subTest(suffix=suffix):
                self.write_config()
                self.out = ancestor / "child" / f"result.{suffix}"
                with self.assertRaises(OSError):
                    gateway.command_generate(self.args("--model", "gpt-image-2.6-flare"))
                self.post.assert_not_called()
                self.assertNotIn("resolved_model", json.loads(self.config.read_text()))

    def test_static_model_cache_does_not_skip_gif_model_discovery(self):
        gateway.save_generation_success(gateway.load_config(), "images", "gpt-image-2")
        self.assertTrue(gateway.load_config()["model_cache_is_current"])
        self.set_success()
        self.get.side_effect = None
        self.get.return_value = response({"data": [{"id": "gpt-image-2.6-flare"}]})
        gateway.command_generate(self.args())
        self.assertEqual(self.post.call_args.kwargs["json"]["model"], "gpt-image-2.6-flare")

    def test_static_only_model_list_cannot_trigger_a_gif_submission(self):
        self.get.side_effect = None
        self.get.return_value = response({"data": [{"id": "gpt-image-2"}]})
        with self.assertRaises(SystemExit):
            gateway.command_generate(self.args())
        self.post.assert_not_called()

    def test_responses_static_image_keeps_its_default_size(self):
        self.write_config(endpoint_mode="responses")
        self.out = self.root / "still.webp"
        raw = image_bytes("WEBP")
        self.post.side_effect = None
        self.post.return_value = response({"output": [{
            "type": "image_generation_call", "result": base64.b64encode(raw).decode("ascii"),
        }]})
        gateway.command_generate(self.args("--model", "responses-model"))
        self.assertEqual(self.out.read_bytes(), raw)
        self.assertEqual(self.post.call_args.kwargs["json"]["tools"], [{
            "type": "image_generation", "size": "1024x1024", "quality": "low", "output_format": "webp",
        }])


if __name__ == "__main__":
    unittest.main()
