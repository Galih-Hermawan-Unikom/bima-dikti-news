"""Offline tests of the exact inline workflow code; never use real credentials."""

import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import textwrap
import unittest
from unittest.mock import Mock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/telegram-preflight.yml"


def inline_code():
    block = textwrap.dedent(WORKFLOW.read_text(encoding="utf-8").split("        run: |\n", 1)[1])
    if not block.startswith("python3 -B - <<'PY'\n") or not block.endswith("PY\n"):
        raise AssertionError("Unexpected inline shell structure")
    return block.split("\n", 1)[1].rsplit("\nPY\n", 1)[0]


class TelegramPreflightTests(unittest.TestCase):
    def setUp(self):
        self.no_socket = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.no_http = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden"))
        self.no_socket.start()
        self.no_http.start()
        self.addCleanup(self.no_socket.stop)
        self.addCleanup(self.no_http.stop)
        self.ns = {"__name__": "offline_preflight_test"}
        exec(compile(inline_code(), str(WORKFLOW) + ":inline", "exec"), self.ns)
        self.env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/master",
            "GITHUB_REPOSITORY": "Galih-Hermawan-Unikom/bima-dikti-news",
            "TELEGRAM_BOT_TOKEN": "123456:OFFLINE_ONLY_SYNTHETIC_CREDENTIAL",
            "TELEGRAM_CHAT_ID": "-100000000123",
        }
        self.results = [
            {"id": 123456, "is_bot": True, "username": "bima_dikti_bot", "first_name": self.env["TELEGRAM_BOT_TOKEN"]},
            {"id": -100000000123, "type": "channel", "username": "dikti_news", "title": "DIKTI News"},
            {"status": "administrator", "can_post_messages": True, "user": {"id": 123456, "is_bot": True}},
        ]

    def invoke(self, results=None, changes=None, exception=None):
        env = dict(self.env)
        for key, value in (changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        api = Mock(side_effect=exception or copy.deepcopy(self.results if results is None else results))
        stream = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.dict(self.ns, {"api_get": api}), contextlib.redirect_stdout(stream):
            code = self.ns["main"]()
        output = stream.getvalue()
        self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], output)
        self.assertNotIn(self.env["TELEGRAM_CHAT_ID"], output)
        self.assertNotIn("https://api.telegram.org/bot", output)
        return code, output, api

    def test_pass_uses_exact_three_methods_and_secret_target(self):
        code, output, api = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual([call.args[1] for call in api.call_args_list], ["getMe", "getChat", "getChatMember"])
        self.assertEqual(api.call_args_list[1].args[2], {"chat_id": self.env["TELEGRAM_CHAT_ID"]})
        self.assertEqual(api.call_args_list[2].args[2], {"chat_id": -100000000123, "user_id": 123456})
        self.assertIn("Bot: @bima_dikti_bot", output)
        self.assertIn("Channel: DIKTI News (@dikti_news), type=channel", output)

    def test_defaults_are_effective_without_variables(self):
        code, output, _ = self.invoke()
        self.assertEqual(code, 0)
        for value in ["NOTIFICATION_METHOD=telegram", "TELEGRAM_SEND_FILES=false", "YOUTUBE_MONITOR_ENABLED=true"]:
            self.assertIn(value, output)

    def test_explicit_boolean_overrides(self):
        code, output, _ = self.invoke(changes={"NOTIFICATION_METHOD": "both", "TELEGRAM_SEND_FILES": " YES ", "YOUTUBE_MONITOR_ENABLED": "0"})
        self.assertEqual(code, 0)
        self.assertIn("TELEGRAM_SEND_FILES=true", output)
        self.assertIn("YOUTUBE_MONITOR_ENABLED=false", output)

    def test_boolean_values_match_production_parser(self):
        source = ast.parse((ROOT / "app_config.py").read_text(encoding="utf-8"))
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_read_bool")
        reference = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "production_boolean_only", "exec"), reference)
        for default in [False, True]:
            for value in ["1", "true", " YES ", "y", "on", "0", "false", " NO ", "n", "off", ""]:
                with self.subTest(default=default, value=value), patch.dict(os.environ, {"SETTING": value}, clear=True):
                    self.assertEqual(self.ns["boolean_setting"]("SETTING", default), reference["_read_bool"](value or str(default), default))

    def test_invalid_boolean_reports_effective_fallback_but_fails(self):
        for name, expected in [("TELEGRAM_SEND_FILES", "false"), ("YOUTUBE_MONITOR_ENABLED", "true")]:
            with self.subTest(name=name):
                code, output, api = self.invoke(changes={name: "INVALID_DO_NOT_ECHO"})
                self.assertEqual(code, 1)
                self.assertIn(name + "=" + expected, output)
                self.assertNotIn("INVALID_DO_NOT_ECHO", output)
                api.assert_not_called()

    def test_disabled_or_invalid_notification_fails_before_api(self):
        for value in ["console", "INVALID_DO_NOT_ECHO"]:
            with self.subTest(value=value):
                code, output, api = self.invoke(changes={"NOTIFICATION_METHOD": value})
                self.assertEqual(code, 1)
                self.assertNotIn("INVALID_DO_NOT_ECHO", output)
                api.assert_not_called()

    def test_untrusted_execution_context_fails_before_api(self):
        for changes in [{"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "pull_request_target"}, {"GITHUB_REF": "refs/heads/fix/reliability-phase1"}, {"GITHUB_REPOSITORY": "untrusted/fork"}]:
            code, _, api = self.invoke(changes=changes)
            self.assertEqual(code, 1)
            api.assert_not_called()

    def test_debug_mode_fails_before_api(self):
        for changes in [{"RUNNER_DEBUG": "1"}, {"ACTIONS_STEP_DEBUG": "true"}]:
            code, _, api = self.invoke(changes=changes)
            self.assertEqual(code, 1)
            api.assert_not_called()

    def test_missing_or_invalid_credentials_fail_safely(self):
        for changes in [{"TELEGRAM_BOT_TOKEN": None}, {"TELEGRAM_CHAT_ID": None}, {"TELEGRAM_BOT_TOKEN": "bad/token?format"}, {"TELEGRAM_CHAT_ID": "123"}, {"TELEGRAM_CHAT_ID": "@kanal_eksperimen"}]:
            code, _, api = self.invoke(changes=changes)
            self.assertEqual(code, 1)
            api.assert_not_called()

    def test_public_channel_username_secret_is_supported(self):
        code, _, _ = self.invoke(changes={"TELEGRAM_CHAT_ID": "@dikti_news"})
        self.assertEqual(code, 0)

    def test_wrong_bot_is_rejected(self):
        for changes in [{"username": "presensi_unikom_bot"}, {"id": 999}, {"id": True}, {"is_bot": False}]:
            results = copy.deepcopy(self.results)
            results[0].update(changes)
            code, _, api = self.invoke(results=results)
            self.assertEqual(code, 1)
            self.assertEqual(api.call_count, 1)

    def test_wrong_target_is_rejected(self):
        for changes in [{"title": "Kanal Eksperimen"}, {"username": "experiment"}, {"type": "private"}, {"type": "supergroup"}, {"id": -100999}, {"id": True}]:
            results = copy.deepcopy(self.results)
            results[1].update(changes)
            code, _, api = self.invoke(results=results)
            self.assertEqual(code, 1)
            self.assertEqual(api.call_count, 2)

    def test_membership_requires_administrator_and_posting(self):
        for changes in [{"status": "member"}, {"status": "creator"}, {"can_post_messages": False}, {"can_post_messages": 1}, {"user": {"id": 999, "is_bot": True}}, {"user": {"id": 123456, "is_bot": False}}, {"user": None}]:
            results = copy.deepcopy(self.results)
            results[2].update(changes)
            code, _, _ = self.invoke(results=results)
            self.assertEqual(code, 1)

    def test_unexpected_exception_never_echoes_details(self):
        code, _, _ = self.invoke(exception=RuntimeError(self.env["TELEGRAM_BOT_TOKEN"] + self.env["TELEGRAM_CHAT_ID"]))
        self.assertEqual(code, 1)

    def transport(self, payload=None, status=200, content_type="application/json", exception=None, raw=None):
        response = Mock(status=status, headers={"Content-Type": content_type})
        response.read.return_value = raw if raw is not None else json.dumps(payload or {"ok": True, "result": {"safe": True}}).encode()
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.side_effect = exception
        opener.open.return_value = manager
        return opener, response

    def test_transport_get_and_timeout_no_secret_output(self):
        opener, response = self.transport()
        stream = io.StringIO()
        with patch("urllib.request.build_opener", return_value=opener), contextlib.redirect_stdout(stream):
            result = self.ns["api_get"](self.env["TELEGRAM_BOT_TOKEN"], "getChat", {"chat_id": self.env["TELEGRAM_CHAT_ID"]})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.host, "api.telegram.org")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        response.read.assert_called_once_with(1024 * 1024 + 1)
        self.assertEqual(result, {"safe": True})
        self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], stream.getvalue())
        self.assertNotIn(self.env["TELEGRAM_CHAT_ID"], stream.getvalue())

    def test_transport_rejects_all_other_methods_before_network(self):
        for method in ["sendMessage", "sendDocument", "getUpdates", "getFile", "GETME"]:
            with patch("urllib.request.build_opener") as build, self.assertRaises(self.ns["PreflightError"]):
                self.ns["api_get"](self.env["TELEGRAM_BOT_TOKEN"], method)
            build.assert_not_called()

    def test_transport_rejects_bad_http_or_payload(self):
        cases = [dict(status=500), dict(content_type="text/html"), dict(payload={"ok": False, "description": self.env["TELEGRAM_BOT_TOKEN"]}), dict(payload={"ok": True, "result": []}), dict(raw=b"not-json"), dict(raw=b"x" * (1024 * 1024 + 1))]
        for case in cases:
            opener, _ = self.transport(**case)
            with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(self.ns["PreflightError"]) as error:
                self.ns["api_get"](self.env["TELEGRAM_BOT_TOKEN"], "getMe")
            self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], str(error.exception))

    def test_transport_redacts_network_timeout_and_http_exceptions(self):
        url = "https://api.telegram.org/bot" + self.env["TELEGRAM_BOT_TOKEN"] + "/getChat?chat_id=" + self.env["TELEGRAM_CHAT_ID"]
        for exception in [TimeoutError(url), urllib.error.URLError(url), urllib.error.HTTPError(url, 403, url, {}, None)]:
            opener, _ = self.transport(exception=exception)
            with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(self.ns["PreflightError"]) as error:
                self.ns["api_get"](self.env["TELEGRAM_BOT_TOKEN"], "getChat")
            self.assertNotIn(self.env["TELEGRAM_BOT_TOKEN"], str(error.exception))
            self.assertNotIn(self.env["TELEGRAM_CHAT_ID"], str(error.exception))

    def test_redirect_is_rejected_without_following(self):
        with self.assertRaises(self.ns["PreflightError"]):
            self.ns["NoRedirect"]().redirect_request(None, None, 302, "Found", {}, "https://untrusted.invalid")

    def test_workflow_static_isolation_and_configuration_parity(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        production = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
        self.assertEqual(text.split("on:\n", 1)[1].split("\npermissions:", 1)[0].strip(), "workflow_dispatch:")
        self.assertIn("permissions: {}", text)
        self.assertIn("timeout-minutes: 2", text)
        self.assertIn("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/master'", text)
        for forbidden in ["uses:", "environment:", "schedule:", "cron:", "pull_request", "run_github.py", "main.py", "monitor_runner", "Notifier", "GITHUB_TOKEN", "pip install"]:
            self.assertNotIn(forbidden, text)
        self.assertEqual(set(re.findall(r"secrets\.([A-Z_]+)", text)), {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"})
        for line in text.splitlines():
            if "${{ secrets." in line or "${{ vars." in line:
                self.assertIn(line.strip(), production)
        config = json.loads((ROOT / "config.json").read_bytes())
        self.assertEqual([config["notification_method"], config["telegram_send_files"], config["youtube_monitor_enabled"]], ["telegram", False, True])
        self.assertNotIn("environment:", production)
        imports = {node.names[0].name for node in ast.walk(ast.parse(inline_code())) if isinstance(node, ast.Import)}
        self.assertEqual(imports, {"json", "os", "re", "sys", "urllib.request"})
        self.assertFalse(any(isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".env") for node in ast.walk(ast.parse(inline_code()))))


if __name__ == "__main__":
    unittest.main()
