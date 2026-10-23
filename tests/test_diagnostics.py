"""Failure classification, credential redaction, and local-only diagnostics."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from chaoxing import cli, diagnostics, session
from chaoxing.common import read_json
from chaoxing.local import RequestStopped, local_runtime

UPLOAD = "https://mooc1-api.chaoxing.com/upload-ans/ueditorupload/attachment?enc2=PRIVATE"


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_transport_failures_keep_their_types_and_never_replay(self):
        for cls, kind, outcome in (
            (requests.ConnectTimeout, "connect_timeout", "not_sent"),
            (requests.ReadTimeout, "read_timeout", "unknown"),
            (requests.exceptions.ProxyError, "proxy_error", "unknown"),
            (requests.exceptions.SSLError, "tls_error", "unknown"),
            (requests.ConnectionError, "connection_error", "unknown"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "session.json"
                with (
                    local_runtime(path),
                    patch(
                        "requests.adapters.HTTPAdapter.send",
                        side_effect=cls("RAW_PRIVATE " + UPLOAD),
                    ) as send,
                ):
                    client = session.new_session()
                    client.trust_env = False
                    with self.assertRaises(cls) as caught:
                        client.post(UPLOAD, data={"answer": "PRIVATE_ANSWER"}, timeout=(30, 180))
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(caught.exception.diagnostic["kind"], kind)
                    self.assertEqual(caught.exception.diagnostic["remoteOutcome"], outcome)
                    self.assertEqual(caught.exception.diagnostic["operation"], "upload")
                    saved = read_json(str(path) + ".requests.json")
                    text = str(caught.exception) + json.dumps(saved)
                    self.assertNotIn("PRIVATE", text)
                    self.assertNotIn("enc2", text)
                    self.assertEqual(saved["last_network_error"]["timeoutSeconds"]["read"], 180)
                    self.assertNotIn("pause_until", saved)
                    client.close()

    def test_http_status_and_write_redirect_are_recorded_without_following(self):
        for status in (302, 413, 415, 502, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with local_runtime(Path(directory) / "session.json"):
                    client = session.new_session()
                    client.trust_env = False
                    response = requests.Response()
                    response.status_code = status
                    response.url = UPLOAD
                    response.request = requests.Request("POST", UPLOAD).prepare()
                    response._content = b"PRIVATE_RESPONSE"
                    response.headers["Location"] = "https://mooc1.chaoxing.com/private?enc=SECRET"
                    with patch("requests.adapters.HTTPAdapter.send", return_value=response) as send:
                        with self.assertRaises(requests.HTTPError) as caught:
                            client.post(UPLOAD)
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(caught.exception.diagnostic["httpStatus"], status)
                    self.assertEqual(caught.exception.diagnostic["remoteOutcome"], "unknown")
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    self.assertNotIn("SECRET", str(caught.exception))
                    client.close()

    def test_existing_verification_stop_is_not_reclassified_as_network_failure(self):
        with tempfile.TemporaryDirectory() as directory, local_runtime(Path(directory) / "s.json"):
            client = session.new_session()
            with patch("requests.Session.send", side_effect=RequestStopped("verification")) as send:
                with self.assertRaises(RequestStopped):
                    client.get("https://mooc1.chaoxing.com/")
            send.assert_called_once()
            self.assertNotIn("last_network_error", client.policy.state)
            client.close()

    def test_stop_before_request_records_that_no_write_was_sent(self):
        with session.new_session() as client:
            with patch.object(
                client.policy, "before_request", side_effect=RequestStopped("cooldown")
            ):
                with patch("requests.adapters.HTTPAdapter.send") as send:
                    with self.assertRaises(RequestStopped) as caught:
                        client.post(UPLOAD)
        self.assertFalse(caught.exception.request_sent)
        send.assert_not_called()

    def test_proxy_summary_redacts_authentication_and_selects_actual_target(self):
        proxies = {
            "https": "http://user:PRIVATE@127.0.0.1:7890/path?token=PRIVATE",
            "https://mooc1-api.chaoxing.com": "socks5://name:SECRET@127.0.0.1:9000",
        }
        result = diagnostics.proxy_summary(UPLOAD, proxies)
        self.assertEqual(result["port"], 9000)
        self.assertEqual(result["scheme"], "socks5")
        self.assertTrue(result["authenticated"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_network_info_reads_system_proxy_without_session_or_network(self):
        with (
            patch(
                "requests.sessions.get_environ_proxies",
                return_value={"https": "http://127.0.0.1:7890"},
            ),
            patch.object(cli, "local_runtime") as runtime,
            patch.object(session, "load_session") as load,
            patch("requests.adapters.HTTPAdapter.send") as send,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.main(["network-info", "--target", "upload"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["effectiveProxy"]["port"], 7890)
        runtime.assert_not_called()
        load.assert_not_called()
        send.assert_not_called()

    def test_bare_stream_exception_never_exposes_signed_url(self):
        error = requests.ConnectionError("download https://d0.cldisk.com/file?token=PRIVATE")
        self.assertNotIn("PRIVATE", diagnostics.safe_error_text(error))

    def test_server_message_redacts_quoted_credentials_and_preserves_human_reason(self):
        for message in (
            '{"Cookie":"PRIVATE"}',
            "Authorization: Bearer PRIVATE",
            '{"enc":"PRIVATE"}',
            "查看 https://example.test/?token=PRIVATE",
        ):
            with self.subTest(message=message):
                self.assertNotIn("PRIVATE", diagnostics.safe_server_message(message))
        self.assertEqual(diagnostics.safe_server_message("附件超过大小限制"), "附件超过大小限制")

    def test_interrupt_during_post_preserves_uncertain_operation(self):
        with (
            session.new_session() as client,
            patch("requests.adapters.HTTPAdapter.send", side_effect=KeyboardInterrupt) as send,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                client.post(UPLOAD)
        self.assertEqual(caught.exception.diagnostic["kind"], "interrupted")
        self.assertEqual(caught.exception.diagnostic["remoteOutcome"], "unknown")
        send.assert_called_once()

    def test_persistence_failure_does_not_replace_transport_failure(self):
        policy = type("Policy", (), {"state": {}, "persist": lambda self: None})()
        with patch.object(policy, "persist", side_effect=PermissionError("PRIVATE")):
            error = diagnostics.annotate_error(requests.ReadTimeout("PRIVATE"), {}, policy)
        self.assertIsInstance(error, requests.ReadTimeout)
        self.assertNotIn("PRIVATE", str(error))


if __name__ == "__main__":
    unittest.main()
