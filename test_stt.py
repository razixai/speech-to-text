"""Pytest suite for stt.pyw.

All external services (Gemini, Groq, ElevenLabs), Windows audio/COM/hotkey/tray
APIs, clipboard, and filesystem state are isolated or mocked so tests run fast
and safely offline.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import logging
import os
import site
import sys
import tempfile
import threading
import time
import types
import urllib.error
import wave
from pathlib import Path
from unittest import mock

import pytest


def _ensure_venv_site_packages(project_dir: Path) -> None:
    """Expose .venv packages when pytest runs from the global Python install."""
    venv_site = project_dir / ".venv" / "Lib" / "site-packages"
    if venv_site.is_dir() and str(venv_site) not in sys.path:
        site.addsitedir(str(venv_site))
        # Prefer the project's pinned dependency versions over global packages.
        if str(venv_site) in sys.path:
            sys.path.remove(str(venv_site))
        sys.path.insert(0, str(venv_site))


def _load_stt_module():
    """Load stt.pyw in an isolated AppData/environment sandbox."""
    project_dir = Path(__file__).resolve().parent
    _ensure_venv_site_packages(project_dir)

    stt_path = project_dir / "stt.pyw"
    sandbox_appdata = tempfile.TemporaryDirectory(prefix="stt_test_import_")
    saved_env = os.environ.copy()
    saved_prefix = sys.prefix

    try:
        os.environ["LOCALAPPDATA"] = sandbox_appdata.name
        for key in (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "ELEVENLABS_API_KEY",
            "GEMINI_DICTATE_DRY_RUN",
        ):
            os.environ.pop(key, None)

        # Prevent _relaunch_with_project_venv() from spawning pythonw.exe under global pytest.
        if sys.prefix == sys.base_prefix:
            sys.prefix = str(project_dir / ".venv")

        with mock.patch("dotenv.dotenv_values", return_value={}):
            loader = importlib.machinery.SourceFileLoader("stt", str(stt_path))
            spec = importlib.util.spec_from_loader("stt", loader)
            assert spec is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules["stt"] = module
            loader.exec_module(module)
        import dotenv

        module.dotenv_values = dotenv.dotenv_values
    finally:
        sys.prefix = saved_prefix
        os.environ.clear()
        os.environ.update(saved_env)
        sandbox_appdata.cleanup()

    module.API_KEY = ""
    module.GROQ_API_KEY = ""
    module.ELEVENLABS_API_KEY = ""
    module.DRY_RUN = False
    module.HOTKEY = None
    module.PREFERRED_MODEL = module.DEFAULT_GEMINI_MODEL
    module.MIN_SECONDS = 0.3
    module.INPUT_DEVICE = module.DEFAULT_MICROPHONE
    module.SETTINGS = {
        "model": module.DEFAULT_GEMINI_MODEL,
        "min_seconds": 0.3,
        "recording_hotkey": None,
        "input_device": module.DEFAULT_MICROPHONE,
    }
    module.VOCABULARY = {}
    return module


stt = _load_stt_module()
np = stt.np


def _make_wav(
    samples: np.ndarray, samplerate: int = 16000, channels: int = 1
) -> bytes:
    """Encode an int16 NumPy array into in-memory WAV bytes."""
    pcm = np.asarray(samples, dtype=np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _read_wav(wav_bytes: bytes) -> tuple[int, int, np.ndarray]:
    """Decode WAV bytes into (samplerate, channels, int16_samples)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return rate, channels, samples


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Redirect persisted paths and in-memory config globals into tmp_path."""
    log_dir = tmp_path / "STT"
    legacy_dir = tmp_path / "GeminiDictate"
    transcript_dir = log_dir / "transcripts"
    settings_file = log_dir / "settings.json"
    vocab_file = log_dir / "vocabulary.json"
    stats_file = log_dir / "stats.jsonl"

    monkeypatch.setattr(stt, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(stt, "LEGACY_DATA_DIR", str(legacy_dir))
    monkeypatch.setattr(stt, "TRANSCRIPT_DIR", str(transcript_dir))
    monkeypatch.setattr(stt, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(stt, "VOCABULARY_FILE", str(vocab_file))
    monkeypatch.setattr(stt, "STATS_FILE", str(stats_file))
    monkeypatch.setattr(stt, "SETTINGS", {})
    monkeypatch.setattr(stt, "VOCABULARY", {})
    monkeypatch.setattr(stt, "HOTKEY_SETTING_MIGRATED", False)

    return {
        "log_dir": log_dir,
        "legacy_dir": legacy_dir,
        "transcript_dir": transcript_dir,
        "settings_file": settings_file,
        "vocab_file": vocab_file,
        "stats_file": stats_file,
    }


# ---------------------------------------------------------------------------
# Config, formatting, and hotkeys
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (stt.GROQ_MODEL, "Groq - Whisper Large V3 Turbo"),
        (stt.ELEVENLABS_MODEL, "ElevenLabs + Gemini (English polish)"),
        (stt.DEFAULT_GEMINI_MODEL, stt.DEFAULT_GEMINI_MODEL),
    ],
)
def test_model_label(model: str, expected: str) -> None:
    assert stt.model_label(model) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("left shift", "shift"),
        ("control", "ctrl"),
        ("right windows", "win"),
        ("space", "space"),
    ],
)
def test_canonical_hotkey_key(key: str, expected: str) -> None:
    assert stt.canonical_hotkey_key(key) == expected


def test_validate_recording_hotkey_accepts_valid_combo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stt.keyboard, "parse_hotkey", lambda _hk: (((29,), (42,), (30,)),)
    )
    assert stt.validate_recording_hotkey(" Ctrl + Shift + A ") == "ctrl + shift + a"
    assert stt.validate_recording_hotkey("Ctrl+Shift+A") == "ctrl+shift+a"


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("", None),
        ("   ", None),
        (None, None),
        (123, None),
        ("ctrl+invalidkey", ValueError("bad")),
        ("ctrl+a, b", (((29,),), ((30,),))),
        ("ctrl+unknown", (((29,), ()),)),
        ("a", (((30,),),)),
        ("ctrl+shift", (((29,), (42,)),)),
        ("shift+backslash+z", (((42,), (43,), (44,)),)),
    ],
)
def test_validate_recording_hotkey_rejects_invalid_inputs(
    monkeypatch: pytest.MonkeyPatch, value: object, parsed: object
) -> None:
    def fake_parse(_hotkey: str):
        if isinstance(parsed, Exception):
            raise parsed
        return parsed

    monkeypatch.setattr(stt.keyboard, "parse_hotkey", fake_parse)
    with pytest.raises(ValueError):
        stt.validate_recording_hotkey(value)


@pytest.mark.parametrize(
    ("hotkey", "expected"),
    [
        ("left shift+backslash", True),
        ("shift+z", True),
        ("ctrl+shift+space", False),
        (None, False),
    ],
)
def test_hotkey_overlaps_learn_chord(hotkey: str | None, expected: bool) -> None:
    assert stt._hotkey_overlaps_learn_chord(hotkey) is expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (42.9, "42 sec"),
        (125, "2 min 5 sec"),
        (3665, "1h 1m"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert stt._format_duration(seconds) == expected


# ---------------------------------------------------------------------------
# Persistence, vocabulary, stats, backups, and migration
# ---------------------------------------------------------------------------
def test_atomic_write_and_settings_roundtrip(isolated_env: dict[str, Path]) -> None:
    log_dir = isolated_env["log_dir"]
    target = log_dir / "sample.txt"
    stt._atomic_write_text(str(target), "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert [p.name for p in log_dir.iterdir()] == ["sample.txt"]

    defaults = stt.load_settings()
    assert defaults["model"] == stt.PREFERRED_MODEL
    assert defaults["recording_hotkey"] is None

    stt.SETTINGS.update({"model": stt.GROQ_MODEL, "min_seconds": 0.5})
    assert stt.save_settings() is True
    migrated = stt.load_settings()
    assert migrated["model"] == stt.GROQ_MODEL
    assert migrated["recording_hotkey"] == stt.LEGACY_RECORDING_HOTKEY
    assert stt.HOTKEY_SETTING_MIGRATED is True


def test_vocabulary_and_build_system_prompt(isolated_env: dict[str, Path]) -> None:
    assert stt.build_system_prompt() == stt.SYSTEM_PROMPT

    stt.VOCABULARY.update({"  Foo  ": "Bar", "baz": "Qux"})
    stt.save_vocabulary()
    isolated_env["vocab_file"].write_text(
        json.dumps({"  Alpha ": "Omega", "": "ignored", "x": "  "}),
        encoding="utf-8",
    )
    loaded = stt.load_vocabulary()
    assert loaded == {"alpha": "Omega"}

    stt.VOCABULARY.clear()
    stt.VOCABULARY.update(loaded)
    prompt = stt.build_system_prompt()
    assert "User vocabulary corrections:" in prompt
    assert '"alpha"' in prompt
    assert '"Omega"' in prompt


def test_stats_record_load_and_compute(isolated_env: dict[str, Path]) -> None:
    stats_file = isolated_env["stats_file"]
    assert stt.load_stats() == []
    empty = stt.compute_stats([])
    assert empty["total_words"] == 0
    assert empty["current_streak"] == 0

    stt.record_stats(30, 15.0, stt.DEFAULT_GEMINI_MODEL)
    stats_file.write_text(
        stats_file.read_text(encoding="utf-8") + "not-json\n",
        encoding="utf-8",
    )
    entries = stt.load_stats()
    assert len(entries) == 1
    assert entries[0]["wpm"] == 120

    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    two_days_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 172800))
    computed = stt.compute_stats(
        [
            {"ts": f"{two_days_ago}T09:00:00", "words": 20, "seconds": 10.0},
            {"ts": f"{yesterday}T10:00:00", "words": 40, "seconds": 20.0},
            {"ts": f"{today}T11:00:00", "words": 60, "seconds": 30.0},
        ]
    )
    assert computed["total_words"] == 120
    assert computed["total_transcriptions"] == 3
    assert computed["avg_wpm"] == 120
    assert computed["current_streak"] == 3
    assert computed["today_words"] == 60
    assert computed["today_transcriptions"] == 1


def test_archive_recording_prunes_old_backups(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = _make_wav(np.full(1600, 1000, dtype=np.int16))
    monkeypatch.setattr(stt, "MAX_TRANSCRIPT_BACKUPS", 2)

    paths: list[str] = []
    for idx in range(3):
        monkeypatch.setattr(stt.time, "time_ns", lambda i=idx: 1000 + i)
        p = stt.archive_recording(wav)
        assert p is not None
        os.utime(p, (1000 + idx, 1000 + idx))
        stt.save_transcript_backup(p, f"text {idx}")
        paths.append(p)

    remaining_wavs = sorted(
        p.name for p in isolated_env["transcript_dir"].iterdir() if p.suffix == ".wav"
    )
    assert remaining_wavs == ["1001.wav", "1002.wav"]
    assert not Path(paths[0]).with_suffix(".txt").exists()
    assert [Path(p).name for p in stt.recent_recordings(limit=2)] == [
        "1002.wav",
        "1001.wav",
    ]


def test_migrate_legacy_data_copies_missing_files_only(
    isolated_env: dict[str, Path],
) -> None:
    legacy_dir = isolated_env["legacy_dir"]
    log_dir = isolated_env["log_dir"]
    transcript_dir = isolated_env["transcript_dir"]
    settings_file = isolated_env["settings_file"]

    (legacy_dir / "transcripts").mkdir(parents=True)
    (legacy_dir / "settings.json").write_text('{"model": "old"}', encoding="utf-8")
    (legacy_dir / "transcripts" / "a.wav").write_bytes(b"RIFF")
    (legacy_dir / "transcripts" / "a.txt").write_text("hi", encoding="utf-8")
    (legacy_dir / "transcripts" / "ignore.log").write_text("x", encoding="utf-8")

    log_dir.mkdir(parents=True)
    settings_file.write_text('{"model": "keep"}', encoding="utf-8")

    stt.migrate_legacy_data()

    assert settings_file.read_text(encoding="utf-8") == '{"model": "keep"}'
    assert (transcript_dir / "a.wav").is_file()
    assert (transcript_dir / "a.txt").is_file()
    assert not (transcript_dir / "ignore.log").exists()


# ---------------------------------------------------------------------------
# Audio DSP and device matching
# ---------------------------------------------------------------------------
def test_downsample_int16_mono() -> None:
    src = np.full(4800, 1000, dtype=np.int16)
    down_int = stt._downsample_int16_mono(src, 48000, 16000)
    assert down_int.dtype == np.int16
    assert len(down_int) == 1600
    assert np.all(down_int == 1000)

    src_44 = np.full(4410, 500, dtype=np.int16)
    down_interp = stt._downsample_int16_mono(src_44, 44100, 16000)
    assert down_interp.dtype == np.int16
    assert len(down_interp) == 1600

    unchanged = stt._downsample_int16_mono(src, 16000, 16000)
    assert unchanged is src


def test_audio_has_signal(caplog: pytest.LogCaptureFixture) -> None:
    silent_wav = _make_wav(np.zeros(8000, dtype=np.int16))
    assert stt.audio_has_signal(silent_wav) is False

    # Short speech burst inside a mostly silent clip should still pass 100ms window check.
    mixed = np.zeros(16000, dtype=np.int16)
    mixed[4000:5600] = 2000
    assert stt.audio_has_signal(_make_wav(mixed)) is True

    with caplog.at_level(logging.ERROR, logger=stt.log.name):
        assert stt.audio_has_signal(b"not-a-wav") is False
    assert "Could not inspect WAV signal" in caplog.text


def test_trim_silence_and_prepare_batch_wav() -> None:
    samples = np.zeros(48000, dtype=np.int16)  # 1s @ 48 kHz
    samples[19200:28800] = 2500  # 0.2s active tone in the middle
    wav_48k = _make_wav(samples, samplerate=48000)

    trimmed = stt.trim_silence(wav_48k)
    rate_t, ch_t, pcm_t = _read_wav(trimmed)
    assert (rate_t, ch_t) == (48000, 1)
    assert len(pcm_t) < len(samples)

    prepared = stt.prepare_batch_wav(wav_48k)
    rate_p, ch_p, pcm_p = _read_wav(prepared)
    assert (rate_p, ch_p) == (16000, 1)
    assert len(pcm_p) > 0


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            "2- Yeti Stereo Microphone (Windows WASAPI)",
            "Yeti Stereo Microphone",
            True,
        ),
        (
            "Headset (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0..;(AirPods Pro 2))",
            "Headphones (AirPods Pro2)",
            True,
        ),
        ("Blue Yeti", "Realtek Mic", False),
    ],
)
def test_same_microphone_name(first: str, second: str, expected: bool) -> None:
    assert stt._same_microphone_name(first, second) is expected


def test_preferred_input_route_and_list_input_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [
        {
            "index": 0,
            "key": "MME | Microsoft Sound Mapper - Input",
            "name": "Microsoft Sound Mapper - Input",
            "hostapi": "MME",
            "label": "Microsoft Sound Mapper - Input (MME)",
            "max_input_channels": 2,
            "default_samplerate": 44100,
        },
        {
            "index": 1,
            "key": "MME | Mic Array",
            "name": "Mic Array",
            "hostapi": "MME",
            "label": "Mic Array (MME)",
            "max_input_channels": 1,
            "default_samplerate": 44100,
        },
        {
            "index": 2,
            "key": "Windows WASAPI | Mic Array",
            "name": "Mic Array",
            "hostapi": "Windows WASAPI",
            "label": "Mic Array (Windows WASAPI)",
            "max_input_channels": 2,
            "default_samplerate": 48000,
        },
    ]
    assert stt._preferred_input_route(raw[1:])["index"] == 2

    monkeypatch.setattr(stt, "_enumerate_input_devices", lambda: raw)
    monkeypatch.setattr(stt, "_windows_capture_device_names", lambda: ["Mic Array"])
    devices = stt.list_input_devices()
    assert len(devices) == 1
    assert devices[0]["index"] == 2
    assert devices[0]["label"] == "Mic Array"


# ---------------------------------------------------------------------------
# Output cleaning and error mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  No speech detected. ", ""),
        ("[no speech]", ""),
        ("  Hello world.  ", "Hello world."),
    ],
)
def test_clean_model_output(raw: str, expected: str) -> None:
    assert stt.clean_model_output(raw) == expected


@pytest.mark.parametrize(
    ("exc", "expected_substring"),
    [
        (stt.MicrophoneUnavailableError("No mic"), "No mic"),
        (stt.ElevenLabsBatchError(401, "unauthorized"), "API key"),
        (stt.ElevenLabsBatchError(402, "no credits"), "credits"),
        (stt.ElevenLabsBatchError(429, "rate limit"), "retried"),
        (stt.ElevenLabsBatchError(503, "down"), "temporarily unavailable"),
        (RuntimeError("429 resource_exhausted"), "API quota reached. Try again later."),
        (RuntimeError("Network DNS failure"), "Network error. Check your connection."),
    ],
)
def test_user_facing_error_mappings(exc: Exception, expected_substring: str) -> None:
    assert expected_substring in stt.user_facing_error(exc)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------
def test_recorder_callback_downmix_sink_and_stop() -> None:
    rec = stt.Recorder(samplerate=16000)
    received: list[tuple[bytes, int]] = []
    assert rec.set_audio_sink(lambda pcm, rate: received.append((pcm, rate))) is True
    rec._recording = True
    rec._stream_samplerate = 16000
    rec._active_samplerate = 16000
    rec._buffer_samplerate = 16000

    # Stereo frame where channel 1 has speech and channel 2 is dead/silent.
    stereo = np.zeros((4800, 2), dtype=np.int16)
    stereo[:, 0] = 3000
    rec._callback(stereo, 4800, None, None)

    assert len(received) == 1
    assert rec.volume_level() > 0.0
    assert rec.set_input_device("other") is False

    wav, seconds = rec.stop()
    assert wav is not None
    assert seconds == pytest.approx(0.3, abs=0.01)
    rate, ch, samples = _read_wav(wav)
    assert (rate, ch) == (16000, 1)
    assert np.all(samples == 3000)


def test_recorder_stop_discards_short_recording_and_abort_clears() -> None:
    rec = stt.Recorder(samplerate=16000)
    rec._recording = True
    rec._stream_samplerate = 16000
    rec._active_samplerate = 16000
    rec._buffer_samplerate = 16000
    rec._callback(np.full(1600, 1000, dtype=np.int16), 1600, None, None)
    wav, seconds = rec.stop()
    assert wav is None
    assert seconds == pytest.approx(0.1, abs=0.01)

    rec._recording = True
    rec._callback(np.full(8000, 1000, dtype=np.int16), 8000, None, None)
    rec.abort()
    assert rec._frames == []
    assert rec.volume_level() == 0.0


def test_recorder_buffer_limit_error() -> None:
    rec = stt.Recorder(samplerate=16000)
    rec._recording = True
    rec._buffer_limit_reached = True
    with pytest.raises(stt.RecordingBufferLimitError):
        rec.stop()


def test_recorder_resolve_input_device() -> None:
    devices = [
        {
            "index": 3,
            "key": "Windows WASAPI | Rode NT-USB",
            "name": "Rode NT-USB",
            "hostapi": "Windows WASAPI",
            "label": "Rode NT-USB (Windows WASAPI)",
            "max_input_channels": 1,
            "default_samplerate": 48000,
        }
    ]
    rec = stt.Recorder(input_device=stt.DEFAULT_MICROPHONE)
    assert rec._resolve_input_device(devices=devices, default_name="Rode NT-USB") == 3

    rec_missing = stt.Recorder(input_device="Windows WASAPI | Missing Mic")
    with pytest.raises(stt.MicrophoneUnavailableError):
        rec_missing._resolve_input_device(devices=devices)


# ---------------------------------------------------------------------------
# Live streaming and polishing
# ---------------------------------------------------------------------------
def test_live_gemini_polisher_reuses_exact_and_fuzzy_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_polish(text: str) -> str:
        calls.append(text)
        return f"Polished: {text}"

    monkeypatch.setattr(stt, "LIVE_GEMINI_POLISH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(stt, "LIVE_GEMINI_POLISH_MIN_INTERVAL_SECONDS", 0.01)

    polisher = stt.LiveGeminiPolisher(fake_polish)
    try:
        polisher.submit("hello world this is a test")

        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["hello world this is a test"]

        # Give worker a moment to store cached result before polish_final.
        time.sleep(0.03)
        result = polisher.polish_final("Hello   world this is a test!")
        assert result == "Polished: hello world this is a test"
    finally:
        polisher.cancel()


def test_live_scribe_session_streams_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_messages: list[dict] = []
    incoming = [
        json.dumps({"message_type": "partial_transcript", "text": "hello"}),
        json.dumps({"message_type": "committed_transcript", "text": "hello world"}),
    ]

    class FakeConnection:
        sock = None

        def settimeout(self, _timeout: float) -> None:
            pass

        def send(self, data: str) -> None:
            sent_messages.append(json.loads(data))

        def recv(self) -> str:
            if incoming:
                return incoming.pop(0)
            raise fake_ws.WebSocketTimeoutException()

        def close(self) -> None:
            pass

    fake_ws = types.SimpleNamespace(
        WebSocketTimeoutException=type("WebSocketTimeoutException", (Exception,), {}),
        create_connection=lambda *_args, **_kwargs: FakeConnection(),
    )

    monkeypatch.setitem(sys.modules, "websocket", fake_ws)
    session = stt.LiveScribeSession("el-key")
    try:
        pcm = b"\x01\x00" * 1600  # 100 ms @ 16 kHz
        assert session.push_audio(pcm, 16000, block=True) is True
        transcript = session.finish()
    finally:
        session.cancel()

    assert transcript == "hello world"
    assert any(msg.get("commit") is True for msg in sent_messages)


def test_live_scribe_session_rejects_invalid_audio_and_errors() -> None:
    session = stt.LiveScribeSession("el-key")
    try:
        assert session.push_audio(b"\x00\x01", 12345) is False
        assert session._failed.is_set()

        session._failed.clear()
        session._handle_message(json.dumps({"message_type": "auth_error"}))
        assert session._failed.is_set()
    finally:
        session.cancel()


# ---------------------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------------------
def test_build_elevenlabs_batch_request_and_error_helpers() -> None:
    body, content_type = stt.Transcriber._build_elevenlabs_batch_request(b"WAVDATA")
    assert "multipart/form-data; boundary=" in content_type
    assert b'name="model_id"' in body
    assert b"scribe_v2" in body
    assert b"WAVDATA" in body

    assert (
        stt.Transcriber._elevenlabs_batch_error_detail(
            json.dumps({"detail": {"message": "Bad key"}}).encode()
        )
        == "Bad key"
    )
    assert (
        stt.Transcriber._elevenlabs_batch_error_detail(b"not-json")
        == "The service returned an unreadable error response."
    )
    assert stt.Transcriber._elevenlabs_batch_retry_delay({"Retry-After": "1.5"}, 0) == 1.5
    assert stt.Transcriber._elevenlabs_batch_retry_delay({}, 1) == 1.0


def test_transcribe_groq_with_vocabulary_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcriber = stt.Transcriber("", "groq-key", "")
    fake_groq = mock.Mock()
    fake_groq.audio.transcriptions.create.return_value = types.SimpleNamespace(
        text="  Hello Groq  "
    )
    monkeypatch.setattr(stt, "VOCABULARY", {"antigrav": "Antigravity"})
    monkeypatch.setattr(transcriber, "_get_groq_client", lambda: fake_groq)

    wav = _make_wav(np.full(1600, 1000, dtype=np.int16))
    result = transcriber.transcribe(wav, model_override=stt.GROQ_MODEL)

    assert result == "Hello Groq"
    kwargs = fake_groq.audio.transcriptions.create.call_args.kwargs
    assert kwargs["model"] == stt.GROQ_MODEL_ID
    assert kwargs["prompt"] == "Antigravity"


@pytest.mark.filterwarnings(
    "ignore:'_UnionGenericAlias' is deprecated:DeprecationWarning"
)
def test_transcribe_gemini_retries_without_system_instruction(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from google.genai import types as genai_types

    transcriber = stt.Transcriber("gem-key", "", "")
    transcriber._types = genai_types
    fake_client = mock.Mock()
    fake_client.models.generate_content.side_effect = [
        RuntimeError("Developer instruction is not enabled"),
        types.SimpleNamespace(text="  Transcribed text "),
    ]
    monkeypatch.setattr(transcriber, "_get_gemini_client", lambda: fake_client)

    wav = _make_wav(np.full(1600, 1000, dtype=np.int16))
    with caplog.at_level(logging.WARNING, logger=stt.log.name):
        result = transcriber.transcribe(wav, model_override=stt.DEFAULT_GEMINI_MODEL)

    assert result == "Transcribed text"
    assert fake_client.models.generate_content.call_count == 2
    assert "retrying as text content" in caplog.text


def test_elevenlabs_batch_retries_transient_http_and_polishes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    transcriber = stt.Transcriber("gem-key", "", "el-key")
    http_503 = urllib.error.HTTPError(
        stt.ELEVENLABS_BATCH_API_URL, 503, "Busy", {}, io.BytesIO(b"{}")
    )
    ok_resp = mock.MagicMock()
    ok_resp.__enter__.return_value = ok_resp
    ok_resp.read.return_value = json.dumps({"text": "raw transcript"}).encode()

    polish_mock = mock.Mock(return_value="Polished.")
    monkeypatch.setattr(transcriber, "_get_gemini_client", lambda: mock.Mock())
    monkeypatch.setattr(transcriber, "_polish_transcript_with_gemini", polish_mock)
    monkeypatch.setattr(
        stt.urllib.request, "urlopen", mock.Mock(side_effect=[http_503, ok_resp])
    )
    monkeypatch.setattr(stt.time, "sleep", lambda _s: None)

    wav = _make_wav(np.full(1600, 1000, dtype=np.int16))
    try:
        with caplog.at_level(logging.WARNING, logger=stt.log.name):
            result = transcriber.transcribe(wav, model_override=stt.ELEVENLABS_MODEL)
    finally:
        http_503.close()

    assert result == "Polished."
    polish_mock.assert_called_once_with("raw transcript")
    assert "HTTP 503" in caplog.text


def test_transcribe_rejects_oversized_upload_and_dry_run_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcriber = stt.Transcriber("gem-key", "", "el-key")
    transcriber.set_model(stt.ELEVENLABS_MODEL)
    monkeypatch.setattr(stt, "DRY_RUN", True)
    assert transcriber.create_live_session() is None

    oversized = b"\x00" * (stt.MAX_UPLOAD_WAV_BYTES + 1)
    monkeypatch.setattr(stt, "prepare_batch_wav", lambda _wav: oversized)
    with pytest.raises(RuntimeError):
        transcriber.transcribe(oversized)


# ---------------------------------------------------------------------------
# App state machine and clipboard
# ---------------------------------------------------------------------------
def test_app_toggle_and_cancel_transcription(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = mock.Mock(spec=stt.Recorder)
    app = stt.App(recorder)

    app.state = stt.State.TRANSCRIBING
    cancel_ev = threading.Event()
    app._transcription_cancel_event = cancel_ev
    live = mock.Mock()
    app._live_session = live

    monkeypatch.setattr(stt, "_release_escape_hotkey", lambda: None)
    app.cancel_current_operation()

    assert app.state == stt.State.IDLE
    assert cancel_ev.is_set()
    live.cancel.assert_called_once()
    event_name, _ = app.ui_events.get_nowait()
    assert event_name == "cancelled"

    # Busy states emit flash instead of launching threads.
    app.state = stt.State.STOPPING
    app.toggle()
    event_name, payload = app.ui_events.get_nowait()
    assert event_name == "flash"
    assert payload["text"] == "Microphone is still stopping"


def test_app_finish_dry_run_pastes_and_records_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats_path = tmp_path / "stats.jsonl"
    backup_wav = tmp_path / "123.wav"
    backup_wav.write_bytes(b"RIFF")

    recorder = mock.Mock(spec=stt.Recorder)
    app = stt.App(recorder)
    app.state = stt.State.TRANSCRIBING
    cancel_ev = threading.Event()
    app._transcription_cancel_event = cancel_ev
    app._stop_time = time.perf_counter()

    paste_mock = mock.Mock()
    monkeypatch.setattr(stt, "DRY_RUN", True)
    monkeypatch.setattr(stt, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(stt, "STATS_FILE", str(stats_path))
    monkeypatch.setattr(stt, "paste_text", paste_mock)
    monkeypatch.setattr(stt, "_release_escape_hotkey", lambda: None)

    app._finish(
        b"wav",
        1.5,
        backup_path=str(backup_wav),
        cancel_event=cancel_ev,
        recording_model=stt.DEFAULT_GEMINI_MODEL,
    )

    paste_mock.assert_called_once_with("[dry-run] captured 1.5 s of audio")
    assert (
        backup_wav.with_suffix(".txt").read_text(encoding="utf-8")
        == "[dry-run] captured 1.5 s of audio"
    )
    assert app.state == stt.State.IDLE
    assert stats_path.is_file()


def test_app_finish_skips_silent_audio_without_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = mock.Mock(spec=stt.Recorder)
    transcriber = mock.Mock(spec=stt.Transcriber)
    app = stt.App(recorder, transcriber)
    app.state = stt.State.TRANSCRIBING
    cancel_ev = threading.Event()
    app._transcription_cancel_event = cancel_ev

    paste_mock = mock.Mock()
    monkeypatch.setattr(stt, "DRY_RUN", False)
    monkeypatch.setattr(stt, "paste_text", paste_mock)
    monkeypatch.setattr(stt, "_release_escape_hotkey", lambda: None)

    silent_wav = _make_wav(np.zeros(8000, dtype=np.int16))
    app._finish(silent_wav, 0.5, cancel_event=cancel_ev)

    transcriber.transcribe.assert_not_called()
    paste_mock.assert_not_called()
    events = []
    while not app.ui_events.empty():
        events.append(app.ui_events.get_nowait())
    assert events[0] == ("flash", {"text": "No speech"})
    assert events[-1] == ("idle", {})


def test_paste_text_copies_and_sends_ctrl_v(monkeypatch: pytest.MonkeyPatch) -> None:
    copy_mock = mock.Mock()
    send_mock = mock.Mock()
    monkeypatch.setattr(stt.pyperclip, "copy", copy_mock)
    monkeypatch.setattr(stt.keyboard, "is_pressed", lambda _mod: False)
    monkeypatch.setattr(stt.keyboard, "send", send_mock)

    stt.paste_text("dictated text")

    copy_mock.assert_called_once_with("dictated text")
    send_mock.assert_called_once_with("ctrl+v")


def test_recover_from_system_resume_aborts_only_when_recording(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = stt.App(mock.Mock(spec=stt.Recorder))
    abort_mock = mock.Mock()
    monkeypatch.setattr(app, "abort_recording", abort_mock)

    app.recover_from_system_resume()
    abort_mock.assert_not_called()

    app.state = stt.State.RECORDING
    with caplog.at_level(logging.WARNING, logger=stt.log.name):
        app.recover_from_system_resume()
    abort_mock.assert_called_once()
    assert "system sleep" in caplog.text


# ---------------------------------------------------------------------------
# Frozen mode and API key persistence
# ---------------------------------------------------------------------------
def test_frozen_app_dir_and_restart_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe = tmp_path / "STT" / "STT.exe"
    monkeypatch.setattr(stt.sys, "frozen", True, raising=False)
    monkeypatch.setattr(stt.sys, "executable", str(fake_exe))

    assert stt._app_dir() == str(fake_exe.parent)
    cmd, cwd = stt._restart_command()
    assert cmd == [str(fake_exe), "--wait-for-existing"]
    assert cwd == str(fake_exe.parent)

    # _relaunch_with_project_venv must be a no-op when frozen.
    popen_mock = mock.Mock()
    monkeypatch.setattr(stt.subprocess, "Popen", popen_mock)
    monkeypatch.setattr(stt.sys, "prefix", stt.sys.base_prefix)
    stt._relaunch_with_project_venv()
    popen_mock.assert_not_called()


def test_save_api_keys_and_transcriber_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv_path = tmp_path / "STT" / ".env"
    dotenv_path.parent.mkdir(parents=True)
    dotenv_path.write_text("CUSTOM_FLAG=1\n", encoding="utf-8")

    monkeypatch.setattr(stt, "APP_DOTENV_FILE", str(dotenv_path))
    monkeypatch.setattr(stt, "API_KEY", "")
    monkeypatch.setattr(stt, "GROQ_API_KEY", "")
    monkeypatch.setattr(stt, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    assert stt.save_api_keys(" gem-123\n", "groq-456 ", "el-789") is True
    assert stt.API_KEY == "gem-123"
    assert stt.GROQ_API_KEY == "groq-456"
    assert stt.ELEVENLABS_API_KEY == "el-789"
    assert os.environ["GEMINI_API_KEY"] == "gem-123"
    saved = stt.dotenv_values(str(dotenv_path))
    assert saved["CUSTOM_FLAG"] == "1"
    assert saved["GEMINI_API_KEY"] == "gem-123"
    assert saved["GROQ_API_KEY"] == "groq-456"
    assert saved["ELEVENLABS_API_KEY"] == "el-789"

    transcriber = stt.Transcriber("old1", "old2", "old3")
    old_client = mock.Mock()
    transcriber._client = old_client
    transcriber.update_api_keys("new1", "new2", "new3")
    old_client.close.assert_called_once()
    assert transcriber._client is None
    assert transcriber._gemini_api_key == "new1"
    assert transcriber._groq_api_key == "new2"
    assert transcriber._elevenlabs_api_key == "new3"


def test_app_open_api_keys_emits_event() -> None:
    app = stt.App(mock.Mock(spec=stt.Recorder))
    app.open_api_keys()
    event_name, payload = app.ui_events.get_nowait()
    assert event_name == "api_keys"
    assert payload == {}


# ---------------------------------------------------------------------------
# Launch at Windows startup helpers and tray toggle
# ---------------------------------------------------------------------------
def _make_fake_winreg(initial_keys: dict[str, dict[str, tuple[int, object]]] | None = None):
    """Build an in-memory winreg stand-in backed by a plain dict."""
    store: dict[str, dict[str, tuple[int, object]]] = (
        dict(initial_keys) if initial_keys is not None else {}
    )

    class _FakeKey:
        def __init__(self, subkey: str, create: bool = False) -> None:
            if subkey not in store:
                if not create:
                    raise FileNotFoundError(subkey)
                store[subkey] = {}
            self.subkey = subkey

        def __enter__(self) -> "_FakeKey":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_SET_VALUE=0x0002,
        REG_SZ=1,
        store=store,
        OpenKey=lambda _root, subkey, *_args: _FakeKey(subkey, create=False),
        CreateKey=lambda _root, subkey: _FakeKey(subkey, create=True),
        QueryValueEx=lambda key, name: (
            store[key.subkey][name][1],
            store[key.subkey][name][0],
        )
        if name in store[key.subkey]
        else (_ for _ in ()).throw(FileNotFoundError(name)),
        SetValueEx=lambda key, name, _res, typ, val: store[key.subkey].__setitem__(
            name, (typ, val)
        ),
        DeleteValue=lambda key, name: store[key.subkey].pop(name)
        if name in store[key.subkey]
        else (_ for _ in ()).throw(FileNotFoundError(name)),
    )
    return fake


def test_startup_command_frozen_and_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe = tmp_path / "STT" / "STT.exe"
    monkeypatch.setattr(stt.sys, "frozen", True, raising=False)
    monkeypatch.setattr(stt.sys, "executable", str(fake_exe))
    assert stt._startup_command() == f'"{fake_exe}"'

    monkeypatch.setattr(stt.sys, "frozen", False, raising=False)
    venv_scripts = tmp_path / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    py_exe = venv_scripts / "python.exe"
    pyw_exe = venv_scripts / "pythonw.exe"
    py_exe.write_text("", encoding="utf-8")
    pyw_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(stt.sys, "executable", str(py_exe))
    assert stt._startup_command() == f'"{pyw_exe}" "{Path(stt.__file__).resolve()}"'


def test_startup_enable_disable_and_approved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_winreg = _make_fake_winreg()
    appdata = tmp_path / "AppData"
    startup_dir = (
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    startup_dir.mkdir(parents=True)
    legacy_lnk = startup_dir / "STT.lnk"
    legacy_lnk.write_bytes(b"stub-lnk")

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(stt, "winreg", fake_winreg)
    monkeypatch.setattr(stt.os, "name", "nt")
    monkeypatch.setattr(stt.sys, "frozen", True, raising=False)
    fake_exe = tmp_path / "ProgramFiles" / "STT" / "STT.exe"
    monkeypatch.setattr(stt.sys, "executable", str(fake_exe))

    # Legacy shortcut alone counts as enabled until toggled off.
    assert stt.is_startup_enabled() is True
    assert stt.set_startup_enabled(False) is True
    assert not legacy_lnk.exists()
    assert stt.is_startup_enabled() is False

    # Task Manager StartupApproved disable byte (0x03) marks entry disabled.
    assert stt.set_startup_enabled(True) is True
    assert stt.is_startup_enabled() is True
    fake_winreg.store[stt.WINDOWS_STARTUP_APPROVED_RUN_PATH] = {
        stt.STARTUP_APP_NAME: (3, b"\x03" + b"\x00" * 11)
    }
    assert stt.is_startup_enabled() is False

    # Re-enabling clears the stale StartupApproved disable entry.
    assert stt.set_startup_enabled(True) is True
    assert stt.STARTUP_APP_NAME not in fake_winreg.store[stt.WINDOWS_STARTUP_APPROVED_RUN_PATH]
    assert fake_winreg.store[stt.WINDOWS_RUN_REG_PATH][stt.STARTUP_APP_NAME] == (
        fake_winreg.REG_SZ,
        f'"{fake_exe}"',
    )
    assert stt.is_startup_enabled() is True


def test_tray_toggle_startup_emits_feedback_and_updates_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_winreg = _make_fake_winreg()
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(stt, "winreg", fake_winreg)
    monkeypatch.setattr(stt.os, "name", "nt")

    app = stt.App(mock.Mock(spec=stt.Recorder))
    dummy_tray = types.SimpleNamespace(
        _app=app,
        _icon=mock.Mock(),
        _schedule_menu_update=mock.Mock(),
    )

    assert stt.TrayManager._is_startup_checked(None) is False
    stt.TrayManager._on_toggle_startup(dummy_tray, dummy_tray._icon, None)
    assert stt.is_startup_enabled() is True
    assert stt.TrayManager._is_startup_checked(None) is True
    assert app.ui_events.get_nowait() == (
        "flash",
        {"text": "Launch at startup enabled"},
    )
    dummy_tray._schedule_menu_update.assert_called_once_with(dummy_tray._icon)

    stt.TrayManager._on_toggle_startup(dummy_tray, dummy_tray._icon, None)
    assert stt.is_startup_enabled() is False
    assert app.ui_events.get_nowait() == (
        "flash",
        {"text": "Launch at startup disabled"},
    )


def test_save_api_keys_clears_values_quotes_and_handles_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv_path = tmp_path / "STT" / ".env"
    dotenv_path.parent.mkdir(parents=True)
    dotenv_path.write_text('CUSTOM_NOTE="hello world"\n', encoding="utf-8")

    monkeypatch.setattr(stt, "APP_DOTENV_FILE", str(dotenv_path))
    monkeypatch.setattr(stt, "API_KEY", "old-gem")
    monkeypatch.setattr(stt, "GROQ_API_KEY", "old-groq")
    monkeypatch.setattr(stt, "ELEVENLABS_API_KEY", "old-el")
    monkeypatch.setenv("GEMINI_API_KEY", "old-gem")
    monkeypatch.setenv("GROQ_API_KEY", "old-groq")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "old-el")

    # Clearing Gemini & ElevenLabs removes them from os.environ while preserving CUSTOM_NOTE.
    assert stt.save_api_keys("", "new-groq", "") is True
    assert stt.API_KEY == ""
    assert stt.GROQ_API_KEY == "new-groq"
    assert stt.ELEVENLABS_API_KEY == ""
    assert "GEMINI_API_KEY" not in os.environ
    assert "ELEVENLABS_API_KEY" not in os.environ
    assert os.environ["GROQ_API_KEY"] == "new-groq"
    saved = stt.dotenv_values(str(dotenv_path))
    assert saved["CUSTOM_NOTE"] == "hello world"

    # Disk failure must return False without mutating runtime keys.
    monkeypatch.setattr(
        stt, "_atomic_write_text", mock.Mock(side_effect=OSError("disk full"))
    )
    assert stt.save_api_keys("fail-gem", "fail-groq", "fail-el") is False
    assert stt.GROQ_API_KEY == "new-groq"


def test_tray_manager_settings_menu_and_startup_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pystray

    created_icons = []

    class _FakeIcon:
        def __init__(self, name: str, icon: object, title: str, menu: object) -> None:
            self.name = name
            self.icon = icon
            self.title = title
            self.menu = menu
            created_icons.append(self)

        def update_menu(self) -> None:
            return None

    monkeypatch.setattr(pystray, "Icon", _FakeIcon)
    app = stt.App(mock.Mock(spec=stt.Recorder))
    tray = stt.TrayManager(app, lambda: None)

    assert len(created_icons) == 1
    top_items = {item.text: item for item in created_icons[0].menu.items}
    assert "Settings" in top_items
    settings_items = {item.text: item for item in top_items["Settings"].submenu.items}
    assert "API keys..." in settings_items
    assert "Launch at startup" in settings_items

    monkeypatch.setattr(stt, "is_startup_enabled", lambda: False)
    monkeypatch.setattr(stt, "set_startup_enabled", lambda _enabled: False)
    monkeypatch.setattr(tray, "_schedule_menu_update", mock.Mock())
    tray._on_toggle_startup(created_icons[0], None)
    assert app.ui_events.get_nowait() == (
        "error",
        {"text": "Could not update startup setting"},
    )


def test_app_icon_ico_has_standard_windows_sizes() -> None:
    import struct

    ico_path = Path(__file__).resolve().parent / "STT.ico"
    assert ico_path.is_file()
    data = ico_path.read_bytes()
    reserved, typ, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, typ) == (0, 1)
    dims = set()
    for idx in range(count):
        w, h, _, _, _, bpp, _, _ = struct.unpack_from("<BBBBHHII", data, 6 + idx * 16)
        assert bpp == 32
        dims.add((w or 256, h or 256))
    assert {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)} <= dims
