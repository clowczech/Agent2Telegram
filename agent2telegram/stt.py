"""Optional speech-to-text for Telegram voice messages.

Currently supports **ElevenLabs Scribe** (`scribe_v1`). It is enabled only when the user
provides their own API key (``elevenlabs_api_key`` in config or ``ELEVENLABS_API_KEY`` in
the environment) — there is no shared/default key and no third-party Python dependency:
the multipart upload is built by hand on top of ``urllib``.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("agent2telegram.stt")

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v1"
TRANSIENT_BACKOFFS = (1.0, 3.0)


class STTError(Exception):
    pass


def _describe_error(err: BaseException) -> str:
    if isinstance(err, urllib.error.HTTPError):
        msg = getattr(err, "reason", None) or getattr(err, "msg", "") or ""
        return f"HTTP {err.code}: {msg}".strip()
    return str(err) or err.__class__.__name__


def _is_retryable_http(err: urllib.error.HTTPError) -> bool:
    return 500 <= err.code <= 599


def _multipart(fields: dict[str, str], filename: str, audio: bytes,
               content_type: str = "audio/ogg") -> tuple[str, bytes]:
    boundary = "----a2t" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'.encode()
    )
    parts.append(audio)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    return boundary, b"".join(parts)


def transcribe_elevenlabs(audio: bytes, *, api_key: str, filename: str = "voice.ogg",
                          opener=None, timeout: float = 120,
                          retry_backoffs: tuple[float, ...] = TRANSIENT_BACKOFFS,
                          sleeper=time.sleep) -> str:
    """Transcribe *audio* bytes with ElevenLabs Scribe. Returns the recognized text."""
    if not api_key:
        raise STTError("no ElevenLabs API key configured")
    boundary, body = _multipart({"model_id": MODEL_ID}, filename, audio)
    req = urllib.request.Request(
        ELEVENLABS_URL,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    op = opener or urllib.request.build_opener()
    attempts = len(retry_backoffs) + 1
    for attempt in range(attempts):
        try:
            with op.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            retryable = _is_retryable_http(e)
            if not retryable or attempt == attempts - 1:
                detail = _describe_error(e)
                if retryable and attempts > 1:
                    raise STTError(
                        f"ElevenLabs request failed after {attempt + 1} attempts: {detail}"
                    ) from e
                raise STTError(f"ElevenLabs request failed: {detail}") from e
            detail = _describe_error(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            if attempt == attempts - 1:
                raise STTError(
                    f"ElevenLabs request failed after {attempt + 1} attempts: {_describe_error(e)}"
                ) from e
            detail = _describe_error(e)
        except Exception as e:
            raise STTError(f"ElevenLabs request failed: {_describe_error(e)}") from e

        backoff = retry_backoffs[attempt]
        # ElevenLabs STT occasionally drops long uploads; retry only transient failures.
        log.warning("ElevenLabs STT transient failure (%d/%d): %s; retrying in %.1fs",
                    attempt + 1, attempts, detail, backoff)
        sleeper(backoff)
    else:  # pragma: no cover - for type-checkers; the loop always returns or raises
        raise STTError("ElevenLabs request failed")
    text = (payload.get("text") or "").strip()
    if not text:
        raise STTError("transcription returned no text")
    return text


def transcribe(audio: bytes, *, api_key: str, filename: str = "voice.ogg") -> str:
    """Provider dispatcher (only ElevenLabs Scribe today)."""
    return transcribe_elevenlabs(audio, api_key=api_key, filename=filename)
