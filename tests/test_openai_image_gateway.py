import argparse
import base64
import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "openai-image-gateway"
    / "scripts"
    / "openai_image_gateway.py"
)
SPEC = importlib.util.spec_from_file_location("openai_image_gateway", SCRIPT_PATH)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("invalid", self.text, 0)
        return self._payload


def make_args(**overrides):
    values = {
        "prompt": "draw a test image",
        "image": None,
        "size": "1536x1024",
        "quality": "high",
        "format": "png",
        "compression": 100,
        "model": None,
        "out": None,
        "background": False,
        "stream": False,
        "timeout": 30,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def make_config():
    raw_base_url = "https://gateway.example/v1"
    api_key = "secret"
    endpoint_fingerprint = gateway.endpoint_fingerprint(raw_base_url, api_key)
    fingerprint = gateway.model_fingerprint(raw_base_url, api_key, "responses")
    return {
        "raw_base_url": raw_base_url,
        "base_url": "https://gateway.example/v1",
        "responses_base_url": "https://gateway.example/responses",
        "api_key": api_key,
        "model": "auto",
        "responses_model": "",
        "model_candidates": list(gateway.DEFAULT_MODEL_CANDIDATES),
        "resolved_model": "",
        "model_cache_is_current": False,
        "model_fingerprint": fingerprint,
        "endpoint_mode": "auto",
        "endpoint_mode_is_current": False,
        "endpoint_fingerprint": endpoint_fingerprint,
        "last_successful_mode": "",
        "last_successful_mode_is_current": False,
    }


class ModelFallbackTests(unittest.TestCase):
    def test_candidates_include_unversioned_alias_last(self):
        cfg = make_config()

        candidates = gateway.model_candidates(cfg, "responses")

        self.assertEqual(
            candidates,
            ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image"],
        )

    def test_provider_model_list_is_preferred_over_unconfirmed_defaults(self):
        cfg = make_config()

        candidates = gateway.model_candidates(
            cfg,
            "responses",
            available_models={"gpt-image-1", "vendor-image-pro"},
        )

        self.assertEqual(
            candidates,
            [
                "gpt-image-1",
                "vendor-image-pro",
                "gpt-image-2",
                "gpt-image-1.5",
                "gpt-image",
            ],
        )

    def test_legacy_model_remains_a_responses_preference(self):
        cfg = make_config()
        cfg["model"] = "provider-response-model"

        candidates = gateway.model_candidates(cfg, "responses")

        self.assertEqual(candidates[0], "provider-response-model")

    def test_explicit_configured_model_precedes_cached_model(self):
        cfg = make_config()
        cfg.update(
            {
                "model": "configured-model",
                "resolved_model": "cached-model",
                "model_cache_is_current": True,
            }
        )

        candidates = gateway.model_candidates(cfg, "responses")

        self.assertEqual(candidates[:2], ["configured-model", "cached-model"])

    def test_malformed_success_response_does_not_cache_model(self):
        cfg = make_config()
        response = FakeResponse(200, {})

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=response),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    gateway.generate_with_responses(
                        cfg, make_args(model="configured-model"), 30, "png"
                    )

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertNotIn("resolved_model", saved)

    def test_exhausted_candidates_report_endpoint_and_attempted_models(self):
        cfg = make_config()
        responses = [
            FakeResponse(
                400,
                {"error": {"message": "unsupported model", "param": "model"}},
            )
            for _ in gateway.DEFAULT_MODEL_CANDIDATES
        ]
        stderr = io.StringIO()

        with (
            mock.patch.object(gateway.requests, "post", side_effect=responses),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit):
                gateway.generate_with_responses(cfg, make_args(), 30, "png")

        message = stderr.getvalue()
        self.assertIn(cfg["responses_base_url"], message)
        for model in gateway.DEFAULT_MODEL_CANDIDATES:
            self.assertIn(model, message)

    def test_retries_model_rejections_then_caches_first_success(self):
        cfg = make_config()
        image_bytes = b"generated-image"
        rejected = [
            FakeResponse(
                400,
                {"error": {"message": f"unsupported model: {name}", "param": "model"}},
            )
            for name in ("gpt-image-2", "gpt-image-1.5", "gpt-image-1")
        ]
        success = FakeResponse(
            200,
            {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": base64.b64encode(image_bytes).decode("ascii"),
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text(
                json.dumps({"base_url": cfg["raw_base_url"], "api_key": cfg["api_key"]}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(
                    gateway.requests, "post", side_effect=[*rejected, success]
                ) as post,
            ):
                raw, model = gateway.generate_with_responses(
                    cfg, make_args(), timeout=30, output_format="png"
                )

            self.assertEqual(raw, image_bytes)
            self.assertEqual(model, "gpt-image")
            self.assertEqual(
                [call.kwargs["json"]["model"] for call in post.call_args_list],
                ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image"],
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["resolved_model"], "gpt-image")
            self.assertEqual(saved["model_fingerprint"], cfg["model_fingerprint"])

    def test_does_not_retry_after_ambiguous_gateway_error(self):
        cfg = make_config()
        response = FakeResponse(504, text="Gateway Time-out")

        with mock.patch.object(gateway.requests, "post", return_value=response) as post:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    gateway.generate_with_responses(
                        cfg, make_args(), timeout=30, output_format="png"
                    )

        self.assertEqual(post.call_count, 1)

    def test_responses_tool_receives_generation_options(self):
        cfg = make_config()
        cfg["model"] = "gpt-image"
        image_bytes = b"image"
        response = FakeResponse(
            200,
            {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": base64.b64encode(image_bytes).decode("ascii"),
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=response) as post,
            ):
                gateway.generate_with_responses(
                    cfg,
                    make_args(format="webp", compression=72),
                    timeout=30,
                    output_format="webp",
                )

        tool = post.call_args.kwargs["json"]["tools"][0]
        self.assertEqual(
            tool,
            {
                "type": "image_generation",
                "size": "1536x1024",
                "quality": "high",
                "output_format": "webp",
                "output_compression": 72,
            },
        )

    def test_model_list_lookup_is_read_only_and_does_not_follow_redirects(self):
        cfg = make_config()
        response = FakeResponse(
            200,
            {"data": [{"id": "gpt-image"}, {"id": "text-model"}]},
        )

        with mock.patch.object(gateway.requests, "get", return_value=response) as get:
            models = gateway.fetch_available_models(cfg, timeout=30)

        self.assertEqual(models, {"gpt-image", "text-model"})
        self.assertEqual(get.call_args.args[0], "https://gateway.example/v1/models")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])


class ConfigurationTests(unittest.TestCase):
    def test_first_run_defaults_to_auto_model_and_optional_key(self):
        parser = gateway.build_parser()

        args = parser.parse_args(["config", "--base", "https://gateway.example"])

        self.assertIsNone(args.key)
        self.assertEqual(args.model, "auto")

    def test_config_accepts_and_persists_a_separate_responses_model(self):
        parser = gateway.build_parser()
        args = parser.parse_args(
            [
                "config",
                "--base",
                "https://gateway.example",
                "--key",
                "secret",
                "--model",
                "gpt-image-2",
                "--responses-model",
                "gpt-5.4",
                "--endpoint-mode",
                "images",
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                gateway.command_config(args)
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["model"], "gpt-image-2")
        self.assertEqual(saved["responses_model"], "gpt-5.4")
        self.assertEqual(saved["endpoint_mode"], "images")

    def test_config_without_responses_model_preserves_existing_value(self):
        parser = gateway.build_parser()
        args = parser.parse_args(
            [
                "config",
                "--base",
                "https://gateway.example",
                "--key",
                "secret",
                "--model",
                "gpt-image-2",
                "--endpoint-mode",
                "images",
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text(
                json.dumps({"responses_model": "gpt-5.4"}), encoding="utf-8"
            )
            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                gateway.command_config(args)
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["responses_model"], "gpt-5.4")

    def test_resolved_model_cache_is_scoped_to_endpoint_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            raw_base_url = "https://gateway.example/v1"
            api_key = "secret"
            endpoint_fingerprint = gateway.endpoint_fingerprint(raw_base_url, api_key)
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": raw_base_url,
                        "api_key": api_key,
                        "endpoint_mode": "responses",
                        "endpoint_mode_fingerprint": endpoint_fingerprint,
                        "resolved_model": "images-only-model",
                        "resolved_endpoint_mode": "images",
                        "model_fingerprint": gateway.model_fingerprint(
                            raw_base_url, api_key, "images"
                        ),
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                cfg = gateway.load_config()

        self.assertFalse(cfg["model_cache_is_current"])


class EndpointSelectionV2Tests(unittest.TestCase):
    def _write_config(self, path, endpoint_mode="auto", **extra):
        raw_base_url = "https://gateway.example"
        api_key = "sk-secret-value"
        payload = {
            "base_url": raw_base_url,
            "api_key": api_key,
            "model": "gpt-image-2",
            "responses_model": "gpt-5.4",
            "endpoint_mode": endpoint_mode,
        }
        payload.update(extra)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_default_test_uses_read_only_model_lookup(self):
        cfg = make_config()
        stdout = io.StringIO()

        with (
            mock.patch.object(gateway, "load_config", return_value=cfg),
            mock.patch.object(
                gateway.requests,
                "get",
                return_value=FakeResponse(200, {"data": []}),
            ) as get,
            mock.patch.object(gateway.requests, "post") as post,
            contextlib.redirect_stdout(stdout),
        ):
            gateway.command_test(
                argparse.Namespace(
                    timeout=30,
                    select=False,
                    probe_generation_route=False,
                )
            )

        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://gateway.example/v1/models")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        post.assert_not_called()
        self.assertIn("Read-only check: 200", stdout.getvalue())

    def test_explicit_route_probe_posts_with_probe_header(self):
        cfg = make_config()

        with (
            mock.patch.object(gateway, "load_config", return_value=cfg),
            mock.patch.object(
                gateway.requests,
                "post",
                return_value=FakeResponse(400),
            ) as post,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            gateway.command_test(
                argparse.Namespace(
                    timeout=30,
                    select=False,
                    probe_generation_route=True,
                )
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args.kwargs["headers"].get("X-Image-Gateway-Probe"),
            "1",
        )

    def test_manual_images_mode_is_not_overwritten_by_test(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="images")
            responses = [FakeResponse(502), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_test(
                    argparse.Namespace(
                        timeout=30,
                        select=False,
                        probe_generation_route=True,
                    )
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["endpoint_mode"], "images")

    def test_manual_mode_is_probed_first_even_when_last_success_differs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            fingerprint = gateway.endpoint_fingerprint(
                "https://gateway.example", "sk-secret-value"
            )
            self._write_config(
                config_path,
                endpoint_mode="images",
                last_successful_mode="responses",
                last_successful_mode_fingerprint=fingerprint,
            )
            responses = [FakeResponse(400), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(
                    gateway.requests, "post", side_effect=responses
                ) as post,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_test(
                    argparse.Namespace(
                        timeout=30,
                        select=False,
                        probe_generation_route=True,
                    )
                )

        self.assertTrue(post.call_args_list[0].args[0].endswith("/images/generations"))

    def test_manual_mode_is_not_overwritten_even_with_test_select(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="images")
            responses = [FakeResponse(502), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_test(argparse.Namespace(timeout=30, select=True))

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["endpoint_mode"], "images")

    def test_default_test_is_read_only_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            original = self._write_config(config_path, endpoint_mode="auto")
            responses = [FakeResponse(502), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                mock.patch.object(
                    gateway.requests,
                    "get",
                    return_value=FakeResponse(200, {"data": []}),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_test(argparse.Namespace(timeout=30, select=False))

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved, original)

    def test_400_probe_is_reported_as_unverified_not_generation_capable(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="auto")
            responses = [FakeResponse(502), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                contextlib.redirect_stdout(stdout),
            ):
                gateway.command_test(
                    argparse.Namespace(
                        timeout=30,
                        select=False,
                        probe_generation_route=True,
                    )
                )

        output = stdout.getvalue()
        self.assertIn("responses=400 (reachable, generation unverified)", output)
        self.assertNotIn("Selected endpoint mode", output)

    def test_test_select_updates_only_auto_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="auto")
            responses = [FakeResponse(502), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_test(argparse.Namespace(timeout=30, select=True))

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["endpoint_mode"], "responses")

    def test_auto_generation_prefers_last_successful_mode_without_probing(self):
        cfg = make_config()
        cfg.update(
            {
                "endpoint_mode": "auto",
                "last_successful_mode": "images",
                "last_successful_mode_is_current": True,
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "image.png"
            args = make_args(out=str(out_path), background=False, stream=False)
            with (
                mock.patch.object(gateway, "load_config", return_value=cfg),
                mock.patch.object(
                    gateway,
                    "probe_endpoints",
                    side_effect=AssertionError("last successful mode should skip probing"),
                ),
                mock.patch.object(gateway, "fetch_available_models", return_value=set()),
                mock.patch.object(
                    gateway,
                    "generate_with_images",
                    return_value=(b"image", "gpt-image-2", "generate"),
                ) as generate,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.command_generate(args)

        generate.assert_called_once()

    def test_successful_images_generation_caches_last_successful_mode(self):
        image_bytes = b"generated-image"
        response = FakeResponse(
            200,
            {"data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}]},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="images")
            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                cfg = gateway.load_config()
                with mock.patch.object(gateway.requests, "post", return_value=response):
                    raw, _, _ = gateway.generate_with_images(
                        cfg, make_args(), timeout=30, output_format="png"
                    )
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(raw, image_bytes)
        self.assertEqual(saved["last_successful_mode"], "images")

    def test_successful_responses_generation_caches_last_successful_mode(self):
        image_bytes = b"generated-image"
        response = FakeResponse(
            200,
            {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": base64.b64encode(image_bytes).decode("ascii"),
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="responses")
            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                cfg = gateway.load_config()
                with mock.patch.object(gateway.requests, "post", return_value=response):
                    raw, _ = gateway.generate_with_responses(
                        cfg, make_args(), timeout=30, output_format="png"
                    )
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(raw, image_bytes)
        self.assertEqual(saved["last_successful_mode"], "responses")

    def test_responses_503_does_not_fallback_or_cache_success(self):
        response = FakeResponse(503, text="Service temporarily unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            original = self._write_config(config_path, endpoint_mode="responses")
            with mock.patch.object(gateway, "CONFIG_PATH", config_path):
                cfg = gateway.load_config()
                with (
                    mock.patch.object(gateway.requests, "post", return_value=response) as post,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    gateway.generate_with_responses(
                        cfg, make_args(), timeout=30, output_format="png"
                    )
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(post.call_count, 1)
        self.assertEqual(saved, original)

    def test_test_output_masks_the_full_api_key(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            self._write_config(config_path, endpoint_mode="images")
            responses = [FakeResponse(400), FakeResponse(400)]

            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", side_effect=responses),
                mock.patch.object(
                    gateway.requests,
                    "get",
                    return_value=FakeResponse(200, {"data": []}),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                gateway.command_test(argparse.Namespace(timeout=30, select=False))

        self.assertNotIn("sk-secret-value", stdout.getvalue())


class StreamingResponse:
    status_code = 200
    text = ""

    def __init__(self, events):
        self._events = events

    def iter_lines(self, decode_unicode=False):
        for event in self._events:
            yield "data: " + json.dumps(event)

    def json(self):
        raise AssertionError("a streaming response must be parsed as SSE, not JSON")


def completed_event(image_bytes):
    return {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": base64.b64encode(image_bytes).decode("ascii"),
                }
            ]
        },
    }


class StreamingTests(unittest.TestCase):
    def _generate(self, response, **arg_overrides):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=response) as post,
            ):
                raw, model = gateway.generate_with_responses(
                    cfg,
                    make_args(stream=True, background=False, **arg_overrides),
                    timeout=30,
                    output_format="png",
                )
        return raw, model, post

    def test_saves_only_the_final_image_from_a_stream(self):
        response = StreamingResponse(
            [
                {
                    "type": "response.image_generation_call.partial_image",
                    "partial_image_b64": base64.b64encode(b"preview").decode("ascii"),
                },
                completed_event(b"final-image"),
            ]
        )

        raw, _, post = self._generate(response)

        self.assertEqual(raw, b"final-image")
        self.assertTrue(post.call_args.kwargs["stream"])
        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["stream"])
        self.assertEqual(
            payload["tools"][0]["partial_images"], gateway.PARTIAL_IMAGE_COUNT
        )

    def test_truncated_stream_fails_instead_of_saving_a_partial_preview(self):
        response = StreamingResponse(
            [
                {
                    "type": "response.image_generation_call.partial_image",
                    "partial_image_b64": base64.b64encode(b"preview").decode("ascii"),
                }
            ]
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            self._generate(response)

        self.assertIn("partial previews were discarded", stderr.getvalue())

    def test_stream_failure_event_reports_the_upstream_message(self):
        response = StreamingResponse(
            [{"type": "error", "error": {"message": "content policy violation"}}]
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            self._generate(response)

        self.assertIn("content policy violation", stderr.getvalue())

    def test_streaming_is_not_requested_by_default(self):
        cfg = make_config()
        response = FakeResponse(200, completed_event(b"image")["response"])

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=response) as post,
            ):
                gateway.generate_with_responses(
                    cfg, make_args(), timeout=30, output_format="png"
                )

        payload = post.call_args.kwargs["json"]
        self.assertNotIn("stream", payload)
        self.assertNotIn("background", payload)
        self.assertNotIn("partial_images", payload["tools"][0])
        self.assertFalse(post.call_args.kwargs["stream"])


class BackgroundTests(unittest.TestCase):
    def test_polls_until_the_background_response_completes(self):
        cfg = make_config()
        queued = FakeResponse(200, {"id": "resp_1", "status": "queued", "output": []})
        done = FakeResponse(
            200, dict(completed_event(b"background-image")["response"], status="completed")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=queued) as post,
                mock.patch.object(gateway.requests, "get", return_value=done) as get,
                mock.patch.object(gateway.time, "sleep") as sleep,
            ):
                raw, _ = gateway.generate_with_responses(
                    cfg,
                    make_args(background=True, stream=False),
                    timeout=30,
                    output_format="png",
                )

        self.assertEqual(raw, b"background-image")
        self.assertTrue(post.call_args.kwargs["json"]["background"])
        self.assertEqual(
            get.call_args.args[0], "https://gateway.example/responses/resp_1"
        )
        sleep.assert_called_once_with(gateway.BACKGROUND_POLL_SECONDS)

    def test_failed_background_response_reports_the_upstream_message(self):
        cfg = make_config()
        queued = FakeResponse(200, {"id": "resp_1", "status": "in_progress"})
        failed = FakeResponse(
            200,
            {
                "id": "resp_1",
                "status": "failed",
                "error": {"message": "generation rejected upstream"},
            },
        )

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "local_config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(gateway, "CONFIG_PATH", config_path),
                mock.patch.object(gateway.requests, "post", return_value=queued),
                mock.patch.object(gateway.requests, "get", return_value=failed),
                mock.patch.object(gateway.time, "sleep"),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                gateway.generate_with_responses(
                    cfg,
                    make_args(background=True, stream=False),
                    timeout=30,
                    output_format="png",
                )

        self.assertIn("generation rejected upstream", stderr.getvalue())

    def test_background_polling_stops_at_the_timeout_budget(self):
        cfg = make_config()
        stuck = FakeResponse(200, {"id": "resp_1", "status": "in_progress"})
        clock = iter([0.0, 0.0, 5.0, 100.0, 100.0])

        stderr = io.StringIO()
        with (
            mock.patch.object(gateway.requests, "get", return_value=stuck),
            mock.patch.object(gateway.time, "sleep"),
            mock.patch.object(gateway.time, "monotonic", lambda: next(clock)),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            gateway.await_background_response(
                cfg, {"id": "resp_1", "status": "in_progress"}, {}, timeout=30
            )

        self.assertIn("still in_progress after 30s", stderr.getvalue())

    def test_responses_only_flags_are_rejected_on_the_images_endpoint(self):
        parser = gateway.build_parser()
        args = parser.parse_args(
            ["generate", "--prompt", "a cat", "--out", "/tmp/cat.png", "--stream"]
        )

        cfg = dict(make_config(), endpoint_mode="images", endpoint_mode_is_current=True)
        stderr = io.StringIO()
        with (
            mock.patch.object(gateway, "load_config", return_value=cfg),
            mock.patch.object(
                gateway, "select_endpoint_mode", return_value=("images", [])
            ),
            mock.patch.object(gateway, "fetch_available_models", return_value=set()),
            mock.patch.object(gateway, "generate_with_images") as generate,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            gateway.command_generate(args)

        generate.assert_not_called()
        self.assertIn("--stream requires the responses endpoint", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
