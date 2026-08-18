from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edge_tts_adapter  # noqa: E402
from voiceover import (  # noqa: E402
    CancelledError,
    PermanentProviderError,
    RawAudioResult,
    RetryableProviderError,
    SynthesisRequest,
)


def _request(
    *,
    token: object | None = None,
    voice: str = "zh-CN-YunjianNeural",
    timeout_seconds: object = 5.0,
) -> SynthesisRequest:
    return SynthesisRequest(
        text="这是一个不会访问网络的测试。",
        voice=voice,
        normalizedRate="+0%",
        normalizedPitch="+0Hz",
        normalizedVolume="+0%",
        providerContractVersion=edge_tts_adapter.EDGE_TTS_PROVIDER_CONTRACT_VERSION,
        timeoutSeconds=timeout_seconds,  # type: ignore[arg-type]
        cancellationToken=token,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _HttpError(Exception):
    def __init__(self, status: int, secret: str = "request-id-secret") -> None:
        super().__init__(secret)
        self.status = status


class _Token:
    def __init__(self) -> None:
        self.cancelled = False


class _TimedToken:
    def __init__(self, delay_seconds: float) -> None:
        self.deadline = time.monotonic() + delay_seconds

    @property
    def cancelled(self) -> bool:
        return time.monotonic() >= self.deadline


class EdgeTtsAdapterTests(unittest.TestCase):
    def test_sdk_module_and_signature_capabilities_are_cached_per_process(self) -> None:
        class FakeCommunicate:
            def __init__(self, text, voice, *, rate, pitch, volume, connect_timeout=10):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"audio"}

        fake_module = type("FakeEdgeTts", (), {"Communicate": FakeCommunicate})
        edge_tts_adapter._load_edge_tts.cache_clear()
        edge_tts_adapter._communicate_timeout_parameters.cache_clear()
        with mock.patch.object(
            edge_tts_adapter.importlib.metadata,
            "version",
            return_value=edge_tts_adapter.EDGE_TTS_PACKAGE_VERSION,
        ) as version, mock.patch.dict(sys.modules, {"edge_tts": fake_module}), mock.patch.object(
            edge_tts_adapter.inspect, "signature", wraps=edge_tts_adapter.inspect.signature
        ) as signature:
            self.assertIs(edge_tts_adapter._load_edge_tts(), fake_module)
            self.assertIs(edge_tts_adapter._load_edge_tts(), fake_module)
            self.assertEqual(asyncio.run(edge_tts_adapter._collect_edge_audio(_request())), b"audio")
            self.assertEqual(asyncio.run(edge_tts_adapter._collect_edge_audio(_request())), b"audio")
        self.assertEqual(version.call_count, 1)
        self.assertEqual(signature.call_count, 1)

    def test_sdk_timeout_is_positive_int_rounded_up_at_edge_boundary(self) -> None:
        captured: list[tuple[int, int]] = []

        class FakeCommunicate:
            def __init__(
                self,
                text: str,
                voice: str,
                *,
                rate: str,
                pitch: str,
                volume: str,
                connect_timeout: int = 10,
                receive_timeout: int = 60,
            ) -> None:
                captured.append((connect_timeout, receive_timeout))

            async def stream(self):
                yield {"type": "audio", "data": b"fake-mp3"}

        fake_edge_tts = type("FakeEdgeTts", (), {"Communicate": FakeCommunicate})

        for requested, expected in ((60.0, 60), (1.01, 2), (0.01, 1)):
            with self.subTest(requested=requested):
                captured.clear()
                with mock.patch.object(
                    edge_tts_adapter, "_load_edge_tts", return_value=fake_edge_tts
                ):
                    result = asyncio.run(
                        edge_tts_adapter._collect_edge_audio(
                            _request(timeout_seconds=requested)
                        )
                    )

                self.assertEqual(result, b"fake-mp3")
                self.assertEqual(captured, [(expected, expected)])
                self.assertIs(type(captured[0][0]), int)
                self.assertIs(type(captured[0][1]), int)

    def test_invalid_or_nonfinite_timeout_is_permanent_and_never_calls_provider(self) -> None:
        calls = 0

        def executor(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            return b"unexpected"

        adapter = edge_tts_adapter.EdgeTtsAdapter(
            request_executor=executor,
            queue_interval_seconds=0,
        )
        for timeout_seconds in (0, -0.1, float("nan"), float("inf"), True, "60"):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaises(PermanentProviderError):
                    adapter.synthesize(_request(timeout_seconds=timeout_seconds))

        self.assertEqual(calls, 0)

    def test_success_returns_raw_media_and_only_hashed_request_id(self) -> None:
        adapter = edge_tts_adapter.EdgeTtsAdapter(
            request_executor=lambda request: RawAudioResult(
                b"fake-mp3", "audio/mpeg", "provider-request-id-123"
            ),
            queue_interval_seconds=0,
        )

        result = adapter.synthesize(_request())

        self.assertEqual(result.bytes, b"fake-mp3")
        self.assertEqual(result.declaredFormat, "audio/mpeg")
        self.assertRegex(result.providerRequestId or "", r"^sha256:[0-9a-f]{16}$")
        self.assertNotIn("provider-request-id", result.providerRequestId or "")

    def test_timeout_retries_to_finite_limit_and_enforces_queue_interval(self) -> None:
        calls = 0
        clock = _Clock()

        def timeout(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            raise TimeoutError("secret URL must not escape")

        adapter = edge_tts_adapter.EdgeTtsAdapter(
            max_attempts=3,
            queue_interval_seconds=0.25,
            request_executor=timeout,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

        with self.assertRaisesRegex(RetryableProviderError, "3 次") as raised:
            adapter.synthesize(_request())

        self.assertEqual(calls, 3)
        self.assertGreaterEqual(sum(clock.sleeps), 0.5)
        self.assertNotIn("secret URL", str(raised.exception))

    def test_only_retryable_http_statuses_are_retried(self) -> None:
        calls = 0

        def transient(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _HttpError(503)
            return b"recovered"

        result = edge_tts_adapter.EdgeTtsAdapter(
            request_executor=transient,
            queue_interval_seconds=0,
        ).synthesize(_request())
        self.assertEqual(result.bytes, b"recovered")
        self.assertEqual(calls, 2)

        permanent_calls = 0

        def permanent(_request: SynthesisRequest) -> bytes:
            nonlocal permanent_calls
            permanent_calls += 1
            raise _HttpError(400)

        with self.assertRaises(PermanentProviderError):
            edge_tts_adapter.EdgeTtsAdapter(
                request_executor=permanent,
                queue_interval_seconds=0,
            ).synthesize(_request())
        self.assertEqual(permanent_calls, 1)

    def test_dns_and_connection_errors_retry_but_protocol_error_does_not(self) -> None:
        connection_calls = 0

        def connection(_request: SynthesisRequest) -> bytes:
            nonlocal connection_calls
            connection_calls += 1
            raise ConnectionResetError("connection reset")

        with self.assertRaises(RetryableProviderError):
            edge_tts_adapter.EdgeTtsAdapter(
                max_attempts=2,
                request_executor=connection,
                queue_interval_seconds=0,
            ).synthesize(_request())
        self.assertEqual(connection_calls, 2)

        protocol_calls = 0

        def protocol(_request: SynthesisRequest) -> bytes:
            nonlocal protocol_calls
            protocol_calls += 1
            raise ValueError("bad provider payload")

        with self.assertRaises(PermanentProviderError):
            edge_tts_adapter.EdgeTtsAdapter(
                request_executor=protocol,
                queue_interval_seconds=0,
            ).synthesize(_request())
        self.assertEqual(protocol_calls, 1)

    def test_invalid_voice_and_contract_never_call_provider(self) -> None:
        calls = 0

        def executor(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            return b"unexpected"

        adapter = edge_tts_adapter.EdgeTtsAdapter(
            request_executor=executor,
            queue_interval_seconds=0,
        )
        with self.assertRaises(PermanentProviderError):
            adapter.synthesize(_request(voice=""))
        invalid_contract = _request()
        invalid_contract = SynthesisRequest(
            text=invalid_contract.text,
            voice=invalid_contract.voice,
            normalizedRate=invalid_contract.normalizedRate,
            normalizedPitch=invalid_contract.normalizedPitch,
            normalizedVolume=invalid_contract.normalizedVolume,
            providerContractVersion="different-provider-contract",
            timeoutSeconds=invalid_contract.timeoutSeconds,
            cancellationToken=None,
        )
        with self.assertRaises(PermanentProviderError):
            adapter.synthesize(invalid_contract)
        self.assertEqual(calls, 0)

    def test_cancellation_before_and_between_attempts_is_not_retried(self) -> None:
        token = _Token()
        token.cancelled = True
        calls = 0

        def executor(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            return b"unexpected"

        with self.assertRaises(CancelledError):
            edge_tts_adapter.EdgeTtsAdapter(
                request_executor=executor,
                queue_interval_seconds=0,
            ).synthesize(_request(token=token))
        self.assertEqual(calls, 0)

        token.cancelled = False

        def cancel_after_failure(_request: SynthesisRequest) -> bytes:
            nonlocal calls
            calls += 1
            token.cancelled = True
            raise TimeoutError("transient")

        with self.assertRaises(CancelledError):
            edge_tts_adapter.EdgeTtsAdapter(
                max_attempts=3,
                request_executor=cancel_after_failure,
                queue_interval_seconds=0,
            ).synthesize(_request(token=token))
        self.assertEqual(calls, 1)

    def test_cancellation_interrupts_an_inflight_async_request(self) -> None:
        token = _TimedToken(0.02)

        async def blocked(_request: SynthesisRequest) -> bytes:
            await asyncio.sleep(2)
            return b"unexpected"

        adapter = edge_tts_adapter.EdgeTtsAdapter(
            max_attempts=3,
            queue_interval_seconds=0,
        )
        started = time.monotonic()
        with mock.patch.object(edge_tts_adapter, "_collect_edge_audio", blocked):
            with self.assertRaises(CancelledError):
                adapter.synthesize(_request(token=token))

        self.assertLess(time.monotonic() - started, 0.5)

    def test_shared_adapter_keeps_queue_interval_under_concurrent_callers(self) -> None:
        clock = _Clock()
        calls: list[str] = []
        adapter = edge_tts_adapter.EdgeTtsAdapter(
            request_executor=lambda request: calls.append(request.text) or b"audio",
            queue_interval_seconds=0.25,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(adapter.synthesize, [_request() for _ in range(4)]))
        self.assertEqual(len(results), 4)
        self.assertEqual(len(calls), 4)
        self.assertGreaterEqual(sum(clock.sleeps), 0.75)


if __name__ == "__main__":
    unittest.main()
