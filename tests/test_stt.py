"""Tests for the ElevenLabs Scribe speech-to-text integration — no network."""
import io
import json
import urllib.error
import unittest

from agent2telegram import stt


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.last_request = None

    def open(self, req, timeout=None):
        self.last_request = req
        return _Resp(json.dumps(self.payload).encode())


class _SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.timeouts = []

    def open(self, req, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(json.dumps(outcome).encode())


class STTTests(unittest.TestCase):
    def test_transcribes_text(self):
        op = _FakeOpener({"text": "hello world"})
        out = stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)
        self.assertEqual(out, "hello world")

    def test_uses_scribe_model_in_multipart(self):
        op = _FakeOpener({"text": "x"})
        stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)
        body = op.last_request.data
        self.assertIn(b"scribe_v1", body)
        self.assertIn(b'name="file"', body)
        self.assertIn(b"audio", body)

    def test_sends_api_key_header(self):
        op = _FakeOpener({"text": "x"})
        stt.transcribe_elevenlabs(b"audio", api_key="secret-key", opener=op)
        # urllib normalizes header keys to title-case
        self.assertEqual(op.last_request.headers.get("Xi-api-key"), "secret-key")

    def test_missing_key_raises(self):
        with self.assertRaises(stt.STTError):
            stt.transcribe_elevenlabs(b"audio", api_key="")

    def test_empty_transcription_raises(self):
        op = _FakeOpener({"text": ""})
        with self.assertRaises(stt.STTError):
            stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)

    def test_retries_timeout_then_succeeds(self):
        op = _SequenceOpener([TimeoutError("timed out"), {"text": "after retry"}])
        sleeps = []

        out = stt.transcribe_elevenlabs(
            b"audio",
            api_key="k",
            opener=op,
            retry_backoffs=(1.0, 3.0),
            sleeper=sleeps.append,
        )

        self.assertEqual(out, "after retry")
        self.assertEqual(op.calls, 2)
        self.assertEqual(op.timeouts, [120, 120])
        self.assertEqual(sleeps, [1.0])

    def test_unauthorized_is_not_retried(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "should not happen"}])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(1.0, 3.0),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_bad_request_is_not_retried(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 400, "Bad Request", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "should not happen"}])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(1.0, 3.0),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_retries_5xx_then_succeeds(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 502, "Bad Gateway", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "after 5xx retry"}])
        sleeps = []

        out = stt.transcribe_elevenlabs(
            b"audio",
            api_key="k",
            opener=op,
            retry_backoffs=(0.1, 0.2),
            sleeper=sleeps.append,
        )

        self.assertEqual(out, "after 5xx retry")
        self.assertEqual(op.calls, 2)
        self.assertEqual(sleeps, [0.1])

    def test_exhausted_5xx_retries_reports_attempt_count(self):
        err1 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        err2 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        err3 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err1, err2, err3])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(0.1, 0.2),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 3)
        self.assertEqual(sleeps, [0.1, 0.2])
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertIn("HTTP 503", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
