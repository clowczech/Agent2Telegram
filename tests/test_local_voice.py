"""Tests for the local (no-cloud) STT/TTS backends of the fork."""
import shutil
import sys
import unittest
import wave
import tempfile
import os

from agent2telegram import stt, tts


def _silence_wav_bytes(seconds=0.2, rate=16000):
    import io, struct
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class LocalSttTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
    def test_command_stdout_is_the_transcript(self):
        out = stt.transcribe_local(_silence_wav_bytes(),
                                   [sys.executable, "-c", "print('prepsany text')"],
                                   filename="t.wav")
        self.assertEqual(out, "prepsany text")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
    def test_input_placeholder_is_replaced_with_a_wav(self):
        out = stt.transcribe_local(
            _silence_wav_bytes(),
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "{input}"],
            filename="t.wav")
        self.assertTrue(out.endswith(".wav"), out)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
    def test_empty_transcript_raises(self):
        with self.assertRaises(Exception):
            stt.transcribe_local(_silence_wav_bytes(),
                                 [sys.executable, "-c", "print('')"], filename="t.wav")

    def test_missing_ffmpeg_raises_cleanly(self):
        import unittest.mock as mock
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(Exception):
                stt.transcribe_local(b"xx", ["true"], filename="t.ogg")


class LocalTtsTests(unittest.TestCase):
    def test_output_placeholder_and_stdin_text(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "reply.wav")
            # "TTS": zapíše text ze stdin do výstupního souboru
            tts.synthesize_local_wav(
                "ahoj", [sys.executable, "-c",
                         "import sys; open(sys.argv[1],'w').write(sys.stdin.read())",
                         "{output}"], out)
            self.assertEqual(open(out).read(), "ahoj")

    def test_failure_raises(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "reply.wav")
            with self.assertRaises(RuntimeError):
                tts.synthesize_local_wav("x", [sys.executable, "-c", "raise SystemExit(3)"], out)
            with self.assertRaises(RuntimeError):   # exit 0, ale nic nezapsal
                tts.synthesize_local_wav("x", [sys.executable, "-c", "pass"], out)


if __name__ == "__main__":
    unittest.main()
