from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from scripts.minimax_adapter import MINIMAX_PROVIDER_CONTRACT_VERSION, MiniMaxAdapter
from scripts.voiceover import PermanentProviderError, RetryableProviderError, SynthesisRequest, build_voice_plan


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class _Clock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.value

    def sleep(self, seconds):
        with self.lock:
            self.value += seconds


def request(text="霓虹映在雨后的街道上。", rate="+10%", pitch="+0Hz", volume="+10%"):
    return SynthesisRequest(text, "male-qn-jingying", rate, pitch, volume, MINIMAX_PROVIDER_CONTRACT_VERSION, 5, None)


class MiniMaxAdapterTests(unittest.TestCase):
    def test_builds_v2_request_and_decodes_hex(self):
        seen = {}

        def opener(req, timeout):
            seen["headers"] = dict(req.header_items())
            seen["timeout"] = timeout
            seen["payload"] = json.loads(req.data.decode("utf-8"))
            return _Response({"data": {"audio": "4944"}, "trace_id": "secret-trace", "base_resp": {"status_code": 0, "status_msg": "success"}})

        result = MiniMaxAdapter(api_key="top-secret", opener=opener, queue_interval_seconds=0).synthesize(request())
        self.assertEqual(result.bytes, b"ID")
        self.assertEqual(result.declaredFormat, "audio/mpeg")
        self.assertEqual(result.providerRequestId, "sha256:d648804e970a6f69")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer top-secret")
        self.assertEqual(seen["payload"]["voice_setting"]["speed"], 1.1)
        self.assertEqual(seen["payload"]["voice_setting"]["vol"], 1.1)
        self.assertEqual(seen["payload"]["voice_setting"]["pitch"], 0)
        self.assertEqual(seen["payload"]["audio_setting"]["sample_rate"], 32000)
        self.assertEqual(seen["payload"]["output_format"], "hex")

    def test_provider_error_does_not_echo_key(self):
        def opener(_req, **_kwargs):
            return _Response({"base_resp": {"status_code": 1004, "status_msg": "invalid voice"}})

        with self.assertRaises(PermanentProviderError) as raised:
            MiniMaxAdapter(api_key="top-secret", opener=opener, queue_interval_seconds=0).synthesize(request())
        self.assertNotIn("top-secret", str(raised.exception))

    def test_429_is_bounded_and_retryable(self):
        calls = []

        def opener(_req, **_kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("https://api.minimaxi.com/v1/t2a_v2", 429, "busy", {}, None)

        with self.assertRaises(RetryableProviderError):
            MiniMaxAdapter(api_key="top-secret", opener=opener, max_attempts=2, queue_interval_seconds=0).synthesize(request())
        self.assertEqual(len(calls), 2)

    def test_connection_failure_is_retryable_but_bad_pitch_is_permanent(self):
        def opener(_req, **_kwargs):
            raise socket.timeout()

        with self.assertRaises(RetryableProviderError):
            MiniMaxAdapter(api_key="top-secret", opener=opener, max_attempts=1, queue_interval_seconds=0).synthesize(request())
        with self.assertRaisesRegex(PermanentProviderError, "pitch"):
            MiniMaxAdapter(api_key="top-secret", opener=lambda *_args, **_kwargs: None, queue_interval_seconds=0).synthesize(request(pitch="+13Hz"))

    def test_shared_rpm_scheduler_serializes_concurrent_worker_starts(self):
        clock = _Clock()
        starts = []
        starts_lock = threading.Lock()

        def opener(_req, **_kwargs):
            with starts_lock:
                starts.append(clock.monotonic())
            return _Response({"data": {"audio": "4944"}, "base_resp": {"status_code": 0}})

        adapter = MiniMaxAdapter(
            api_key="top-secret",
            opener=opener,
            queue_interval_seconds=0,
            requests_per_minute=20,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: adapter.synthesize(request()), range(3)))
        self.assertEqual([result.bytes for result in results], [b"ID", b"ID", b"ID"])
        self.assertEqual(starts, [0.0, 3.0, 6.0])

    def test_business_rpm_error_is_bounded_retryable_and_applies_shared_backoff(self):
        clock = _Clock()
        starts = []

        def opener(_req, **_kwargs):
            starts.append(clock.monotonic())
            if len(starts) == 1:
                return _Response({"base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"}})
            return _Response({"data": {"audio": "4944"}, "base_resp": {"status_code": 0}})

        result = MiniMaxAdapter(
            api_key="top-secret",
            opener=opener,
            max_attempts=2,
            queue_interval_seconds=0,
            rate_limit_backoff_seconds=7,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ).synthesize(request())
        self.assertEqual(result.bytes, b"ID")
        self.assertEqual(starts, [0.0, 7.0])


class MiniMaxPlanTests(unittest.TestCase):
    def test_minimax_voice_plan_is_a_first_class_audio_mode(self):
        plan = build_voice_plan(
            project_id="p1",
            source_srt_sha256="a" * 64,
            cues=[{"sourceOrdinal": 1, "startMs": 0, "endMs": 1000, "text": "测试文本。"}],
            provider_id="minimax",
            protocol="MiniMax",
            voice="male-qn-jingying",
            provider_contract_version=MINIMAX_PROVIDER_CONTRACT_VERSION,
            provider_options={"model": "speech-2.8-hd", "emotion": "calm", "textNormalization": True},
        )
        self.assertEqual(plan["mode"], "minimax")
        self.assertEqual(plan["provider"]["protocol"], "MiniMax")
        self.assertEqual(plan["provider"]["options"]["model"], "speech-2.8-hd")


if __name__ == "__main__":
    unittest.main()
