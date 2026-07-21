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
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def make_config():
    fingerprint = gateway.model_fingerprint(
        "https://gateway.example/v1", "secret", "responses"
    )
    return {
        "raw_base_url": "https://gateway.example/v1",
        "base_url": "https://gateway.example/v1",
        "responses_base_url": "https://gateway.example/responses",
        "api_key": "secret",
        "model": "auto",
        "responses_model": "",
        "model_candidates": list(gateway.DEFAULT_MODEL_CANDIDATES),
        "resolved_model": "",
        "model_cache_is_current": False,
        "model_fingerprint": fingerprint,
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


if __name__ == "__main__":
    unittest.main()
