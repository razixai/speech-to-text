"""
stt.pyw
------------------
Background push-to-toggle dictation for Windows.

Press the configured recording shortcut once to start recording and again to
stop. When the ElevenLabs model is selected, audio streams to Scribe and
cumulative transcript updates are polished by Gemini while recording. The
matching final Scribe transcript reuses that work; changed final text receives
a normal final polish. If streaming fails, the saved recording is sent to
Scribe's batch endpoint as a reliability fallback. The result is pasted at the
cursor.

Keep this file in the project folder. A Startup-folder shortcut should point to
the project virtual environment's pythonw.exe and pass this file's full path.
Logs and settings go under %LOCALAPPDATA%\\STT.

Set the environment variable GEMINI_DICTATE_DRY_RUN=1 (or DRY_RUN = True
below) to test the whole hotkey/recording/paste loop without spending any
API credits.
"""

import base64
import contextlib
import ctypes
import difflib
import io
import json
import logging
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from ctypes import wintypes

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only stdlib module
    winreg = None  # type: ignore[assignment]
from collections.abc import Callable, Iterator, Mapping
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

APP_DATA_DIR = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
LEGACY_DATA_DIR = os.path.join(APP_DATA_DIR, "GeminiDictate")
LOG_DIR = os.path.join(APP_DATA_DIR, "STT")
LOG_FILE = os.path.join(LOG_DIR, "gemini_dictate.log")
SETTINGS_FILE = os.path.join(LOG_DIR, "settings.json")
VOCABULARY_FILE = os.path.join(LOG_DIR, "vocabulary.json")
STATS_FILE = os.path.join(LOG_DIR, "stats.jsonl")
TRANSCRIPT_DIR = os.path.join(LOG_DIR, "transcripts")
APP_DOTENV_FILE = os.path.join(LOG_DIR, ".env")


def _app_dir() -> str:
    """Return the executable directory when frozen, or the script directory."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DOTENV_FILE = os.path.join(_app_dir(), ".env")

log = logging.getLogger("gemini_dictate")


def setup_logging() -> None:
    """Configure rotating file and optional stderr logging once."""
    if log.handlers:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8", delay=True
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    handler.setFormatter(fmt)
    log.addHandler(handler)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.INFO)


def _startup_error_box(title: str, message: str) -> None:
    """Display a fatal startup error dialog and mirror it to stderr."""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            ctypes.WinDLL("user32").MessageBoxW(None, message, title, 0x10)
    if sys.stderr is not None:
        with contextlib.suppress(Exception):
            print(f"{title}: {message}", file=sys.stderr)


def _project_venv_setup_hint(venv_python: str | None = None) -> str:
    """Build setup and repair instructions for the local venv."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(script_dir, ".venv")
    create_or_repair = "--upgrade " if os.path.isdir(venv_dir) else ""
    return (
        f"App folder: {script_dir}\n"
        f"Environment interpreter: {venv_python or os.path.join(venv_dir, 'Scripts', 'python.exe')}\n\n"
        "From the app folder, create/repair the environment and install its packages:\n"
        f"  py -3.14 -m venv {create_or_repair}.venv\n"
        "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
        "Then launch with .\\.venv\\Scripts\\pythonw.exe .\\stt.pyw."
    )


def _relaunch_with_project_venv() -> None:
    """Use the app-local venv when a shortcut invokes global Python."""
    if os.name != "nt" or getattr(sys, "frozen", False) or sys.prefix != sys.base_prefix:
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, os.path.basename(__file__))
    venv_dir = os.path.join(script_dir, ".venv", "Scripts")
    venv_python = os.path.join(venv_dir, "python.exe")
    venv_pythonw = os.path.join(venv_dir, "pythonw.exe")
    if not os.path.isfile(venv_python):
        return

    probe = (
        "import tkinter, keyboard, numpy, pyperclip, sounddevice, dotenv, "
        "PIL, pycaw, pystray, websocket, google.genai, groq"
    )
    try:
        result = subprocess.run(
            [venv_python, "-c", probe],
            cwd=script_dir,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _startup_error_box(
            "STT Environment Error",
            f"The app-local Python environment could not be started.\n\n{exc}\n\n"
            f"{_project_venv_setup_hint(venv_python)}",
        )
        sys.exit(1)

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or "Package import check failed"
        ).strip()
        _startup_error_box(
            "STT Environment Error",
            f"The app-local environment is missing a required package or is invalid:\n\n"
            f"{detail[:1200]}\n\n{_project_venv_setup_hint(venv_python)}",
        )
        sys.exit(1)

    if not os.path.isfile(venv_pythonw):
        _startup_error_box(
            "STT Environment Error",
            f"The app-local environment is missing pythonw.exe.\n\n"
            f"{_project_venv_setup_hint(venv_python)}",
        )
        sys.exit(1)

    try:
        subprocess.Popen(
            [venv_pythonw, script_path, *sys.argv[1:]],
            cwd=script_dir,
            close_fds=True,
            creationflags=(
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        )
    except OSError as exc:
        _startup_error_box(
            "STT Environment Error",
            f"Could not launch the app-local Python environment:\n\n{exc}\n\n"
            f"{_project_venv_setup_hint(venv_python)}",
        )
        sys.exit(1)
    sys.exit(0)


_relaunch_with_project_venv()


try:
    import tkinter as tk
    from tkinter import messagebox, ttk

    import keyboard
    import numpy as np
    import pyperclip
    import sounddevice as sd
    from dotenv import dotenv_values
    from PIL import Image, ImageDraw
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
except Exception as import_exc:  # noqa: BLE001 - report missing runtime deps under pyw.exe
    with contextlib.suppress(Exception):
        setup_logging()
        log.exception("Failed to import required dependency")
    _startup_error_box(
        "STT Startup Error",
        f"Failed to load a required Python package:\n\n{import_exc}\n\n"
        f"Python: {sys.executable}\nLog: {LOG_FILE}\n\n"
        f"{_project_venv_setup_hint()}",
    )
    sys.exit(1)

if TYPE_CHECKING:
    import pystray
    from google import genai
    from google.genai import types
    from groq import Groq

_INHERITED_ENVIRONMENT_KEYS = frozenset(os.environ)


def _load_dotenv_values(path: str, *, override: bool = False) -> None:
    """Load non-empty file values without overriding inherited OS variables."""
    if not os.path.isfile(path):
        return
    for name, value in dotenv_values(path).items():
        if value is None or not value.strip() or name in _INHERITED_ENVIRONMENT_KEYS:
            continue
        if override or name not in os.environ:
            os.environ[name] = value


def _format_dotenv_line(name: str, value: str) -> str:
    """Format one single-line .env entry safely."""
    cleaned = value.replace("\r", "").replace("\n", "").strip()
    if any(ch.isspace() or ch in ('"', "'", "#") for ch in cleaned):
        escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
        return f'{name}="{escaped}"'
    return f"{name}={cleaned}"


def save_api_keys(gemini_key: str, groq_key: str, elevenlabs_key: str) -> bool:
    """Persist provider API keys to the AppData .env file and update runtime state."""
    global API_KEY, GROQ_API_KEY, ELEVENLABS_API_KEY
    updates = {
        "GEMINI_API_KEY": gemini_key.replace("\r", "").replace("\n", "").strip(),
        "GROQ_API_KEY": groq_key.replace("\r", "").replace("\n", "").strip(),
        "ELEVENLABS_API_KEY": elevenlabs_key.replace("\r", "").replace("\n", "").strip(),
    }
    with SETTINGS_LOCK:
        existing: dict[str, str] = {}
        if os.path.isfile(APP_DOTENV_FILE):
            try:
                for key, val in dotenv_values(APP_DOTENV_FILE).items():
                    if key and val is not None:
                        existing[key] = val
            except Exception:
                log.warning("Could not read existing AppData .env; overwriting keys", exc_info=True)
        existing.update(updates)
        lines = [_format_dotenv_line(key, val) for key, val in existing.items()]
        contents = "\n".join(lines) + ("\n" if lines else "")
        try:
            _atomic_write_text(APP_DOTENV_FILE, contents)
        except OSError:
            log.exception("Could not save API keys")
            return False

        for key, val in updates.items():
            if val:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        API_KEY = updates["GEMINI_API_KEY"]
        GROQ_API_KEY = updates["GROQ_API_KEY"]
        ELEVENLABS_API_KEY = updates["ELEVENLABS_API_KEY"]
    return True


def _restart_command() -> tuple[list[str], str]:
    """Build the command and working directory used when restarting STT."""
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        return [exe_path, "--wait-for-existing"], os.path.dirname(exe_path)
    script_path = os.path.abspath(__file__)
    return [sys.executable, script_path, "--wait-for-existing"], os.path.dirname(script_path)


STARTUP_APP_NAME = "STT"
WINDOWS_RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_STARTUP_APPROVED_RUN_PATH = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
)
STARTUP_LOCK = threading.Lock()


def _startup_shortcut_path() -> str:
    """Return the legacy per-user Windows Startup folder shortcut path."""
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return ""
    return os.path.join(
        appdata,
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
        f"{STARTUP_APP_NAME}.lnk",
    )


def _startup_command() -> str:
    """Build the command stored in HKCU Run to start STT on Windows login."""
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        return f'"{exe_path}"'
    python_exe = os.path.abspath(sys.executable)
    if os.path.basename(python_exe).casefold() == "python.exe":
        pythonw_exe = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
        if os.path.isfile(pythonw_exe):
            python_exe = pythonw_exe
    script_path = os.path.abspath(__file__)
    return f'"{python_exe}" "{script_path}"'


def _is_startup_approved_disabled() -> bool:
    """Return True when Task Manager explicitly disabled the Run startup entry."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, WINDOWS_STARTUP_APPROVED_RUN_PATH
        ) as key:
            data, _ = winreg.QueryValueEx(key, STARTUP_APP_NAME)
    except OSError:
        return False
    if isinstance(data, (bytes, bytearray)) and len(data) >= 1:
        return bool(data[0] & 1)
    return False


def is_startup_enabled() -> bool:
    """Return whether STT is configured to launch automatically at Windows login."""
    if os.name != "nt" or winreg is None:
        return False
    value = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_REG_PATH) as key:
            raw_val, _ = winreg.QueryValueEx(key, STARTUP_APP_NAME)
            if isinstance(raw_val, str):
                value = raw_val
    except FileNotFoundError:
        value = ""
    except OSError:
        log.warning("Could not read Windows startup registry entry", exc_info=True)
        value = ""
    if value.strip():
        return not _is_startup_approved_disabled()
    shortcut = _startup_shortcut_path()
    return bool(shortcut and os.path.isfile(shortcut))


def _delete_hkcu_value(subkey: str, value_name: str) -> None:
    """Delete an HKCU registry value if present, raising OSError on other errors."""
    if winreg is None:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE
        ) as key:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass


def set_startup_enabled(enabled: bool) -> bool:
    """Enable or disable automatic launch at Windows login via HKCU Run."""
    if os.name != "nt" or winreg is None:
        return False
    with STARTUP_LOCK:
        shortcut = _startup_shortcut_path()
        try:
            if shortcut and os.path.isfile(shortcut):
                os.remove(shortcut)
        except OSError:
            if not enabled:
                log.exception("Could not remove legacy Windows startup shortcut")
                return False
            log.warning(
                "Could not remove legacy Windows startup shortcut", exc_info=True
            )

        try:
            try:
                _delete_hkcu_value(WINDOWS_STARTUP_APPROVED_RUN_PATH, STARTUP_APP_NAME)
            except OSError:
                log.debug("Could not clear StartupApproved entry", exc_info=True)

            if enabled:
                command = _startup_command()
                with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER, WINDOWS_RUN_REG_PATH
                ) as run_key:
                    winreg.SetValueEx(
                        run_key, STARTUP_APP_NAME, 0, winreg.REG_SZ, command
                    )
                log.info("Enabled launch at Windows startup: %s", command)
                return True

            _delete_hkcu_value(WINDOWS_RUN_REG_PATH, STARTUP_APP_NAME)
            log.info("Disabled launch at Windows startup")
            return True
        except OSError:
            log.exception("Could not update Windows startup setting")
            return False


# Project-local values take priority over AppData, while OS values always win.
_load_dotenv_values(APP_DOTENV_FILE)
_load_dotenv_values(DOTENV_FILE, override=True)

# ---------------------------------------------------------------
# Config -- change these if you like
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
# Used only to migrate installations that predate saved recording shortcuts.
LEGACY_RECORDING_HOTKEY = "left shift+backslash"
HOTKEY = None  # loaded from settings; press once to start, again to stop
LEARN_HOTKEY = "left shift+backslash+z"  # save a word correction
HOTKEY_MODIFIERS = frozenset(
    {
        "ctrl",
        "control",
        "left ctrl",
        "right ctrl",
        "alt",
        "left alt",
        "right alt",
        "alt gr",
        "shift",
        "left shift",
        "right shift",
        "win",
        "windows",
        "left win",
        "right win",
        "left windows",
        "right windows",
    }
)
HOTKEY_KEY_ALIASES = {
    "control": "ctrl",
    "left ctrl": "ctrl",
    "right ctrl": "ctrl",
    "left alt": "alt",
    "right alt": "alt",
    "left shift": "shift",
    "right shift": "shift",
    "windows": "win",
    "left windows": "win",
    "right windows": "win",
}
LEARN_HOTKEY_SCANCODES = ((42, 43, 44),)
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
PREFERRED_MODEL = DEFAULT_GEMINI_MODEL
GROQ_MODEL_ID = "whisper-large-v3-turbo"
GROQ_MODEL = f"groq:{GROQ_MODEL_ID}"
ELEVENLABS_REALTIME_MODEL_ID = "scribe_v2_realtime"
ELEVENLABS_MODEL = f"elevenlabs:{ELEVENLABS_REALTIME_MODEL_ID}"
LEGACY_ELEVENLABS_MODEL = "elevenlabs:scribe_v2"
GEMINI_ENGLISH_POLISH_MODEL = DEFAULT_GEMINI_MODEL
SAMPLE_RATE = 16000
ELEVENLABS_SUPPORTED_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 44100, 48000})
MIN_SECONDS = 0.3  # shorter recordings are discarded without an API call
MIN_RECORDING_OPTIONS = (0.1, 0.3, 0.5)
AUDIO_RMS_THRESHOLD = 140
AUDIO_PEAK_THRESHOLD = 400
SILENCE_TRIM_THRESHOLD = 140
SILENCE_PADDING_SECONDS = 0.15
MAX_UPLOAD_WAV_BYTES = 24 * 1024 * 1024
MAX_RECORDING_BUFFER_BYTES = MAX_UPLOAD_WAV_BYTES - 44  # PCM WAV header is 44 bytes.
DRY_RUN = os.environ.get("GEMINI_DICTATE_DRY_RUN", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_PLAY_PAUSE = 14
SMTO_ABORTIFHUNG = 0x0002
GEMINI_POLISH_TIMEOUT_SECONDS = 60
TRANSCRIPTION_TIMEOUT_SECONDS = 150
ELEVENLABS_REALTIME_API_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
ELEVENLABS_BATCH_API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_BATCH_MODEL_ID = "scribe_v2"
ELEVENLABS_BATCH_TIMEOUT_SECONDS = 60
ELEVENLABS_BATCH_MAX_ATTEMPTS = 2
ELEVENLABS_BATCH_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
LIVE_AUDIO_CHUNK_SECONDS = 0.1
LIVE_AUDIO_QUEUE_SIZE = 600
LIVE_CONNECT_TIMEOUT_SECONDS = 5
LIVE_SOCKET_TIMEOUT_SECONDS = 2.0
LIVE_FINALIZE_TIMEOUT_SECONDS = 5
LIVE_COMMIT_INTERVAL_SECONDS = 25
LIVE_FINAL_RESPONSE_POLL_SECONDS = 0.1
LIVE_GEMINI_POLISH_DEBOUNCE_SECONDS = 0.5
LIVE_GEMINI_POLISH_MIN_INTERVAL_SECONDS = 4.0
LIVE_GEMINI_POLISH_CACHE_LIMIT = 32
LIVE_POLISH_FUZZY_THRESHOLD = 0.9
HOTKEY_CHORD_GRACE_SECONDS = 0.35
MICROPHONE_REFRESH_INTERVAL_MS = 10_000
RESTART_INSTANCE_WAIT_SECONDS = 30
SHUTDOWN_CLEANUP_TIMEOUT_SECONDS = 3.0

SYSTEM_PROMPT = """
You are an English-only speech transcriber and transcript editor. The input may be speech audio or a rough speech transcript. Return only the speaker's intended message in clear, natural English.
- Translate all non-English and mixed-language content into English.
- Correct grammar, word order, and punctuation; polish phrasing only to improve clarity and flow.
- When the speaker corrects themselves with "sorry" or "I mean", discard the wrong version and keep only the correction (e.g., "I live in Berlin, oh sorry, I live in London" becomes "I live in London"). Remove the apology itself.
- Preserve the meaning, names, numbers, facts, tone, and intent. Do not add details, answer questions, or summarize.
- Remove fillers, stutters, repeated sentences, duplicate phrases, and false starts without dropping meaningful content.
- If a word is unclear, keep the closest wording supported by the input instead of inventing one.
- Do not include a heading, explanation, language label, or quotation marks around the result.
""".strip()

MAX_TRANSCRIPT_BACKUPS = 30
MUTEX_NAME = "Local\\GeminiDictate"
MUTEX_HANDLE = None

MODEL_OPTIONS = (
    DEFAULT_GEMINI_MODEL,
    "gemini-3.8-flash",
    GROQ_MODEL,
    ELEVENLABS_MODEL,
)
REMOVED_MODEL_OPTIONS = frozenset({"gemini-3.5-transcribe", "gemini-2.5-flash"})

MODEL_LABELS = {
    GROQ_MODEL: "Groq - Whisper Large V3 Turbo",
    ELEVENLABS_MODEL: "ElevenLabs + Gemini (English polish)",
}

SETTINGS_LOCK = threading.RLock()
_COM_RPC_E_CHANGED_MODE = -2147417850


@contextlib.contextmanager
def _com_scope() -> Iterator[None]:
    """Initialize COM safely on worker threads even if PortAudio set MTA."""
    initialized = False
    try:
        from comtypes import CoInitialize, CoUninitialize

        try:
            CoInitialize()
            initialized = True
        except OSError as exc:
            if getattr(exc, "winerror", None) != _COM_RPC_E_CHANGED_MODE:
                raise
        yield
    finally:
        if initialized:
            with contextlib.suppress(Exception):
                from comtypes import CoUninitialize

                CoUninitialize()


def migrate_legacy_data() -> None:
    """Copy existing settings and transcript backups without changing originals."""
    if not os.path.isdir(LEGACY_DATA_DIR):
        return

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        log.warning("Could not create the new app data directory", exc_info=True)
        return

    for filename in ("settings.json", "vocabulary.json"):
        source = os.path.join(LEGACY_DATA_DIR, filename)
        destination = os.path.join(LOG_DIR, filename)
        if os.path.isfile(source) and not os.path.exists(destination):
            try:
                shutil.copy2(source, destination)
            except OSError:
                log.warning(
                    "Could not copy legacy app data file: %s", source, exc_info=True
                )

    legacy_transcript_dir = os.path.join(LEGACY_DATA_DIR, "transcripts")
    if not os.path.isdir(legacy_transcript_dir):
        return

    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        for filename in os.listdir(legacy_transcript_dir):
            if not filename.lower().endswith((".wav", ".txt")):
                continue
            source = os.path.join(legacy_transcript_dir, filename)
            destination = os.path.join(TRANSCRIPT_DIR, filename)
            if os.path.isfile(source) and not os.path.exists(destination):
                try:
                    shutil.copy2(source, destination)
                except OSError:
                    log.warning(
                        "Could not copy legacy transcript backup: %s",
                        source,
                        exc_info=True,
                    )
    except OSError:
        log.warning("Could not migrate legacy transcript backups", exc_info=True)


def model_label(model: str) -> str:
    """Return the display label for a model identifier."""
    return MODEL_LABELS.get(model, model)


def canonical_hotkey_key(key: str) -> str:
    """Normalize a modifier key name to its canonical alias."""
    return HOTKEY_KEY_ALIASES.get(key, key)


def validate_recording_hotkey(value: object) -> str:
    """Validate and normalize a recording shortcut string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Capture a shortcut with at least one modifier and one key.")

    hotkey = value.strip().casefold()
    try:
        parsed = keyboard.parse_hotkey(hotkey)
    except Exception as exc:
        raise ValueError("That shortcut is not recognized.") from exc

    if len(parsed) != 1:
        raise ValueError("Use one key combination, not a sequence.")
    if any(not key_codes for key_codes in parsed[0]):
        raise ValueError("That shortcut contains an unknown key.")
    keys = {part.strip() for part in hotkey.split("+")}
    if not keys.intersection(HOTKEY_MODIFIERS):
        raise ValueError("Include a modifier, such as Ctrl, Alt, or Shift.")
    if keys.issubset(HOTKEY_MODIFIERS):
        raise ValueError("Include a regular key along with the modifier.")

    if sorted(map(canonical_hotkey_key, keys)) == sorted(
        map(canonical_hotkey_key, LEARN_HOTKEY.split("+"))
    ):
        raise ValueError("That shortcut is already used for vocabulary learning.")
    return hotkey


def load_settings() -> dict[str, Any]:
    """Load persisted user settings with defaults and hotkey migration."""
    global HOTKEY_SETTING_MIGRATED
    defaults = {
        "model": PREFERRED_MODEL,
        "min_seconds": MIN_SECONDS,
        "recording_hotkey": None,
    }
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            saved = json.load(settings_file)
        if isinstance(saved, dict):
            if saved and "recording_hotkey" not in saved:
                defaults["recording_hotkey"] = LEGACY_RECORDING_HOTKEY
                HOTKEY_SETTING_MIGRATED = True
            defaults.update(saved)
    except (OSError, ValueError):
        pass
    return defaults


def _atomic_write_text(path: str, contents: str) -> None:
    """Replace a text file atomically so failures cannot leave it truncated."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as output_file:
            output_file.write(contents)
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temp_path)


def save_settings() -> bool:
    """Persist the current settings dictionary atomically to disk."""
    with SETTINGS_LOCK:
        try:
            snapshot = dict(SETTINGS)
            _atomic_write_text(SETTINGS_FILE, json.dumps(snapshot, indent=2))
        except OSError:
            log.exception("Could not save settings")
            return False
    return True


def load_vocabulary() -> dict[str, str]:
    """Load case-folded custom vocabulary corrections from disk."""
    try:
        with open(VOCABULARY_FILE, "r", encoding="utf-8") as vocabulary_file:
            saved = json.load(vocabulary_file)
        if isinstance(saved, dict):
            return {
                str(wrong).strip().casefold(): str(correct).strip()
                for wrong, correct in saved.items()
                if str(wrong).strip() and str(correct).strip()
            }
    except (OSError, ValueError):
        pass
    return {}


def save_vocabulary() -> None:
    """Persist the custom vocabulary dictionary atomically to disk."""
    with SETTINGS_LOCK:
        try:
            snapshot = dict(VOCABULARY)
            contents = json.dumps(snapshot, indent=2, ensure_ascii=False)
            _atomic_write_text(VOCABULARY_FILE, contents)
        except OSError:
            log.exception("Could not save vocabulary")


def archive_recording(wav: bytes) -> str | None:
    """Save a recording for recovery and return its backup path."""
    temp_path = None
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        stem = str(time.time_ns())
        path = os.path.join(TRANSCRIPT_DIR, f"{stem}.wav")
        temp_path = f"{path}.tmp"
        with open(temp_path, "wb") as recording_file:
            recording_file.write(wav)
        os.replace(temp_path, path)
    except OSError:
        log.exception("Could not archive recording")
        return None
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                os.remove(temp_path)

    try:
        recordings = sorted(
            (
                os.path.join(TRANSCRIPT_DIR, name)
                for name in os.listdir(TRANSCRIPT_DIR)
                if name.endswith(".wav")
            ),
            key=os.path.getmtime,
            reverse=True,
        )
    except OSError:
        log.exception("Could not list old transcript backups for cleanup")
        return path

    for old_path in recordings[MAX_TRANSCRIPT_BACKUPS:]:
        try:
            os.remove(old_path)
            old_text = os.path.splitext(old_path)[0] + ".txt"
            with contextlib.suppress(FileNotFoundError):
                os.remove(old_text)
        except OSError:
            log.warning(
                "Could not remove old transcript backup: %s", old_path, exc_info=True
            )
    return path


def recent_recordings(limit: int = 3) -> list[str]:
    """Return paths to the most recent archived WAV recordings."""
    try:
        recordings = [
            os.path.join(TRANSCRIPT_DIR, name)
            for name in os.listdir(TRANSCRIPT_DIR)
            if name.endswith(".wav")
        ]
        return sorted(recordings, key=os.path.getmtime, reverse=True)[:limit]
    except OSError:
        return []


def save_transcript_backup(wav_path: str, text: str) -> None:
    """Save a transcript file alongside its archived WAV backup."""
    try:
        text_path = os.path.splitext(wav_path)[0] + ".txt"
        _atomic_write_text(text_path, text)
    except OSError:
        log.exception("Could not save transcript backup")


def record_stats(words: int, seconds: float, model: str) -> None:
    """Append one usage entry after a successful transcription paste."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        entry = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "words": words,
                "seconds": round(seconds, 1),
                "wpm": round(words / (seconds / 60)) if seconds > 0 else 0,
                "model": model or "",
            },
            ensure_ascii=False,
        )
        with open(STATS_FILE, "a", encoding="utf-8") as stats_file:
            stats_file.write(entry + "\n")
    except Exception:
        log.info("Could not record usage stats", exc_info=True)


def load_stats() -> list[dict[str, Any]]:
    """Read all usage entries from the stats file."""
    entries = []
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as stats_file:
            for line in stats_file:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except (TypeError, ValueError):
                        continue
    except FileNotFoundError:
        pass
    except OSError:
        log.info("Could not read usage stats", exc_info=True)
    return entries


def _format_duration(total_seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    total_seconds = int(total_seconds)
    if total_seconds < 60:
        return f"{total_seconds} sec"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} sec"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def compute_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate statistics from usage entries."""
    if not entries:
        return {
            "total_words": 0,
            "total_transcriptions": 0,
            "total_seconds": 0.0,
            "avg_wpm": 0,
            "current_streak": 0,
            "today_words": 0,
            "today_transcriptions": 0,
        }
    import datetime

    total_words = sum(e.get("words", 0) for e in entries)
    total_seconds = sum(e.get("seconds", 0) for e in entries)
    total_transcriptions = len(entries)
    avg_wpm = round(total_words / (total_seconds / 60)) if total_seconds > 0 else 0

    today = time.strftime("%Y-%m-%d")
    today_entries = [e for e in entries if e.get("ts", "").startswith(today)]
    today_words = sum(e.get("words", 0) for e in today_entries)
    today_transcriptions = len(today_entries)

    dates = set()
    for e in entries:
        ts = e.get("ts", "")
        if len(ts) >= 10:
            dates.add(ts[:10])

    current_streak = 0
    check_date = datetime.date.today()
    if today not in dates:
        check_date -= datetime.timedelta(days=1)
    while check_date.isoformat() in dates:
        current_streak += 1
        check_date -= datetime.timedelta(days=1)

    return {
        "total_words": total_words,
        "total_transcriptions": total_transcriptions,
        "total_seconds": total_seconds,
        "avg_wpm": avg_wpm,
        "current_streak": current_streak,
        "today_words": today_words,
        "today_transcriptions": today_transcriptions,
    }


HOTKEY_SETTING_MIGRATED = False
migrate_legacy_data()
SETTINGS = load_settings()
VOCABULARY = load_vocabulary()
HOTKEY_SETTING_RESET = False
try:
    saved_hotkey = SETTINGS.get("recording_hotkey")
    HOTKEY = validate_recording_hotkey(saved_hotkey) if saved_hotkey else None
except ValueError:
    HOTKEY = None
    SETTINGS["recording_hotkey"] = None
    HOTKEY_SETTING_RESET = True
saved_model = SETTINGS.get("model", PREFERRED_MODEL)
PREFERRED_MODEL = (
    saved_model.strip()
    if isinstance(saved_model, str) and saved_model.strip()
    else DEFAULT_GEMINI_MODEL
)
MODEL_SETTING_MIGRATED = PREFERRED_MODEL in REMOVED_MODEL_OPTIONS
if PREFERRED_MODEL == LEGACY_ELEVENLABS_MODEL:
    PREFERRED_MODEL = ELEVENLABS_MODEL
    SETTINGS["model"] = PREFERRED_MODEL
    MODEL_SETTING_MIGRATED = True
elif PREFERRED_MODEL not in MODEL_OPTIONS:
    PREFERRED_MODEL = DEFAULT_GEMINI_MODEL
    SETTINGS["model"] = PREFERRED_MODEL
    MODEL_SETTING_MIGRATED = True
OBSOLETE_SETTINGS_MIGRATED = False
for obsolete_setting in ("reasoning_level", "smart_mode", "beeps"):
    if obsolete_setting in SETTINGS:
        SETTINGS.pop(obsolete_setting)
        OBSOLETE_SETTINGS_MIGRATED = True
try:
    MIN_SECONDS = float(SETTINGS.get("min_seconds", MIN_SECONDS))
except (TypeError, ValueError, OverflowError):
    MIN_SECONDS = 0.3
if MIN_SECONDS not in MIN_RECORDING_OPTIONS:
    MIN_SECONDS = 0.3
DEFAULT_MICROPHONE = "default"
saved_input_device = SETTINGS.get("input_device", DEFAULT_MICROPHONE)
INPUT_DEVICE_SETTING_RESET = not (
    isinstance(saved_input_device, str) and saved_input_device.strip()
)
INPUT_DEVICE = (
    saved_input_device.strip()
    if isinstance(saved_input_device, str) and saved_input_device.strip()
    else DEFAULT_MICROPHONE
)
if INPUT_DEVICE_SETTING_RESET:
    SETTINGS["input_device"] = DEFAULT_MICROPHONE


def build_system_prompt() -> str:
    """Append vocabulary rules to the base transcription system prompt."""
    with SETTINGS_LOCK:
        vocab_items = sorted(VOCABULARY.items())
    if not vocab_items:
        return SYSTEM_PROMPT
    corrections = "\n".join(
        f"- Only when the transcript contains exactly the word {json.dumps(wrong)}, "
        f"replace it with {json.dumps(correct)}. Do not replace similar-sounding words."
        for wrong, correct in vocab_items
    )
    return f"{SYSTEM_PROMPT}\n\nUser vocabulary corrections:\n{corrections}"


def _enumerate_input_devices() -> list[dict[str, Any]]:
    """Return raw PortAudio inputs, including alternate APIs and virtual inputs."""
    try:
        hostapis = sd.query_hostapis()
        devices = []
        for index, device in enumerate(sd.query_devices()):
            try:
                max_input_channels = int(device["max_input_channels"])
                if max_input_channels <= 0:
                    continue
                hostapi_name = hostapis[device["hostapi"]]["name"]
                name = str(device["name"])
                default_samplerate = round(float(device["default_samplerate"]))
            except (IndexError, KeyError, TypeError, ValueError, OverflowError):
                log.info("Skipping incomplete input device entry %d", index)
                continue

            key = f"{hostapi_name} | {name}"
            devices.append(
                {
                    "index": index,
                    "key": key,
                    "name": name,
                    "hostapi": hostapi_name,
                    "label": f"{name} ({hostapi_name})",
                    "max_input_channels": max_input_channels,
                    "default_samplerate": default_samplerate,
                }
            )
        return devices
    except (sd.PortAudioError, OSError, RuntimeError, TypeError, ValueError):
        log.exception("Could not enumerate microphone devices")
        return []


MICROPHONE_API_PRIORITY = {
    "Windows WASAPI": 0,
    "Windows DirectSound": 1,
    "MME": 2,
    "Windows WDM-KS": 3,
}
GENERIC_INPUT_ROUTE_MARKERS = (
    "microsoft sound mapper",
    "primary sound capture driver",
    "stereo mix",
)


def _microphone_identity(name: str) -> str:
    """Normalise a Windows capture name across PortAudio host APIs."""
    normalized = str(name).casefold()
    host_api_labels = {label.casefold() for label in MICROPHONE_API_PRIORITY}
    if "bthhfenum.sys" in normalized:
        parenthesized_names = re.findall(r"\(([^()]*)\)", normalized)
        if parenthesized_names:
            normalized = next(
                (
                    candidate.strip()
                    for candidate in reversed(parenthesized_names)
                    if candidate.strip().casefold() not in host_api_labels
                ),
                parenthesized_names[-1],
            )
    else:
        for host_api in host_api_labels:
            suffix = f" ({host_api})"
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
    # Strip Windows endpoint enumeration prefixes such as "2- ".
    normalized = re.sub(r"^\s*\d+\s*-\s*", "", normalized)
    normalized = re.sub(
        r"\b(?:hands[\s-]*free|headphones?|headset|microphones?|stereo mix)\b",
        " ",
        normalized,
    )
    normalized = re.sub(r"\bpro\s+(\d+)\b", r"pro\1", normalized)
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE).replace("_", "")


def _same_microphone_name(first_name: str, second_name: str) -> bool:
    """Return whether two Windows route names refer to one physical input."""
    first_text = str(first_name).casefold().strip()
    second_text = str(second_name).casefold().strip()
    if first_text == second_text:
        return True
    if (
        "internal microphone" in first_text
        and "internal microphone" in second_text
        and "conexant" in first_text
        and "conexant" in second_text
    ):
        return True
    first = _microphone_identity(first_name)
    second = _microphone_identity(second_name)
    if not first or not second:
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) >= 8 and longer.startswith(shorter)


def _extract_friendly_name(device_obj: Any) -> str | None:
    """Extract the Windows friendly name from a pycaw audio device."""
    if device_obj is None:
        return None
    name = getattr(device_obj, "FriendlyName", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    try:
        wrapped = AudioUtilities.CreateDevice(device_obj)
        name = getattr(wrapped, "FriendlyName", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return None


def _windows_capture_device_names() -> list[str] | None:
    """Return active Windows capture endpoints, or None if Core Audio is unavailable."""
    try:
        with _com_scope():
            try:
                from pycaw.constants import DEVICE_STATE, EDataFlow

                devices = AudioUtilities.GetAllDevices(
                    data_flow=EDataFlow.eCapture.value,
                    device_state=DEVICE_STATE.ACTIVE.value,
                )
            except TypeError:
                # Older pycaw versions do not accept data_flow / device_state kwargs.
                return None
            names = []
            for device in devices:
                name = getattr(device, "FriendlyName", None)
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            return names
    except Exception:
        log.info("Could not read active Windows microphone endpoints", exc_info=True)
        return None


def _windows_default_microphone_name() -> str | None:
    """Read the current Windows system-default capture device each time."""
    try:
        with _com_scope():
            raw_mic = AudioUtilities.GetMicrophone()
            return _extract_friendly_name(raw_mic)
    except Exception:
        log.info("Could not read the Windows default microphone", exc_info=True)
        return None


def _is_generic_input_route(device: dict[str, Any]) -> bool:
    """Return whether a PortAudio entry is a generic mapper route."""
    name = str(device["name"]).casefold()
    return any(marker in name for marker in GENERIC_INPUT_ROUTE_MARKERS)


def _preferred_input_route(routes: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer WASAPI, then fall back through the other Windows APIs."""
    return min(
        routes,
        key=lambda device: (
            MICROPHONE_API_PRIORITY.get(device["hostapi"], 99),
            -int(
                device["default_samplerate"]
                if device["default_samplerate"] in ELEVENLABS_SUPPORTED_SAMPLE_RATES
                else 0
            ),
            -int(device["max_input_channels"]),
            device["index"],
        ),
    )


def list_input_devices() -> list[dict[str, Any]]:
    """Show one reliable route for each currently usable physical microphone."""
    devices = [
        device
        for device in _enumerate_input_devices()
        if not _is_generic_input_route(device)
    ]
    active_names = _windows_capture_device_names()
    if active_names is not None:
        active_devices = [
            device
            for device in devices
            if any(_same_microphone_name(device["name"], name) for name in active_names)
        ]
        if active_devices or not active_names:
            devices = active_devices
        else:
            log.warning(
                "No PortAudio microphones matched active Windows endpoints; showing raw inputs"
            )

    groups: list[list[dict]] = []
    for device in devices:
        for group in groups:
            if _same_microphone_name(device["name"], group[0]["name"]):
                group.append(device)
                break
        else:
            groups.append([device])

    result = []
    for group in groups:
        preferred = _preferred_input_route(group)
        friendly_name = next(
            (
                name
                for name in active_names or ()
                if _same_microphone_name(preferred["name"], name)
            ),
            preferred["name"],
        )
        result.append({**preferred, "label": friendly_name})
    return sorted(result, key=lambda device: device["label"].casefold())


# ---------------------------------------------------------------
# System / Window / Media helpers
# ---------------------------------------------------------------
def acquire_single_instance() -> bool:
    """Returns False if another instance already holds the mutex."""
    global MUTEX_HANDLE
    if os.name != "nt":
        raise OSError("STT currently supports Windows only")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create_mutex(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        close_handle(handle)
        return False
    MUTEX_HANDLE = handle
    return True


def release_single_instance() -> None:
    """Close this process's single-instance mutex handle during shutdown."""
    global MUTEX_HANDLE
    handle, MUTEX_HANDLE = MUTEX_HANDLE, None
    if handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def active_media_pids() -> set[int]:
    """Return PIDs of other processes currently producing audio."""
    pids: set[int] = set()
    try:
        own_pid = os.getpid()
        with _com_scope():
            for session in AudioUtilities.GetAllSessions():
                try:
                    process = session.Process
                    if process is not None and process.pid == own_pid:
                        continue
                    meter = session._ctl.QueryInterface(IAudioMeterInformation)
                    if meter.GetPeakValue() > 0.01:
                        if process is not None:
                            pids.add(int(process.pid))
                        else:
                            pids.add(-1)
                except Exception:
                    continue
    except Exception:
        log.info("Could not inspect background audio sessions", exc_info=True)
    return pids


def background_media_is_playing() -> bool:
    """Return whether another process is currently outputting audio."""
    return bool(active_media_pids())


def wait_for_background_media(expected_playing: bool, timeout: float = 0.35) -> bool:
    """Wait briefly for Windows media state to settle after a media command."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if background_media_is_playing() == expected_playing:
            return True
        time.sleep(0.03)
    return background_media_is_playing() == expected_playing


def find_media_window(pids: set[int]) -> int | None:
    """Locate a top-level window owned by an actively playing audio process."""
    valid_pids = {pid for pid in pids if pid > 0}
    if not valid_pids:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found_visible: list[int] = []
    found_any: list[int] = []
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def enum_callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in valid_pids:
            if user32.IsWindowVisible(hwnd):
                found_visible.append(int(hwnd))
                return False
            found_any.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc_type(enum_callback), 0)
    if found_visible:
        return found_visible[0]
    if found_any:
        return found_any[0]
    return None


def toggle_background_media(window: int | None) -> bool:
    """Toggle playback in the media window without blocking on hung windows."""
    if not window:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send_message_timeout = user32.SendMessageTimeoutW
    send_message_timeout.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    send_message_timeout.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t(0)
    command = APPCOMMAND_MEDIA_PLAY_PAUSE << 16
    ok = send_message_timeout(
        wintypes.HWND(window),
        WM_APPCOMMAND,
        0,
        command,
        SMTO_ABORTIFHUNG,
        300,
        ctypes.byref(result),
    )
    return bool(ok)


# ---------------------------------------------------------------
# State
# ---------------------------------------------------------------
class State(Enum):
    """Lifecycle states of the dictation state machine."""

    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    TRANSCRIBING = "transcribing"


# ---------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------
class MicrophoneUnavailableError(RuntimeError):
    """Raised when a specifically selected microphone is no longer usable."""


class RecordingBufferLimitError(RuntimeError):
    """Raised when a recording reaches the safe in-memory upload limit."""


def _downsample_int16_mono(
    samples: np.ndarray, src_rate: int, dst_rate: int
) -> np.ndarray:
    """Resample 1-D int16 audio down to dst_rate using NumPy only."""
    if src_rate <= dst_rate or samples.size == 0:
        return samples
    if src_rate % dst_rate == 0:
        factor = src_rate // dst_rate
        usable = (samples.size // factor) * factor
        if usable > 0:
            return (
                np.round(
                    samples[:usable].astype(np.float32).reshape(-1, factor).mean(axis=1)
                )
                .clip(-32768, 32767)
                .astype(np.int16)
            )
    target_len = max(1, int(round(samples.size * dst_rate / src_rate)))
    x_old = np.arange(samples.size, dtype=np.float64)
    x_new = np.linspace(0, samples.size - 1, target_len, dtype=np.float64)
    resampled = np.interp(x_new, x_old, samples.astype(np.float32))
    return np.round(resampled).clip(-32768, 32767).astype(np.int16)


class Recorder:
    """Capture from the microphone only while a recording is active."""

    def __init__(
        self, samplerate: int = SAMPLE_RATE, input_device: str = DEFAULT_MICROPHONE
    ) -> None:
        """Initialize audio capture buffers, locks, and device selection."""
        self.samplerate = samplerate
        self._active_samplerate = samplerate
        self._stream_samplerate = samplerate
        self._buffer_samplerate = min(samplerate, SAMPLE_RATE)
        self._input_device = input_device
        self._frames: list[bytes] = []
        self._buffered_bytes = 0
        self._buffer_limit_reached = False
        self._input_overflow_count = 0
        self._recording = False
        self._volume_level = 0.0
        self._audio_sink = None
        self._lock = threading.Lock()
        self._stream = None

    @staticmethod
    def _same_microphone(first_name: str, second_name: str) -> bool:
        """Match the same physical microphone across Windows host APIs."""
        return _same_microphone_name(first_name, second_name)

    def _resolve_input_device(
        self,
        devices: list[dict[str, Any]] | None = None,
        default_name: str | None = None,
        input_device: str | None = None,
    ) -> int:
        """Resolve the target PortAudio input device index for recording."""
        if devices is None:
            devices = _enumerate_input_devices()
        target_sel = self._input_device if input_device is None else input_device
        if target_sel == DEFAULT_MICROPHONE:
            if default_name:
                matching = [
                    device
                    for device in devices
                    if not _is_generic_input_route(device)
                    and self._same_microphone(default_name, device["name"])
                ]
                if matching:
                    selected = _preferred_input_route(matching)
                    log.info(
                        "Using Windows default microphone: %s via %s",
                        default_name,
                        selected["hostapi"],
                    )
                    return selected["index"]
                log.warning(
                    "Could not map Windows default microphone (%s) to PortAudio; falling back",
                    default_name,
                )

            # Prefer concrete WASAPI host default over generic sound mapper routes.
            try:
                wasapi = next(
                    (
                        hostapi
                        for hostapi in sd.query_hostapis()
                        if hostapi["name"] == "Windows WASAPI"
                    ),
                    None,
                )
                if wasapi is not None:
                    default_index = int(wasapi["default_input_device"])
                    selected = next(
                        (
                            device
                            for device in devices
                            if device["index"] == default_index
                            and not _is_generic_input_route(device)
                        ),
                        None,
                    )
                    if selected is not None:
                        log.info(
                            "Using Windows WASAPI host default microphone: %s",
                            selected["name"],
                        )
                        return selected["index"]
            except Exception:
                log.info(
                    "Could not resolve the Windows WASAPI default microphone",
                    exc_info=True,
                )

            # Fall back to PortAudio's global default input or any non-generic route.
            try:
                pa_default = sd.default.device[0]
                if pa_default is not None and int(pa_default) >= 0:
                    selected = next(
                        (
                            device
                            for device in devices
                            if device["index"] == int(pa_default)
                        ),
                        None,
                    )
                    if selected is not None:
                        log.info(
                            "Using PortAudio default microphone: %s",
                            selected["key"],
                        )
                        return selected["index"]
            except Exception:
                pass

            non_generic = [d for d in devices if not _is_generic_input_route(d)]
            if non_generic:
                preferred = _preferred_input_route(non_generic)
                log.info("Falling back to available microphone: %s", preferred["key"])
                return preferred["index"]
            if devices:
                return devices[0]["index"]

            raise MicrophoneUnavailableError(
                "No usable microphone was found. Connect a microphone and try again."
            )

        selected = next(
            (device for device in devices if device["key"] == target_sel),
            None,
        )
        if selected is None:
            saved_name = target_sel.split(" | ", 1)[-1]
            selected = next(
                (
                    device
                    for device in devices
                    if self._same_microphone(saved_name, device["name"])
                ),
                None,
            )
        if selected is None:
            raise MicrophoneUnavailableError(
                "Selected microphone is unavailable. Reconnect it or choose System default."
            )

        matching = [
            device
            for device in devices
            if not _is_generic_input_route(device)
            and self._same_microphone(selected["name"], device["name"])
        ]
        preferred = _preferred_input_route(matching) if matching else selected
        if preferred["key"] != selected["key"]:
            log.info(
                "Using the best route for selected microphone: %s",
                preferred["key"],
            )
        return preferred["index"]

    def _input_device_candidates(
        self,
        target_device: int | None,
        devices: list[dict[str, Any]] | None = None,
        default_name: str | None = None,
        input_device: str | None = None,
    ) -> list[int | None]:
        """Return the chosen device plus same-microphone alternate APIs."""
        if devices is None:
            devices = _enumerate_input_devices()
        target_sel = self._input_device if input_device is None else input_device
        if target_device is None or target_device < 0:
            return []

        primary = next(
            (device for device in devices if device["index"] == target_device),
            None,
        )
        if primary is None:
            return [target_device]

        candidates = [target_device]
        used_indices = {target_device}
        matching = sorted(
            (
                device
                for device in devices
                if self._same_microphone(primary["name"], device["name"])
                and device["index"] not in used_indices
            ),
            key=lambda device: (
                MICROPHONE_API_PRIORITY.get(device["hostapi"], 99),
                -int(
                    device["default_samplerate"]
                    if device["default_samplerate"] in ELEVENLABS_SUPPORTED_SAMPLE_RATES
                    else 0
                ),
                device["index"],
            ),
        )
        for device in matching:
            candidates.append(device["index"])
            used_indices.add(device["index"])

        if target_sel == DEFAULT_MICROPHONE:
            try:
                hostapis = sorted(
                    sd.query_hostapis(),
                    key=lambda hostapi: MICROPHONE_API_PRIORITY.get(
                        hostapi["name"], 99
                    ),
                )
                devices_by_index = {device["index"]: device for device in devices}
                for hostapi in hostapis:
                    try:
                        index = int(hostapi["default_input_device"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    device = devices_by_index.get(index)
                    if device is None or index in used_indices:
                        continue
                    if _is_generic_input_route(device):
                        continue
                    candidates.append(index)
                    used_indices.add(index)
            except (sd.PortAudioError, OSError, RuntimeError, TypeError, ValueError):
                log.info(
                    "Could not enumerate Windows host-default microphone routes",
                    exc_info=True,
                )
            # Last resort: PortAudio global default (None) in case specific device indices fail.
            candidates.append(None)
        return candidates

    def set_input_device(self, input_device: str) -> bool:
        """Update the preferred input device when not recording."""
        with self._lock:
            if self._recording:
                return False
            self._input_device = input_device
        return True

    def _candidate_sample_rates(self, device_index: int | None) -> list[int]:
        """Build ordered sample rates supported by the device and Scribe."""
        rates = []
        try:
            idx = sd.default.device[0] if device_index is None else device_index
            if idx is not None and idx >= 0:
                device = sd.query_devices(idx)
                native_rate = round(float(device["default_samplerate"]))
                if native_rate in ELEVENLABS_SUPPORTED_SAMPLE_RATES:
                    rates.append(native_rate)
                elif native_rate > 0:
                    compatible = min(
                        ELEVENLABS_SUPPORTED_SAMPLE_RATES,
                        key=lambda rate: (abs(rate - native_rate), -rate),
                    )
                    rates.extend([compatible, native_rate])
        except Exception:
            pass
        for fallback in (48000, 44100, 16000):
            if fallback not in rates:
                rates.append(fallback)
        return rates

    def _candidate_channel_counts(self, device_index: int | None) -> list[int]:
        """Build ordered channel counts to try when opening the stream."""
        counts = [1]
        try:
            idx = sd.default.device[0] if device_index is None else device_index
            if idx is not None and idx >= 0:
                max_ch = max(1, int(sd.query_devices(idx)["max_input_channels"]))
                for ch in (min(2, max_ch), max_ch):
                    if ch not in counts:
                        counts.append(ch)
        except Exception:
            pass
        return counts

    def _callback(
        self, indata: np.ndarray, _frames: int, _time_info: Any, status: Any
    ) -> None:
        """Downmix, resample, meter, and buffer incoming audio frames."""
        with self._lock:
            if not self._recording or self._buffer_limit_reached:
                return
            sink = self._audio_sink
            stream_rate = self._stream_samplerate
            active_rate = self._active_samplerate
            buffer_rate = self._buffer_samplerate
            if bool(getattr(status, "input_overflow", False)):
                self._input_overflow_count += 1

        samples = indata.astype(np.float32)
        if samples.ndim > 1:
            if samples.shape[1] == 1:
                samples = samples[:, 0]
            else:
                # If channel 1 has signal and channel 2 is silent/unplugged, avoid halving gain.
                ch0 = samples[:, 0]
                ch_rest = samples[:, 1:]
                rms0 = float(np.sqrt(np.mean(ch0 * ch0)))
                rms_rest = float(np.sqrt(np.mean(ch_rest * ch_rest)))
                if rms_rest < max(5.0, rms0 * 0.1):
                    samples = ch0
                elif rms0 < max(5.0, rms_rest * 0.1):
                    samples = ch_rest[:, 0]
                else:
                    samples = np.mean(samples, axis=1)

        mono_samples = np.clip(samples, -32768, 32767).astype(np.int16)
        if stream_rate != active_rate:
            mono_samples = _downsample_int16_mono(
                mono_samples, stream_rate, active_rate
            )

        pcm = mono_samples.tobytes()
        rms = float(np.sqrt(np.mean(mono_samples.astype(np.float32) ** 2)))
        vol = min(1.0, rms / 6000.0)
        stored_samples = (
            _downsample_int16_mono(mono_samples, active_rate, buffer_rate)
            if active_rate != buffer_rate
            else mono_samples
        )
        stored_pcm = stored_samples.tobytes()

        with self._lock:
            if not self._recording:
                return
            self._volume_level = vol
            if self._buffered_bytes + len(stored_pcm) > MAX_RECORDING_BUFFER_BYTES:
                self._buffer_limit_reached = True
                self._volume_level = 0.0
                return
            self._frames.append(stored_pcm)
            self._buffered_bytes += len(stored_pcm)

        if sink is not None:
            try:
                sink(pcm, active_rate)
            except Exception:
                log.exception("Could not queue audio for live transcription")

    def set_audio_sink(self, audio_sink: Callable[[bytes, int], Any] | None) -> bool:
        """Set a non-blocking PCM consumer before a recording starts."""
        with self._lock:
            if self._recording:
                return False
            self._audio_sink = audio_sink
        return True

    def volume_level(self) -> float:
        """Return the latest normalized input RMS volume level."""
        with self._lock:
            return self._volume_level

    def start(self) -> None:
        """Open and start the best compatible PortAudio input stream."""
        with self._lock:
            input_device = self._input_device
            old_stream, self._stream = self._stream, None
            self._frames = []
            self._buffered_bytes = 0
            self._buffer_limit_reached = False
            self._input_overflow_count = 0
            self._volume_level = 0.0
            self._recording = False

        if old_stream is not None:
            self._close_stream(old_stream)

        default_name = (
            _windows_default_microphone_name()
            if input_device == DEFAULT_MICROPHONE
            else None
        )
        devices = _enumerate_input_devices()
        target_device = self._resolve_input_device(devices, default_name, input_device)
        devices_by_index = {device["index"]: device for device in devices}
        candidates = self._input_device_candidates(
            target_device, devices, default_name, input_device
        )

        last_error = None
        for candidate_device in candidates:
            sample_rates = self._candidate_sample_rates(candidate_device)
            channel_counts = self._candidate_channel_counts(candidate_device)
            candidate = (
                devices_by_index.get(candidate_device)
                if candidate_device is not None
                else None
            )
            route_name = candidate["key"] if candidate else "PortAudio default"

            for sample_rate in sample_rates:
                # Stream out at an ElevenLabs-supported rate if native > 48 kHz.
                if sample_rate in ELEVENLABS_SUPPORTED_SAMPLE_RATES:
                    active_rate = sample_rate
                else:
                    active_rate = min(
                        ELEVENLABS_SUPPORTED_SAMPLE_RATES,
                        key=lambda r: (abs(r - min(sample_rate, 48000)), -r),
                    )
                for channel_count in channel_counts:
                    log.info(
                        "Microphone stream attempt: selection=%s device=%s index=%s rate=%d->%d channels=%d",
                        input_device,
                        route_name,
                        candidate_device,
                        sample_rate,
                        active_rate,
                        channel_count,
                    )
                    stream = None
                    try:
                        stream = sd.InputStream(
                            samplerate=sample_rate,
                            channels=channel_count,
                            dtype="int16",
                            blocksize=0,
                            latency="low",
                            device=candidate_device,
                            callback=self._callback,
                        )
                        with self._lock:
                            self._frames = []
                            self._stream = stream
                            self._stream_samplerate = sample_rate
                            self._active_samplerate = active_rate
                            self._buffer_samplerate = min(active_rate, SAMPLE_RATE)
                            self._buffered_bytes = 0
                            self._buffer_limit_reached = False
                            self._input_overflow_count = 0
                            self._recording = True
                        stream.start()
                        if candidate_device != target_device:
                            log.info(
                                "Using the same microphone through alternate device_index=%s",
                                candidate_device,
                            )
                        return
                    except (
                        sd.PortAudioError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as exc:
                        last_error = exc
                        with self._lock:
                            self._recording = False
                            self._stream = None
                        if stream is not None:
                            self._close_stream(stream)
                        log.info(
                            "Microphone route failed on device=%s index=%s rate=%d ch=%d (%s)",
                            route_name,
                            candidate_device,
                            sample_rate,
                            channel_count,
                            type(exc).__name__,
                        )

        if input_device != DEFAULT_MICROPHONE:
            raise MicrophoneUnavailableError(
                "Selected microphone could not be opened. Reconnect it or choose System default."
            ) from last_error
        raise MicrophoneUnavailableError(
            "Windows' default microphone could not be opened. Check your input device in Sound Settings."
        ) from last_error

    @staticmethod
    def _close_stream(stream: "sd.InputStream") -> None:
        """Stop and close a PortAudio input stream safely."""
        try:
            stream.stop()
        except Exception:
            log.exception("Could not stop microphone stream")
        try:
            stream.close()
        except Exception:
            log.exception("Could not close microphone stream")

    def stop(self) -> tuple[bytes | None, float]:
        """Returns (wav_bytes, seconds); wav_bytes is None if too short."""
        with self._lock:
            self._recording = False
            self._volume_level = 0.0
            self._audio_sink = None
            stream, self._stream = self._stream, None
            buffer_rate = self._buffer_samplerate
            limit_reached = self._buffer_limit_reached
            overflow_count = self._input_overflow_count
        if stream is not None:
            self._close_stream(stream)
        with self._lock:
            frames, self._frames = self._frames, []
            self._buffered_bytes = 0
            self._buffer_limit_reached = False
            self._input_overflow_count = 0
        if overflow_count:
            log.warning(
                "Microphone input reported %d overflow event(s)", overflow_count
            )
        if limit_reached:
            raise RecordingBufferLimitError(
                "Recording reached the 24 MiB safety limit. Please use a shorter clip."
            )
        pcm = b"".join(frames)
        seconds = len(pcm) / 2 / buffer_rate if buffer_rate else 0.0
        if seconds < MIN_SECONDS:
            return None, seconds
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(buffer_rate)
            wf.writeframes(pcm)
        return buf.getvalue(), seconds

    def abort(self) -> None:
        """Stop capture and discard every buffered frame without creating WAV data."""
        with self._lock:
            self._recording = False
            self._volume_level = 0.0
            self._audio_sink = None
            stream, self._stream = self._stream, None
            self._frames = []
            self._buffered_bytes = 0
            self._buffer_limit_reached = False
            self._input_overflow_count = 0
        if stream is not None:
            self._close_stream(stream)
        with self._lock:
            self._frames = []

    def close(self) -> None:
        """Stop capture and release the active PortAudio input stream."""
        with self._lock:
            self._recording = False
            self._audio_sink = None
            stream, self._stream = self._stream, None
            self._frames = []
            self._buffered_bytes = 0
            self._buffer_limit_reached = False
            self._input_overflow_count = 0
        if stream is not None:
            self._close_stream(stream)


# ---------------------------------------------------------------
# ElevenLabs Realtime
# ---------------------------------------------------------------
class LiveGeminiPolisher:
    """Polish cumulative ASR snapshots and reuse a result matching final words."""

    def __init__(self, polish_callback: Callable[[str], str]) -> None:
        """Initialize state and start the background live-polish thread."""
        self._polish_callback = polish_callback
        self._condition = threading.Condition()
        self._pending_text = None
        self._pending_signature = None
        self._pending_deadline = 0.0
        self._inflight_signature = None
        self._last_call_started = 0.0
        self._results = {}
        self._accept_updates = True
        self._cancelled = False
        self._shutdown = False
        self._final_signature = None
        self._final_waiting = False
        self._final_done = False
        self._final_result = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gemini-live-polish"
        )
        self._thread.start()

    @staticmethod
    def _signature(transcript: str) -> str:
        """Normalize transcript whitespace and case into a cache key."""
        # Preserve punctuation and symbols while normalizing whitespace/case.
        return re.sub(r"\s+", " ", transcript.strip().casefold())

    def submit(self, transcript: str) -> None:
        """Offer the latest cumulative ASR snapshot without blocking Scribe."""
        transcript = transcript.strip()
        signature = self._signature(transcript)
        if not signature:
            return
        with self._condition:
            if not self._accept_updates or self._cancelled or self._shutdown:
                return
            if (
                signature in self._results
                or signature == self._pending_signature
                or signature == self._inflight_signature
            ):
                return
            self._pending_text = transcript
            self._pending_signature = signature
            self._pending_deadline = (
                time.monotonic() + LIVE_GEMINI_POLISH_DEBOUNCE_SECONDS
            )
            self._condition.notify_all()

    def polish_final(self, transcript: str) -> str | None:
        """Reuse matching live polish; leave changed final text to normal polish."""
        transcript = transcript.strip()
        signature = self._signature(transcript)
        if not signature:
            return None

        deadline = time.monotonic() + GEMINI_POLISH_TIMEOUT_SECONDS + 2
        with self._condition:
            self._accept_updates = False
            if self._cancelled or self._shutdown:
                return None
            cached = self._results.get(signature)
            if cached:
                self._shutdown = True
                self._pending_text = None
                self._pending_signature = None
                self._condition.notify_all()
                log.info(
                    "Reusing live Gemini polish for final transcript (%d chars)",
                    len(transcript),
                )
                return cached

            # Fuzzy match: reuse a cached polish when the final text
            # is very similar to an already-polished interim snapshot.
            if self._results:
                best_ratio = 0.0
                best_result = None
                for cached_sig, cached_result in self._results.items():
                    ratio = difflib.SequenceMatcher(
                        None, signature, cached_sig
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_result = cached_result
                if best_result and best_ratio >= LIVE_POLISH_FUZZY_THRESHOLD:
                    self._shutdown = True
                    self._pending_text = None
                    self._pending_signature = None
                    self._condition.notify_all()
                    log.info(
                        "Reusing fuzzy-matched live Gemini polish "
                        "(%.0f%% similar, %d chars)",
                        best_ratio * 100,
                        len(transcript),
                    )
                    return best_result

            if self._inflight_signature != signature:
                self._shutdown = True
                self._pending_text = None
                self._pending_signature = None
                self._condition.notify_all()
                return None

            self._final_signature = signature
            self._final_waiting = True
            self._pending_text = None
            self._pending_signature = None
            self._condition.notify_all()
            while not self._final_done and not self._cancelled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._shutdown = True
                    self._condition.notify_all()
                    log.warning("Timed out waiting for live Gemini polish")
                    return None
                self._condition.wait(remaining)
            return self._final_result

    def cancel(self) -> None:
        """Cancel pending or in-flight incremental polish requests."""
        with self._condition:
            self._cancelled = True
            self._shutdown = True
            self._pending_text = None
            self._pending_signature = None
            self._condition.notify_all()

    def _run(self) -> None:
        """Debounce and rate-limit incremental Gemini polish calls."""
        while True:
            with self._condition:
                while (
                    self._pending_text is None
                    and not self._cancelled
                    and not self._shutdown
                ):
                    self._condition.wait()
                if self._cancelled or self._shutdown:
                    return

                while True:
                    if self._cancelled or self._shutdown:
                        return
                    now = time.monotonic()
                    ready_at = self._pending_deadline
                    if self._last_call_started:
                        ready_at = max(
                            ready_at,
                            self._last_call_started
                            + LIVE_GEMINI_POLISH_MIN_INTERVAL_SECONDS,
                        )
                    remaining = ready_at - now
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                    if self._pending_text is None:
                        break

                if self._cancelled or self._shutdown:
                    return
                if self._pending_text is None:
                    continue

                transcript = self._pending_text
                signature = self._pending_signature
                self._pending_text = None
                self._pending_signature = None
                self._inflight_signature = signature
                self._last_call_started = time.monotonic()

            polished = None
            try:
                candidate = self._polish_callback(transcript)
                if isinstance(candidate, str) and candidate.strip():
                    polished = candidate.strip()
            except Exception:
                log.warning(
                    "Incremental Gemini polish failed; final transcript will use fallback",
                    exc_info=True,
                )

            with self._condition:
                self._inflight_signature = None
                if self._cancelled:
                    return
                if polished:
                    self._results[signature] = polished
                    while len(self._results) > LIVE_GEMINI_POLISH_CACHE_LIMIT:
                        self._results.pop(next(iter(self._results)))
                if self._final_waiting:
                    self._final_result = (
                        polished if signature == self._final_signature else None
                    )
                    self._final_done = True
                    self._shutdown = True
                    self._pending_text = None
                    self._pending_signature = None
                    self._condition.notify_all()
                    return
                if self._shutdown:
                    return


class LiveScribeSession:
    """Stream audio to ElevenLabs and interim transcript snapshots to Gemini."""

    _STOP = object()
    _SUPPORTED_SAMPLE_RATES = ELEVENLABS_SUPPORTED_SAMPLE_RATES
    _ERROR_TYPES = frozenset(
        {
            "error",
            "auth_error",
            "quota_exceeded",
            "unaccepted_terms",
            "rate_limited",
            "queue_overflow",
            "resource_exhausted",
            "session_time_limit_exceeded",
            "chunk_size_exceeded",
            "insufficient_audio_activity",
            "transcriber_error",
            "input_error",
            "invalid_request",
            "commit_throttled",
            "scribe_error",
            "scribe_auth_error",
            "scribe_quota_exceeded_error",
            "scribe_throttled_error",
            "scribe_unaccepted_terms_error",
            "scribe_rate_limited_error",
            "scribe_queue_overflow_error",
            "scribe_resource_exhausted_error",
            "scribe_session_time_limit_exceeded_error",
            "scribe_input_error",
            "scribe_invalid_request_error",
            "scribe_chunk_size_exceeded_error",
            "scribe_insufficient_audio_activity_error",
            "scribe_transcriber_error",
        }
    )

    def __init__(
        self, api_key: str, polish_callback: Callable[[str], str] | None = None
    ) -> None:
        """Initialize queues, state flags, and start the WebSocket thread."""
        self._api_key = api_key
        self._gemini_polisher = (
            LiveGeminiPolisher(polish_callback) if polish_callback else None
        )
        self._audio_queue: queue.Queue[bytes | object] = queue.Queue(
            maxsize=LIVE_AUDIO_QUEUE_SIZE
        )
        self._input_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._pending_audio = bytearray()
        self._sample_rate: int | None = None
        self._stop_requested = threading.Event()
        self._cancelled = threading.Event()
        self._closed = threading.Event()
        self._failed = threading.Event()
        self._state_lock = threading.Lock()
        self._failure_reason = None
        self._committed: list[str] = []
        self._partial_text = ""
        self._commit_requests = 0
        self._commit_events = 0
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="elevenlabs-stream"
        )
        self._thread.start()

    def push_audio(self, pcm: bytes, sample_rate: int, *, block: bool = False) -> bool:
        """Queue roughly 100 ms chunks; keep microphone callbacks non-blocking."""
        if (
            not pcm
            or self._stop_requested.is_set()
            or self._cancelled.is_set()
            or self._failed.is_set()
        ):
            return False
        if sample_rate not in self._SUPPORTED_SAMPLE_RATES or len(pcm) % 2:
            self._mark_failed("unsupported audio format")
            return False

        chunk_bytes = max(2, int(sample_rate * LIVE_AUDIO_CHUNK_SECONDS) * 2)
        with self._input_lock:
            if self._stop_requested.is_set() or self._failed.is_set():
                return False
            if self._sample_rate is None:
                self._sample_rate = sample_rate
            elif self._sample_rate != sample_rate:
                self._mark_failed("microphone sample rate changed")
                return False

            self._pending_audio.extend(pcm)
            while len(self._pending_audio) >= chunk_bytes:
                chunk = bytes(self._pending_audio[:chunk_bytes])
                del self._pending_audio[:chunk_bytes]
                if block:
                    while not self._failed.is_set() and not self._cancelled.is_set():
                        try:
                            self._audio_queue.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    else:
                        return False
                else:
                    try:
                        self._audio_queue.put_nowait(chunk)
                    except queue.Full:
                        self._mark_failed("audio queue filled")
                        return False
            return not self._failed.is_set() and not self._cancelled.is_set()

    def finish(self) -> str | None:
        """Commit the final segment and return text, or signal caller to retry."""
        with self._finish_lock:
            if not self._stop_requested.is_set():
                self._stop_requested.set()
                with self._input_lock:
                    if self._pending_audio and not self._failed.is_set():
                        try:
                            self._audio_queue.put_nowait(bytes(self._pending_audio))
                        except queue.Full:
                            self._mark_failed("audio queue filled while finishing")
                        self._pending_audio.clear()
                    try:
                        if self._failed.is_set():
                            self._audio_queue.put_nowait(self._STOP)
                        else:
                            self._audio_queue.put(self._STOP, timeout=0.5)
                    except queue.Full:
                        self._mark_failed("could not finish the audio stream")

        wait_seconds = LIVE_CONNECT_TIMEOUT_SECONDS + LIVE_FINALIZE_TIMEOUT_SECONDS + 2
        if not self._closed.wait(wait_seconds):
            self._mark_failed("final transcript timed out")
            self._cancelled.set()
            return None
        if self._failed.is_set() or self._cancelled.is_set():
            return None
        with self._state_lock:
            transcript = " ".join(self._committed).strip()
        return transcript or None

    def polish_transcript(self, transcript: str) -> str | None:
        """Return a matching live-polished transcript if available."""
        if self._gemini_polisher is None:
            return None
        return self._gemini_polisher.polish_final(transcript)

    def cancel(self) -> None:
        """Stop streaming when recording is cancelled or the app exits."""
        self._cancelled.set()
        self._stop_requested.set()
        if self._gemini_polisher is not None:
            self._gemini_polisher.cancel()
        with contextlib.suppress(queue.Full):
            self._audio_queue.put_nowait(self._STOP)

    def _mark_failed(self, reason: str) -> None:
        """Record the first failure reason and flag the session as failed."""
        with self._state_lock:
            if not self._failed.is_set():
                self._failure_reason = reason
                self._failed.set()

    def _handle_message(self, raw_message: str) -> None:
        """Process an incoming ElevenLabs realtime WebSocket JSON event."""
        try:
            message = json.loads(raw_message)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid realtime response") from exc
        if not isinstance(message, dict):
            return

        message_type = message.get("message_type")
        if message_type in (
            "committed_transcript",
            "committed_transcript_with_timestamps",
        ):
            text = message.get("text")
            with self._state_lock:
                self._commit_events += 1
                if isinstance(text, str) and text.strip():
                    self._committed.append(text.strip())
                self._partial_text = ""
                transcript = " ".join((*self._committed, self._partial_text)).strip()
            if transcript and self._gemini_polisher is not None:
                self._gemini_polisher.submit(transcript)
        elif message_type == "partial_transcript":
            text = message.get("text")
            if isinstance(text, str):
                with self._state_lock:
                    self._partial_text = text.strip()
                    transcript = " ".join(
                        (*self._committed, self._partial_text)
                    ).strip()
                if transcript and self._gemini_polisher is not None:
                    self._gemini_polisher.submit(transcript)
        elif message_type in self._ERROR_TYPES:
            self._mark_failed(f"ElevenLabs realtime {message_type}")
        elif message_type == "warning":
            log.info("ElevenLabs realtime returned a session warning")

    def _receive_message(
        self, connection: Any, websocket_module: Any, wait_seconds: float = 0.0
    ) -> bool:
        """Read one available server event without slowing outbound audio sends."""
        socket = getattr(connection, "sock", None)
        if socket is not None:
            try:
                pending = getattr(socket, "pending", None)
                has_buffered_data = callable(pending) and pending() > 0
                if not has_buffered_data:
                    readable, _, _ = select.select(
                        (socket,), (), (), max(0.0, wait_seconds)
                    )
                    if not readable:
                        return False
            except (OSError, TypeError, ValueError):
                pass
        try:
            raw_message = connection.recv()
        except websocket_module.WebSocketTimeoutException:
            return False
        if not raw_message:
            raise RuntimeError("realtime connection closed")
        self._handle_message(raw_message)
        return True

    @staticmethod
    def _send_audio(
        connection: Any, pcm: bytes, sample_rate: int, commit: bool = False
    ) -> None:
        """Send a base64-encoded PCM audio chunk over the WebSocket."""
        payload = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": sample_rate,
        }
        if commit:
            payload["commit"] = True
        connection.send(json.dumps(payload))

    def _register_commit(self) -> int:
        """Increment and return the count of sent commit requests."""
        with self._state_lock:
            self._commit_requests += 1
            return self._commit_requests

    def _run(self) -> None:
        """Stream queued audio chunks to ElevenLabs and collect text."""
        connection = None
        try:
            first_chunk = None
            while not self._cancelled.is_set() and not self._failed.is_set():
                try:
                    first_chunk = self._audio_queue.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            if first_chunk is self._STOP or first_chunk is None:
                return

            sample_rate = self._sample_rate
            if sample_rate not in self._SUPPORTED_SAMPLE_RATES:
                raise RuntimeError("unsupported microphone sample rate")
            try:
                import websocket
            except ImportError as exc:
                raise RuntimeError("websocket-client is not installed") from exc

            parameters = urlencode(
                {
                    "model_id": ELEVENLABS_REALTIME_MODEL_ID,
                    "audio_format": f"pcm_{sample_rate}",
                    "commit_strategy": "manual",
                    "no_verbatim": "true",
                }
            )
            connection = websocket.create_connection(
                f"{ELEVENLABS_REALTIME_API_URL}?{parameters}",
                header=[f"xi-api-key: {self._api_key}"],
                timeout=LIVE_CONNECT_TIMEOUT_SECONDS,
            )
            connection.settimeout(LIVE_SOCKET_TIMEOUT_SECONDS)

            pending_chunk = first_chunk
            sent_since_commit = 0
            commit_interval_bytes = int(sample_rate * 2 * LIVE_COMMIT_INTERVAL_SECONDS)
            final_commit_target = None
            final_deadline = None

            while not self._cancelled.is_set() and not self._failed.is_set():
                if final_commit_target is None:
                    try:
                        next_chunk = self._audio_queue.get(timeout=0.02)
                    except queue.Empty:
                        next_chunk = None

                    if next_chunk is self._STOP:
                        if pending_chunk is None:
                            pending_chunk = b""
                        self._send_audio(
                            connection, pending_chunk, sample_rate, commit=True
                        )
                        pending_chunk = None
                        final_commit_target = self._register_commit()
                        final_deadline = (
                            time.monotonic() + LIVE_FINALIZE_TIMEOUT_SECONDS
                        )
                    elif isinstance(next_chunk, bytes):
                        if pending_chunk is not None:
                            sent_since_commit += len(pending_chunk)
                            commit = sent_since_commit >= commit_interval_bytes
                            self._send_audio(
                                connection, pending_chunk, sample_rate, commit
                            )
                            pending_chunk = next_chunk
                            if commit:
                                sent_since_commit = 0
                                self._register_commit()

                wait_poll = (
                    LIVE_FINAL_RESPONSE_POLL_SECONDS
                    if final_commit_target is not None
                    else 0.0
                )
                if self._receive_message(connection, websocket, wait_poll):
                    while (
                        not self._cancelled.is_set()
                        and not self._failed.is_set()
                        and self._receive_message(connection, websocket, 0.0)
                    ):
                        pass

                if final_commit_target is not None:
                    with self._state_lock:
                        committed = self._commit_events >= final_commit_target
                    if committed:
                        break
                    if time.monotonic() >= final_deadline:
                        self._mark_failed("final transcript timed out")

        except Exception as exc:  # noqa: BLE001 - retry saved audio after stream failure
            detail = str(exc).strip().replace("\n", " ")[:160]
            reason = (
                type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
            )
            self._mark_failed(reason)
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
            self._closed.set()
            if self._failed.is_set() and not self._cancelled.is_set():
                with self._state_lock:
                    reason = self._failure_reason or "stream failed"
                log.warning(
                    "ElevenLabs Realtime unavailable (%s); caller will retry saved audio",
                    reason,
                )


# ---------------------------------------------------------------
# ElevenLabs batch fallback
# ---------------------------------------------------------------
class ElevenLabsBatchError(RuntimeError):
    """A compact, user-safe description of a batch fallback failure."""

    def __init__(self, status_code: int | None, message: str) -> None:
        """Store the HTTP status code and user-safe error message."""
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------
class Transcriber:
    """Send captured audio to the selected Gemini, Groq, or ElevenLabs backend."""

    def __init__(self, api_key: str, groq_api_key: str, elevenlabs_api_key: str) -> None:
        """Store provider API keys and initialize lazy client handles."""
        self._gemini_api_key = api_key
        self._groq_api_key = groq_api_key
        self._elevenlabs_api_key = elevenlabs_api_key
        self._types = None
        self._client = None
        self._prefetch_client = None
        self._groq_client = None
        self._client_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._selected_model = PREFERRED_MODEL

    def get_model(self) -> str:
        """Return the currently selected transcription model identifier."""
        with self._model_lock:
            return self._selected_model

    def set_model(self, model: str) -> None:
        """Set the active transcription model identifier."""
        with self._model_lock:
            self._selected_model = model
        log.info("Model selected from tray: %s", model)

    def _create_gemini_client(self) -> "genai.Client":
        """Instantiate a configured Google GenAI client."""
        if not self._gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Gemini Python package is not installed") from exc
        self._types = types
        return genai.Client(
            api_key=self._gemini_api_key,
            http_options=types.HttpOptions(
                timeout=GEMINI_POLISH_TIMEOUT_SECONDS * 1000
            ),
        )

    def _get_gemini_client(self) -> "genai.Client":
        """Return or lazily create the main Gemini client."""
        with self._client_lock:
            if self._client is None:
                self._client = self._create_gemini_client()
            return self._client

    def _get_prefetch_gemini_client(self) -> "genai.Client":
        """Return or lazily create the live-polish Gemini client."""
        with self._client_lock:
            if self._prefetch_client is None:
                self._prefetch_client = self._create_gemini_client()
            return self._prefetch_client

    def _get_groq_client(self) -> "Groq":
        """Return or lazily create the Groq API client."""
        with self._client_lock:
            if self._groq_client is not None:
                return self._groq_client
            if not self._groq_api_key:
                raise RuntimeError("GROQ_API_KEY is not configured")
            try:
                from groq import Groq
            except ImportError as exc:
                raise RuntimeError("Groq Python package is not installed") from exc
            self._groq_client = Groq(
                api_key=self._groq_api_key,
                timeout=max(30.0, TRANSCRIPTION_TIMEOUT_SECONDS - 10.0),
            )
            return self._groq_client

    def _detach_clients_locked(self) -> tuple[Any, Any, Any]:
        """Clear and return cached SDK clients while holding _client_lock."""
        clients = (self._client, self._prefetch_client, self._groq_client)
        self._client = None
        self._prefetch_client = None
        self._groq_client = None
        return clients

    @staticmethod
    def _close_clients(clients: tuple[Any, Any, Any]) -> None:
        """Close any detached SDK client instances best-effort."""
        for client in clients:
            if client is not None and hasattr(client, "close"):
                with contextlib.suppress(Exception):
                    client.close()

    def update_api_keys(
        self, api_key: str, groq_api_key: str, elevenlabs_api_key: str
    ) -> None:
        """Atomically replace stored provider keys and close cached SDK clients."""
        with self._client_lock:
            clients = self._detach_clients_locked()
            self._gemini_api_key = api_key
            self._groq_api_key = groq_api_key
            self._elevenlabs_api_key = elevenlabs_api_key
        self._close_clients(clients)

    def close(self) -> None:
        """Close any initialized Gemini and Groq API clients."""
        with self._client_lock:
            clients = self._detach_clients_locked()
        self._close_clients(clients)

    def prepare_backend(self) -> str:
        """Initialize only the selected provider clients without network lookups."""
        model = self.get_model()
        try:
            if model == ELEVENLABS_MODEL:
                if not self._elevenlabs_api_key:
                    raise RuntimeError("ELEVENLABS_API_KEY is not configured")
                if not self._gemini_api_key:
                    raise RuntimeError(
                        "GEMINI_API_KEY is required for English polishing"
                    )
                self._get_gemini_client()
                log.info(
                    "Prepared ElevenLabs plus Gemini English polish (%s)",
                    GEMINI_ENGLISH_POLISH_MODEL,
                )
            elif model == GROQ_MODEL:
                self._get_groq_client()
                log.info("Prepared Groq model: %s", GROQ_MODEL)
            else:
                self._get_gemini_client()
                log.info("Prepared Gemini model: %s", model)
        except Exception:
            log.exception("Could not prepare selected model: %s", model)
        return model

    @staticmethod
    def _build_gemini_config(
        types: Any, model: str, system_instruction: str | None, max_tokens: int
    ) -> "types.GenerateContentConfig":
        """Build deterministic GenerateContentConfig options for Gemini."""
        kwargs = {
            "temperature": 0,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _build_elevenlabs_batch_request(wav: bytes) -> tuple[bytes, str]:
        """Build the small multipart upload used only after Realtime fails."""
        boundary = f"----STT{uuid.uuid4().hex}"
        body = bytearray()

        def add_field(name: str, value: str):
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        add_field("model_id", ELEVENLABS_BATCH_MODEL_ID)
        add_field("no_verbatim", "true")
        add_field("tag_audio_events", "false")
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
        )
        body.extend(b"Content-Type: audio/wav\r\n\r\n")
        body.extend(wav)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("ascii"))
        return bytes(body), f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _elevenlabs_batch_error_detail(response_body: bytes) -> str:
        """Return a concise server message without leaking request details."""
        try:
            payload = json.loads(response_body.decode("utf-8", errors="replace"))
        except (AttributeError, TypeError, ValueError):
            return "The service returned an unreadable error response."
        if not isinstance(payload, dict):
            return "The service rejected the request."
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:300]
        return "The service rejected the request."

    @staticmethod
    def _elevenlabs_batch_retry_delay(
        headers: Mapping[str, str] | None, attempt: int
    ) -> float:
        """Compute retry delay from Retry-After or exponential backoff."""
        retry_after = headers.get("Retry-After") if headers else None
        try:
            return min(3.0, max(0.25, float(retry_after)))
        except (TypeError, ValueError):
            return 0.5 * (2**attempt)

    def _transcribe_saved_audio_with_elevenlabs_batch(self, wav: bytes) -> str:
        """Use Scribe v2 batch only if the live WebSocket did not complete."""
        if not self._elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not configured")

        body, content_type = self._build_elevenlabs_batch_request(wav)
        response_body = None
        deadline = time.monotonic() + max(
            5,
            TRANSCRIPTION_TIMEOUT_SECONDS - GEMINI_POLISH_TIMEOUT_SECONDS - 5,
        )
        for attempt in range(ELEVENLABS_BATCH_MAX_ATTEMPTS):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise ElevenLabsBatchError(None, "request timed out")
            request = urllib.request.Request(
                ELEVENLABS_BATCH_API_URL,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": content_type,
                    "xi-api-key": self._elevenlabs_api_key,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=min(ELEVENLABS_BATCH_TIMEOUT_SECONDS, remaining_seconds),
                ) as response:
                    response_body = response.read()
                break
            except urllib.error.HTTPError as exc:
                error_body = exc.read()
                if (
                    exc.code in ELEVENLABS_BATCH_RETRYABLE_STATUS_CODES
                    and attempt + 1 < ELEVENLABS_BATCH_MAX_ATTEMPTS
                ):
                    delay = self._elevenlabs_batch_retry_delay(exc.headers, attempt)
                    log.warning(
                        "ElevenLabs batch fallback returned HTTP %d; retrying in %.2f seconds",
                        exc.code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise ElevenLabsBatchError(
                    exc.code, self._elevenlabs_batch_error_detail(error_body)
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt + 1 < ELEVENLABS_BATCH_MAX_ATTEMPTS:
                    delay = self._elevenlabs_batch_retry_delay(None, attempt)
                    log.warning(
                        "ElevenLabs batch fallback network error; retrying in %.2f seconds: %s",
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                    continue
                raise ElevenLabsBatchError(None, "network request failed") from exc

        try:
            payload = json.loads((response_body or b"").decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, ValueError) as exc:
            raise ElevenLabsBatchError(None, "invalid JSON response") from exc
        if not isinstance(payload, dict):
            raise ElevenLabsBatchError(None, "unexpected response format")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ElevenLabsBatchError(None, "response did not contain a transcript")
        return text.strip()

    def _polish_transcript_with_gemini(
        self, transcript: str, *, prefetch: bool = False
    ) -> str:
        """Translate and polish an ElevenLabs transcript into English."""
        transcript = transcript.strip()
        if not transcript:
            log.info("Skipping Gemini polish because the transcript is empty")
            return ""
        client = (
            self._get_prefetch_gemini_client()
            if prefetch
            else self._get_gemini_client()
        )
        types = self._types
        config = self._build_gemini_config(
            types,
            GEMINI_ENGLISH_POLISH_MODEL,
            build_system_prompt(),
            2048,
        )
        response = None
        for _attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=GEMINI_ENGLISH_POLISH_MODEL,
                    contents=transcript,
                    config=config,
                )
                break
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if _attempt < 2 and isinstance(status, int) and status in (
                    429, 500, 502, 503, 504,
                ):
                    delay = 0.5 * (2 ** _attempt)
                    log.warning(
                        "Gemini polish attempt %d failed (HTTP %d); "
                        "retrying in %.1fs",
                        _attempt + 1,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise

        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Gemini English polish returned no text")
        log.info(
            "Gemini English polish completed (%d input chars, %d output chars)",
            len(transcript),
            len(text.strip()),
        )
        return text.strip()

    def create_live_session(self) -> LiveScribeSession | None:
        """Start realtime transcription only for the ElevenLabs + Gemini model."""
        if (
            self.get_model() != ELEVENLABS_MODEL
            or not self._elevenlabs_api_key
            or not self._gemini_api_key
            or DRY_RUN
        ):
            return None
        try:
            self._get_prefetch_gemini_client()
            return LiveScribeSession(
                self._elevenlabs_api_key,
                lambda text: self._polish_transcript_with_gemini(text, prefetch=True),
            )
        except Exception:
            log.exception("Could not start ElevenLabs realtime; using saved audio")
            return None

    def polish_live_transcript(self, transcript: str) -> str:
        """Polish completed realtime Scribe text into English with Gemini."""
        return self._polish_transcript_with_gemini(transcript, prefetch=False)

    @staticmethod
    def _groq_prompt() -> str | None:
        """Join custom vocabulary terms into a short Groq prompt hint."""
        with SETTINGS_LOCK:
            terms = list(dict.fromkeys(VOCABULARY.values()))
        if not terms:
            return None
        joined = ", ".join(terms)
        return joined[:400]

    def transcribe(self, wav: bytes, model_override: str | None = None) -> str:
        """Transcribe audio with the selected (or recording-locked) backend."""
        model = model_override or self.get_model()
        wav = prepare_batch_wav(wav)
        if len(wav) > MAX_UPLOAD_WAV_BYTES:
            raise RuntimeError(
                "Recording is too large to upload. Split long dictations into shorter clips."
            )

        if model == ELEVENLABS_MODEL:
            self._get_gemini_client()
            transcript = self._transcribe_saved_audio_with_elevenlabs_batch(wav)
            return self._polish_transcript_with_gemini(transcript)

        if model == GROQ_MODEL:
            groq_client = self._get_groq_client()
            kwargs = {
                "file": ("recording.wav", wav),
                "model": GROQ_MODEL_ID,
                "temperature": 0,
                "response_format": "verbose_json",
            }
            prompt_hint = self._groq_prompt()
            if prompt_hint:
                kwargs["prompt"] = prompt_hint
            response = groq_client.audio.transcriptions.create(**kwargs)
            return str(response.text or "").strip()

        client = self._get_gemini_client()
        types = self._types
        prompt = build_system_prompt()
        config = self._build_gemini_config(types, model, prompt, 2048)
        audio_part = types.Part.from_bytes(data=wav, mime_type="audio/wav")
        try:
            response = client.models.generate_content(
                model=model,
                contents=types.Content(parts=[audio_part]),
                config=config,
            )
        except Exception as exc:
            error_text = str(exc).lower()
            if "developer instruction" not in error_text:
                raise
            log.warning("Model rejected system instruction; retrying as text content")
            response = client.models.generate_content(
                model=model,
                contents=types.Content(
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                    ]
                ),
                config=self._build_gemini_config(types, model, None, 2048),
            )

        text = getattr(response, "text", None)
        return text.strip() if isinstance(text, str) else ""


# ---------------------------------------------------------------
# Paste helper
# ---------------------------------------------------------------
def paste_text(text: str) -> None:
    """Clipboard copy + Ctrl+V. Waits for held modifier keys before pasting."""
    last_copy_err = None
    for _ in range(5):
        try:
            pyperclip.copy(text)
            last_copy_err = None
            break
        except Exception as exc:
            last_copy_err = exc
            time.sleep(0.03)
    if last_copy_err is not None:
        raise RuntimeError(f"Could not write text to clipboard: {last_copy_err}")

    modifiers = ("shift", "ctrl", "alt", "win")
    deadline = time.perf_counter() + 0.5
    while time.perf_counter() < deadline:
        still_down = False
        for mod in modifiers:
            try:
                if keyboard.is_pressed(mod):
                    still_down = True
                    break
            except Exception:
                continue
        if not still_down:
            break
        time.sleep(0.01)
    keyboard.send("ctrl+v")


def clean_model_output(text: str) -> str:
    """Treat common model silence messages as an actually empty response."""
    cleaned = (text or "").strip()
    silence_markers = {
        "no speech detected",
        "no speech detected.",
        "no audible speech",
        "no audible speech.",
        "[no speech]",
        "(no speech)",
        "silence",
        "silence.",
    }
    if cleaned.casefold() in silence_markers:
        return ""
    return cleaned


def user_facing_error(exc: Exception) -> str:
    """Convert common API failures into short overlay messages."""
    error_text = str(exc).casefold()
    if isinstance(exc, MicrophoneUnavailableError):
        return str(exc)
    if "too large to upload" in error_text:
        return "Recording is too long to upload. Try a shorter clip."
    if "clipboard" in error_text:
        return "Clipboard is busy. Try pasting manually."
    if isinstance(exc, ElevenLabsBatchError):
        if exc.status_code in (401, 403):
            return "ElevenLabs API key is missing, invalid, or not allowed."
        if exc.status_code == 402:
            return "ElevenLabs credits are unavailable. Check your account."
        if exc.status_code == 429:
            return "ElevenLabs is busy. The app retried; try again shortly."
        if exc.status_code == 408:
            return "ElevenLabs timed out. Try the saved recording again."
        if exc.status_code is not None and exc.status_code >= 500:
            return "ElevenLabs is temporarily unavailable. Try again shortly."
        if exc.status_code is not None:
            return "ElevenLabs rejected the fallback request. Check the app log."
        if "timed out" in error_text or "timeout" in error_text:
            return "ElevenLabs timed out. Try the saved recording again."
        if "network" in error_text:
            return "Network error. Check your connection."
        return "ElevenLabs did not return a transcript. Try again."
    if "elevenlabs" in error_text and (
        "realtime" in error_text or "scribe" in error_text
    ):
        return "ElevenLabs streaming failed. Check the log or retry a saved recording."
    quota_markers = (
        "resource_exhausted",
        "quota exceeded",
        "quota limit",
        "rate limit",
        "too many requests",
        "429",
    )
    if any(marker in error_text for marker in quota_markers):
        return "API quota reached. Try again later."
    if any(
        marker in error_text
        for marker in (
            "api key",
            "api_key",
            "groq_api_key",
            "gemini_api_key",
            "elevenlabs_api_key",
            "unauthorized",
            "authentication",
        )
    ):
        return "API key is missing or invalid. Check the selected provider key."
    if any(
        marker in error_text for marker in ("timed out", "timeout", "deadline exceeded")
    ):
        return "Request timed out. Try again."
    if any(
        marker in error_text
        for marker in ("connection", "network", "dns", "socket", "unavailable")
    ):
        return "Network error. Check your connection."
    if any(marker in error_text for marker in ("microphone", "audio device")):
        return "Microphone error. Check your input device."
    return "Transcription failed. Try again."


def audio_has_signal(wav: bytes) -> bool:
    """Check 100 ms windows so leading/trailing silence does not hide short speech."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as recording:
            rate = recording.getframerate() or SAMPLE_RATE
            samples = np.frombuffer(
                recording.readframes(recording.getnframes()), dtype=np.int16
            )
        if samples.size == 0:
            return False
        abs_samples = np.abs(samples.astype(np.float32))
        peak = float(np.max(abs_samples))
        if peak < AUDIO_PEAK_THRESHOLD:
            return False
        win = max(1, int(rate * 0.1))
        if samples.size <= win:
            rms = float(np.sqrt(np.mean(abs_samples * abs_samples)))
            return rms >= AUDIO_RMS_THRESHOLD
        trimmed_len = (samples.size // win) * win
        blocks = abs_samples[:trimmed_len].reshape(-1, win)
        max_window_rms = float(np.max(np.sqrt(np.mean(blocks * blocks, axis=1))))
        if samples.size > trimmed_len:
            tail = abs_samples[trimmed_len:]
            tail_rms = float(np.sqrt(np.mean(tail * tail)))
            max_window_rms = max(max_window_rms, tail_rms)
        return max_window_rms >= AUDIO_RMS_THRESHOLD
    except (EOFError, ValueError, wave.Error):
        log.exception("Could not inspect WAV signal")
        return False


def trim_silence(wav: bytes) -> bytes:
    """Trim quiet edges while keeping a short pad around spoken audio."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as recording:
            params = recording.getparams()
            pcm = recording.readframes(recording.getnframes())
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return wav
        active = np.flatnonzero(
            np.abs(samples.astype(np.int32)) >= SILENCE_TRIM_THRESHOLD
        )
        if active.size == 0:
            return wav
        pad = int(params.framerate * SILENCE_PADDING_SECONDS)
        start = max(0, int(active[0]) - pad)
        end = min(samples.size, int(active[-1]) + pad + 1)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setparams(params)
            output.writeframes(samples[start:end].astype(np.int16).tobytes())
        return buffer.getvalue()
    except (EOFError, ValueError, wave.Error):
        log.exception("Could not trim WAV silence")
        return wav


def prepare_batch_wav(wav: bytes) -> bytes:
    """Trim silence and downsample high-rate audio to 16 kHz for batch upload."""
    trimmed = trim_silence(wav)
    try:
        with wave.open(io.BytesIO(trimmed), "rb") as recording:
            params = recording.getparams()
            if (
                params.framerate <= SAMPLE_RATE
                or params.nchannels != 1
                or params.sampwidth != 2
            ):
                return trimmed
            pcm = recording.readframes(recording.getnframes())
        samples = np.frombuffer(pcm, dtype=np.int16)
        downsampled = _downsample_int16_mono(samples, params.framerate, SAMPLE_RATE)
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(downsampled.tobytes())
        return out.getvalue()
    except (EOFError, ValueError, wave.Error):
        log.exception("Could not downsample WAV for batch upload")
        return trimmed


# ---------------------------------------------------------------
# Overlay (tkinter)
# ---------------------------------------------------------------
class _MONITORINFO(ctypes.Structure):
    """Win32 MONITORINFO structure for display work-area queries."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _active_monitor_work_area() -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of the active monitor's taskbar-safe work area."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        hmon = None
        if hwnd:
            hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)
        if not hmon:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            hmon = user32.MonitorFromPoint(pt, 2)
        if not hmon:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        r = info.rcWork
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return None


class Overlay:
    """Small always-on-top pill at the bottom-centre of the active monitor."""

    def __init__(self, root: tk.Tk, volume_getter: Callable[[], float]) -> None:
        """Create the hidden always-on-top status pill and volume meter."""
        self._root = root
        self._volume_getter = volume_getter
        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.88)
        self._win.configure(bg="#1a1a2e")
        self._win.withdraw()

        self._label = tk.Label(
            self._win,
            text="",
            font=("Segoe UI", 11, "bold"),
            fg="white",
            bg="#1a1a2e",
            padx=16,
            pady=6,
        )
        self._label.pack()
        self._meter = tk.Canvas(
            self._win,
            width=180,
            height=8,
            bg="#641f1f",
            highlightthickness=0,
        )
        self._meter.pack(padx=16, pady=(0, 8))
        self._meter.create_rectangle(
            0, 0, 0, 8, fill="#2ecc71", outline="", tags="level"
        )

        self._recording_start: float = 0.0
        self._transcribing_start: float = 0.0
        self._timer_after_id: str | None = None
        self._meter_after_id: str | None = None
        self._flash_after_id: str | None = None
        self._blink_state = True

    def _position(self) -> None:
        """Centre the pill above the taskbar on the active monitor."""
        self._win.update_idletasks()
        ww = self._win.winfo_width()
        wh = self._win.winfo_height()
        work = _active_monitor_work_area()
        if work is not None:
            left, _top, right, bottom = work
            x = left + max(0, (right - left - ww) // 2)
            y = bottom - wh - 24
        else:
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = (sw - ww) // 2
            y = sh - wh - 60
        self._win.geometry(f"+{x}+{y}")

    def show_recording(self) -> None:
        """Display the red recording indicator, timer, and volume meter."""
        self._cancel_timers()
        self._recording_start = time.perf_counter()
        self._blink_state = True
        self._win.configure(bg="#c0392b")
        self._label.config(bg="#c0392b")
        self._meter.pack(padx=16, pady=(0, 8))
        self._update_recording_timer()
        self._update_meter()
        self._win.deiconify()
        self._win.lift()
        self._position()

    def show_starting(self) -> None:
        """Display a status message while the microphone opens."""
        self._show_status("Starting microphone...")

    def show_stopping(self) -> None:
        """Display a status message while the microphone closes."""
        self._show_status("Stopping microphone...")

    def _show_status(self, text: str) -> None:
        """Show a neutral status banner without the volume meter."""
        self._cancel_timers()
        self._meter.pack_forget()
        self._win.configure(bg="#2c3e50")
        self._label.config(text=text, bg="#2c3e50")
        self._win.deiconify()
        self._win.lift()
        self._position()

    def _update_recording_timer(self) -> None:
        """Refresh the blinking recording indicator and elapsed timer."""
        elapsed = time.perf_counter() - self._recording_start
        mins, secs = divmod(int(elapsed), 60)
        dot = "🔴" if self._blink_state else "⚪"
        self._blink_state = not self._blink_state
        self._label.config(text=f"{dot} Recording {mins:02d}:{secs:02d}", bg="#c0392b")
        self._position()
        self._timer_after_id = self._root.after(500, self._update_recording_timer)

    def _update_meter(self) -> None:
        """Poll recorder volume and resize the level meter bar."""
        level = max(0.0, min(1.0, self._volume_getter()))
        self._meter.coords("level", 0, 0, 180 * level, 8)
        self._meter_after_id = self._root.after(60, self._update_meter)

    def show_transcribing(self) -> None:
        """Display the elapsed-time banner during transcription."""
        self._cancel_timers()
        self._meter.pack_forget()
        self._transcribing_start = time.perf_counter()
        self._win.configure(bg="#2c3e50")
        self._update_transcribing_timer()
        self._win.deiconify()
        self._win.lift()
        self._position()

    def _update_transcribing_timer(self) -> None:
        """Refresh the elapsed transcription timer every 500 ms."""
        elapsed = int(time.perf_counter() - self._transcribing_start)
        mins, secs = divmod(elapsed, 60)
        self._label.config(text=f"Transcribing {mins:02d}:{secs:02d}", bg="#2c3e50")
        self._position()
        self._timer_after_id = self._root.after(500, self._update_transcribing_timer)

    def flash(self, text: str, ms: int = 2000) -> None:
        """Show a transient notification pill and hide it after a delay."""
        self._cancel_timers()
        self._meter.pack_forget()
        self._win.configure(bg="#1a1a2e")
        self._label.config(text=text, bg="#1a1a2e")
        self._win.deiconify()
        self._win.lift()
        self._position()
        self._flash_after_id = self._root.after(ms, self.hide)

    def hide(self) -> None:
        """Cancel active timers and hide the overlay window."""
        self._cancel_timers()
        self._win.withdraw()

    def _cancel_timers(self) -> None:
        """Cancel any scheduled Tkinter timer callbacks for the overlay."""
        if self._timer_after_id:
            self._root.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        if self._meter_after_id:
            self._root.after_cancel(self._meter_after_id)
            self._meter_after_id = None
        if self._flash_after_id:
            self._root.after_cancel(self._flash_after_id)
            self._flash_after_id = None


# ---------------------------------------------------------------
# App state machine
# ---------------------------------------------------------------
class App:
    """IDLE -> STARTING -> RECORDING -> STOPPING -> TRANSCRIBING -> IDLE.

    UI events are pushed to ``self.ui_events`` as ``(name, payload)`` tuples and
    consumed on the tkinter thread.
    """

    def __init__(
        self, recorder: Recorder, transcriber: Transcriber | None = None
    ) -> None:
        """Initialize the app state machine and UI event queue."""
        self.recorder = recorder
        self.transcriber = transcriber
        self.state = State.IDLE
        self._lock = threading.Lock()
        self.ui_events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._stop_time = 0.0
        self._media_was_toggled = False
        self._media_window = None
        self._live_session: LiveScribeSession | None = None
        self._active_recording_model: str | None = None
        self._transcription_cancel_event: threading.Event | None = None

    def _emit(self, name: str, **payload: Any) -> None:
        """Queue a named event for processing on the Tkinter thread."""
        self.ui_events.put((name, payload))

    def learn_correction(self) -> None:
        """Request opening the vocabulary correction dialog."""
        self._emit("learn")

    def open_vocabulary(self) -> None:
        """Request opening the vocabulary manager window."""
        self._emit("vocabulary")

    def open_stats(self) -> None:
        """Request opening the usage statistics dialog."""
        self._emit("stats")

    def open_shortcut_settings(self) -> None:
        """Request opening the recording shortcut capture dialog."""
        self._emit("shortcut_settings")

    def open_api_keys(self) -> None:
        """Request opening the API keys configuration dialog."""
        self._emit("api_keys")

    def recover_from_system_resume(self) -> None:
        """Discard a recording whose microphone stream crossed system sleep."""
        with self._lock:
            recording = self.state == State.RECORDING
        if recording:
            log.warning("Stopping recording interrupted by system sleep")
            self.abort_recording(
                reason="Recording stopped after system sleep. Start a new recording."
            )

    def change_recording_hotkey(self, hotkey: object) -> str:
        """Validate, re-register, and save a new recording shortcut."""
        global HOTKEY, TOGGLE_HOTKEY_HANDLE
        new_hotkey = validate_recording_hotkey(hotkey)
        with self._lock:
            if self.state != State.IDLE:
                raise RuntimeError(
                    "Stop recording or transcription before changing the shortcut."
                )

            with HOTKEY_LOCK:
                old_hotkey = HOTKEY
                if TOGGLE_HOTKEY_HANDLE is not None:
                    keyboard.remove_hotkey(TOGGLE_HOTKEY_HANDLE)
                    TOGGLE_HOTKEY_HANDLE = None

                HOTKEY = new_hotkey
                try:
                    register_hotkey(self.toggle)
                except Exception as exc:
                    HOTKEY = old_hotkey
                    if old_hotkey:
                        try:
                            register_hotkey(self.toggle)
                        except Exception:
                            log.exception(
                                "Could not restore previous recording shortcut"
                            )
                    raise RuntimeError("Could not register that shortcut.") from exc

                with SETTINGS_LOCK:
                    old_saved_hotkey = SETTINGS.get("recording_hotkey", old_hotkey)
                    SETTINGS["recording_hotkey"] = new_hotkey
                if not save_settings():
                    with SETTINGS_LOCK:
                        SETTINGS["recording_hotkey"] = old_saved_hotkey
                    if TOGGLE_HOTKEY_HANDLE is not None:
                        keyboard.remove_hotkey(TOGGLE_HOTKEY_HANDLE)
                        TOGGLE_HOTKEY_HANDLE = None
                    HOTKEY = old_hotkey
                    if old_hotkey:
                        try:
                            register_hotkey(self.toggle)
                        except Exception:
                            log.exception(
                                "Could not restore the previous recording shortcut"
                            )
                    raise RuntimeError("Could not save the shortcut setting.")

        log.info("Recording shortcut changed to %s", new_hotkey)
        return new_hotkey

    def set_input_device(self, input_device: str) -> None:
        """Switch the active microphone and persist the selection."""
        if not self.recorder.set_input_device(input_device):
            self._emit("flash", text="Stop recording before changing microphone")
            return
        global INPUT_DEVICE
        with SETTINGS_LOCK:
            INPUT_DEVICE = input_device
            SETTINGS["input_device"] = input_device
        save_settings()
        log.info("Microphone selected: %s", input_device)

    def _pause_background_media(self) -> None:
        """Pause active background audio playback while recording."""
        try:
            pids = active_media_pids()
            if not pids:
                log.info("No active background media to pause")
                return
            self._media_window = find_media_window(pids)
            if self._media_window is None:
                log.info("No background media window found; leaving media unchanged")
                return
            if not toggle_background_media(self._media_window):
                log.info("Media window did not respond; leaving media unchanged")
                self._media_window = None
                return
            self._media_was_toggled = wait_for_background_media(False)
            if self._media_was_toggled:
                log.info("Background media paused")
            else:
                log.info("Background media did not confirm pause")
        except Exception:
            log.exception("Could not pause background media")

    def _resume_background_media(self) -> None:
        """Resume background audio previously paused by the app."""
        if not self._media_was_toggled:
            return
        try:
            if background_media_is_playing():
                log.info("Background media already resumed; no toggle needed")
                return
            if not toggle_background_media(self._media_window):
                log.info(
                    "Original media window is unavailable; leaving media unchanged"
                )
                return
            if wait_for_background_media(True):
                log.info("Background media resumed")
            else:
                log.info("Background media did not confirm resume")
        except Exception:
            log.exception("Could not resume background media")
        finally:
            self._media_was_toggled = False
            self._media_window = None

    def toggle(self) -> None:
        """Toggle between starting and stopping dictation recording."""
        worker = None
        with self._lock:
            if self.state == State.IDLE:
                self.state = State.STARTING
                worker = (self._start_recording, "microphone-start")
                self._emit("starting")
            elif self.state == State.RECORDING:
                self._stop_time = time.perf_counter()
                self.state = State.STOPPING
                worker = (self._stop_recording, "microphone-stop")
                self._emit("stopping")
            else:
                message = {
                    State.STARTING: "Microphone is still starting",
                    State.STOPPING: "Microphone is still stopping",
                    State.TRANSCRIBING: "Still transcribing...",
                }.get(self.state, "Please try again")
                self._emit("flash", text=message)
                log.info("Toggle ignored while app state is %s", self.state.value)
                return

        target, name = worker
        try:
            threading.Thread(target=target, daemon=True, name=name).start()
        except Exception as exc:
            with self._lock:
                if self.state == State.STARTING:
                    self.state = State.IDLE
                elif self.state == State.STOPPING:
                    self.state = State.RECORDING
            self._emit("error", text=f"Could not start microphone worker: {exc}")
            log.exception("Could not launch microphone operation")

    def _start_recording(self) -> None:
        """Initialize live session, open microphone, and start recording."""
        live_session = None
        recording_model = None
        try:
            if self.transcriber is not None:
                recording_model = self.transcriber.get_model()
                live_session = self.transcriber.create_live_session()
            self._live_session = live_session
            self._active_recording_model = recording_model
            if not self.recorder.set_audio_sink(
                live_session.push_audio if live_session is not None else None
            ):
                raise RuntimeError("Could not prepare the live audio stream")
            self.recorder.start()
            self._pause_background_media()
            try:
                register_escape_hotkey(self.cancel_current_operation)
            except Exception:
                log.exception("Could not register Escape cancellation hotkey")
                self._emit("flash", text="Escape cancellation is unavailable")
            with self._lock:
                if self.state != State.STARTING:
                    abandoned = True
                else:
                    self.state = State.RECORDING
                    abandoned = False
                    self._emit("recording")
            if abandoned:
                _release_escape_hotkey()
                self.recorder.abort()
                if live_session is not None:
                    live_session.cancel()
                self._live_session = None
                self._active_recording_model = None
                self._resume_background_media()
                return
        except Exception as exc:
            try:
                self.recorder.abort()
            except Exception:
                log.exception("Could not clean up microphone after startup failure")
            if live_session is not None:
                live_session.cancel()
            self._live_session = None
            self._active_recording_model = None
            _release_escape_hotkey()
            self._resume_background_media()
            with self._lock:
                if self.state == State.STARTING:
                    self.state = State.IDLE
            self._emit("error", text=f"Microphone: {exc}")
            log.exception("Could not start microphone")
            return

        log.info("Recording started")

    def _stop_recording(self) -> None:
        """Stop capture, archive valid audio, and spawn transcription."""
        cancel_event = None
        live_session = self._live_session
        recording_model = self._active_recording_model
        try:
            wav, seconds = self.recorder.stop()
            self._resume_background_media()
            if wav is None:
                if live_session is not None:
                    live_session.cancel()
                with self._lock:
                    if self._live_session is live_session:
                        self._live_session = None
                    self._active_recording_model = None
                    if self.state == State.STOPPING:
                        _release_escape_hotkey()
                        self.state = State.IDLE
                self._emit("flash", text="Recording too short")
                log.info("Recording too short (%.2f s) -- discarded", seconds)
                return

            backup_path = archive_recording(wav) if audio_has_signal(wav) else None
            cancel_event = threading.Event()
            with self._lock:
                if self.state != State.STOPPING:
                    if live_session is not None:
                        live_session.cancel()
                    if self._live_session is live_session:
                        self._live_session = None
                    self._active_recording_model = None
                    return
                self.state = State.TRANSCRIBING
                self._transcription_cancel_event = cancel_event
                self._emit("transcribing")
            log.info("Recording stopped: %.2f s, %d bytes", seconds, len(wav))
            threading.Thread(
                target=self._watch_transcription,
                args=(cancel_event,),
                daemon=True,
                name="transcription-watchdog",
            ).start()
            threading.Thread(
                target=self._finish,
                args=(
                    wav,
                    seconds,
                    backup_path,
                    cancel_event,
                    live_session,
                    recording_model,
                ),
                daemon=True,
                name="worker",
            ).start()
        except Exception as exc:
            if live_session is not None:
                live_session.cancel()
            try:
                self.recorder.abort()
            except Exception:
                log.exception("Could not clean up microphone after stop failure")
            self._resume_background_media()
            with self._lock:
                if self._live_session is live_session:
                    self._live_session = None
                self._active_recording_model = None
                if self.state == State.STOPPING:
                    _release_escape_hotkey()
                    self.state = State.IDLE
                elif self.state == State.TRANSCRIBING:
                    if (
                        cancel_event is not None
                        and self._transcription_cancel_event is cancel_event
                    ):
                        cancel_event.set()
                        self._transcription_cancel_event = None
                        _release_escape_hotkey()
                    self.state = State.IDLE
            self._emit("error", text=f"Could not stop microphone: {exc}")
            log.exception("Could not stop microphone")

    def abort_recording(self, reason: str | None = None) -> None:
        """Cancel an active recording without transcribing or pasting it."""
        with self._lock:
            if self.state != State.RECORDING:
                return
            self.state = State.STOPPING
            self._emit("stopping")
        try:
            threading.Thread(
                target=self._abort_recording,
                args=(reason,),
                daemon=True,
                name="microphone-abort",
            ).start()
        except Exception as exc:
            with self._lock:
                if self.state == State.STOPPING:
                    self.state = State.RECORDING
            self._emit("error", text=f"Could not cancel recording: {exc}")
            log.exception("Could not launch microphone cancellation")

    def cancel_current_operation(self) -> None:
        """Route Escape to the active recording or transcription operation."""
        with self._lock:
            state = self.state
        if state == State.RECORDING:
            self.abort_recording()
        elif state == State.TRANSCRIBING:
            self.cancel_transcription()

    def cancel_transcription(self) -> None:
        """Discard an in-flight transcript and interrupt live finalization."""
        with self._lock:
            if self.state != State.TRANSCRIBING:
                return
            cancel_event = self._transcription_cancel_event
            if cancel_event is None or cancel_event.is_set():
                return

            _release_escape_hotkey()
            cancel_event.set()
            self._transcription_cancel_event = None
            self.state = State.IDLE
            live_session, self._live_session = self._live_session, None
            self._active_recording_model = None
            self._emit("cancelled")
            log.info("Transcription cancelled by Escape")

        if live_session is not None:
            live_session.cancel()

    def _abort_recording(self, reason: str | None = None) -> None:
        """Abort active recording, cancel live stream, and restore media."""
        try:
            _release_escape_hotkey()
            self.recorder.abort()
            live_session, self._live_session = self._live_session, None
            self._active_recording_model = None
            if live_session is not None:
                live_session.cancel()
            self._resume_background_media()
            with self._lock:
                if self.state == State.STOPPING:
                    self.state = State.IDLE
            if reason:
                self._emit("cancelled", text=reason)
                log.info("Recording stopped after system sleep")
            else:
                self._emit("cancelled")
                log.info("Recording cancelled by Escape")
        except Exception as exc:
            self._resume_background_media()
            with self._lock:
                if self.state == State.STOPPING:
                    self.state = State.IDLE
            self._emit("error", text=f"Could not cancel recording: {exc}")
            log.exception("Could not cancel recording")

    def retry_recording(self, rank: int = 0) -> None:
        """Retry one of the newest archived recordings without recording again."""
        with self._lock:
            if self.state != State.IDLE:
                message = (
                    "Still transcribing..."
                    if self.state == State.TRANSCRIBING
                    else "Stop recording first"
                )
                self._emit("flash", text=message)
                return
            recordings = recent_recordings()
            if rank >= len(recordings):
                self._emit("flash", text="That backup is not available")
                return
            path = recordings[rank]
            try:
                with open(path, "rb") as recording_file:
                    wav = recording_file.read()
                with wave.open(io.BytesIO(wav), "rb") as recording:
                    seconds = recording.getnframes() / recording.getframerate()
            except (OSError, wave.Error):
                self._emit("error", text="Could not open backup")
                log.exception("Could not open recording backup: %s", path)
                return
            self._stop_time = time.perf_counter()
            self.state = State.TRANSCRIBING
            cancel_event = threading.Event()
            self._transcription_cancel_event = cancel_event
            model = self.transcriber.get_model() if self.transcriber else None
        try:
            register_escape_hotkey(self.cancel_current_operation)
        except Exception:
            log.exception("Could not register Escape cancellation hotkey")
            self._emit("flash", text="Escape cancellation is unavailable")
        self._emit("transcribing")
        log.info("Retrying recording backup %d: %s", rank + 1, path)
        try:
            threading.Thread(
                target=self._watch_transcription,
                args=(cancel_event,),
                daemon=True,
                name="retry-watchdog",
            ).start()
            threading.Thread(
                target=self._finish,
                args=(wav, seconds, path, cancel_event, None, model),
                daemon=True,
                name="retry-worker",
            ).start()
        except Exception:
            with self._lock:
                if self._transcription_cancel_event is cancel_event:
                    cancel_event.set()
                    self._transcription_cancel_event = None
                    _release_escape_hotkey()
                    self.state = State.IDLE
            self._emit("error", text="Could not start backup retry")
            log.exception("Could not launch transcript retry")

    def _watch_transcription(self, cancel_event: threading.Event) -> None:
        """Enforce the overall transcription timeout and reset state."""
        if cancel_event.wait(TRANSCRIPTION_TIMEOUT_SECONDS):
            return
        with self._lock:
            if (
                self._transcription_cancel_event is not cancel_event
                or cancel_event.is_set()
            ):
                return
            _release_escape_hotkey()
            cancel_event.set()
            self.state = State.IDLE
            self._transcription_cancel_event = None
            live_session, self._live_session = self._live_session, None
            self._active_recording_model = None
        if live_session is not None:
            live_session.cancel()
        self._emit("error", text="Transcription timed out. Try again.")
        log.error("Transcription exceeded %d seconds", TRANSCRIPTION_TIMEOUT_SECONDS)

    def _finish(
        self,
        wav: bytes,
        seconds: float,
        backup_path: str | None = None,
        cancel_event: threading.Event | None = None,
        live_session: LiveScribeSession | None = None,
        recording_model: str | None = None,
    ) -> None:
        """Finalize or transcribe audio, paste text, and record stats."""
        try:
            if cancel_event is not None and cancel_event.is_set():
                return
            if DRY_RUN:
                text = f"[dry-run] captured {seconds:.1f} s of audio"
            else:
                if self.transcriber is None:
                    raise RuntimeError("The transcription service is unavailable")
                t0 = time.perf_counter()
                live_transcript = (
                    live_session.finish() if live_session is not None else None
                )
                if cancel_event is not None and cancel_event.is_set():
                    log.info("Discarding cancelled transcription before fallback")
                    return
                if live_transcript:
                    log.info(
                        "Live Scribe transcript completed (%d chars)",
                        len(live_transcript),
                    )
                has_signal = audio_has_signal(wav)
                if live_transcript and has_signal:
                    text = (
                        live_session.polish_transcript(live_transcript)
                        if live_session is not None
                        else None
                    )
                    if not text:
                        text = self.transcriber.polish_live_transcript(live_transcript)
                elif not has_signal:
                    text = ""
                    if live_transcript:
                        log.info(
                            "Live transcript discarded -- "
                            "audio signal below speech threshold"
                        )
                    else:
                        log.info("Audio signal below speech threshold -- skipping API")
                    if live_session is not None:
                        live_session.cancel()
                else:
                    if live_session is not None:
                        live_session.cancel()
                        log.info("Using saved-audio ElevenLabs batch fallback")
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    text = self.transcriber.transcribe(
                        wav, model_override=recording_model
                    )
                if cancel_event is not None and cancel_event.is_set():
                    log.info("Discarding cancelled transcription result")
                    return
                text = clean_model_output(text)
                api_ms = (time.perf_counter() - t0) * 1000
                log.info("API round-trip: %.0f ms (audio %.1f s)", api_ms, seconds)

            if cancel_event is not None and cancel_event.is_set():
                log.info(
                    "Discarding transcription result after cancellation or timeout"
                )
                return
            if not text:
                self._emit("flash", text="No speech")
                log.info("Empty response -- nothing pasted")
                return
            if backup_path:
                save_transcript_backup(backup_path, text)

            with self._lock:
                if cancel_event is not None and (
                    cancel_event.is_set()
                    or self._transcription_cancel_event is not cancel_event
                ):
                    log.info(
                        "Discarding transcription result after cancellation or timeout"
                    )
                    return

            paste_text(text)

            with self._lock:
                if cancel_event is not None:
                    cancel_event.set()
            total_ms = (time.perf_counter() - self._stop_time) * 1000
            self._emit("done")
            log.info(
                "Pasted %d chars (stop -> paste %.0f ms)",
                len(text),
                total_ms,
            )
            with contextlib.suppress(Exception):
                record_stats(
                    len(text.split()), seconds, recording_model or PREFERRED_MODEL
                )
        except Exception as exc:
            if cancel_event is None or not cancel_event.is_set():
                self._emit("error", text=user_facing_error(exc))
                log.exception("Transcription failed")
            else:
                log.info(
                    "Transcription worker stopped after cancellation", exc_info=True
                )
        finally:
            if live_session is not None:
                live_session.cancel()
            with self._lock:
                if self._live_session is live_session:
                    self._live_session = None
                self._active_recording_model = None
                current = self._transcription_cancel_event is cancel_event
                if current:
                    _release_escape_hotkey()
                    self.state = State.IDLE
                    self._transcription_cancel_event = None
                    if cancel_event is not None:
                        cancel_event.set()
            if current:
                self._emit("idle")

    def restore_background_media(self) -> None:
        """Resume media if the app exits while recording."""
        live_session, self._live_session = self._live_session, None
        if live_session is not None:
            live_session.cancel()
        self.recorder.set_audio_sink(None)
        with self._lock:
            self._resume_background_media()


# ---------------------------------------------------------------
# Hotkey registration
# ---------------------------------------------------------------
TOGGLE_TIMER = None
TOGGLE_TIMER_LOCK = threading.Lock()
HOTKEY_LOCK = threading.RLock()
ESCAPE_HOTKEY = None
TOGGLE_HOTKEY_HANDLE = None
LEARN_HOTKEY_HANDLE = None

DEVICE_NOTIFY_CALLBACK_FLAG = 0x00000002
PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

_DEVICE_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
)


class _DeviceNotifySubscribeParameters(ctypes.Structure):
    """Win32 callback parameters for power resume notifications."""

    _fields_ = [
        ("Callback", _DEVICE_NOTIFY_CALLBACK),
        ("Context", ctypes.c_void_p),
    ]


class WindowsResumeMonitor:
    """Receive Windows suspend/resume notifications through a native callback."""

    def __init__(self, callback: Callable[[], Any]) -> None:
        """Register a Win32 suspend/resume notification callback."""
        if os.name != "nt":
            raise OSError("Windows power notifications are only available on Windows")

        self._callback = callback
        self._callback_proc = _DEVICE_NOTIFY_CALLBACK(self._on_notification)
        self._parameters = _DeviceNotifySubscribeParameters(self._callback_proc, None)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        register = user32.RegisterSuspendResumeNotification
        register.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        register.restype = ctypes.c_void_p
        self._unregister = user32.UnregisterSuspendResumeNotification
        self._unregister.argtypes = (ctypes.c_void_p,)
        self._unregister.restype = ctypes.c_int

        recipient = ctypes.cast(ctypes.pointer(self._parameters), ctypes.c_void_p)
        self._handle = register(recipient, DEVICE_NOTIFY_CALLBACK_FLAG)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def _on_notification(self, _context: Any, event_type: int, _setting: Any) -> int:
        """Trigger the callback when Windows reports system resume."""
        if event_type in (
            PBT_APMRESUMECRITICAL,
            PBT_APMRESUMESUSPEND,
            PBT_APMRESUMEAUTOMATIC,
        ):
            try:
                self._callback()
            except Exception:
                log.exception("Could not queue hotkey recovery after system resume")
        return 0

    def close(self) -> None:
        """Unregister the Windows suspend/resume notification handle."""
        if self._handle:
            if not self._unregister(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None


def register_escape_hotkey(callback: Callable[[], Any]) -> None:
    """Capture Escape while a recording or transcription is active."""
    global ESCAPE_HOTKEY
    with HOTKEY_LOCK:
        if ESCAPE_HOTKEY is None:
            ESCAPE_HOTKEY = keyboard.add_hotkey(
                "esc",
                callback,
                suppress=True,
                trigger_on_release=False,
            )


def unregister_escape_hotkey() -> None:
    """Release Escape so other applications receive it normally."""
    global ESCAPE_HOTKEY
    with HOTKEY_LOCK:
        if ESCAPE_HOTKEY is not None:
            keyboard.remove_hotkey(ESCAPE_HOTKEY)
            ESCAPE_HOTKEY = None


def _release_escape_hotkey() -> None:
    """Best-effort removal keeps cleanup failures from stranding app state."""
    try:
        unregister_escape_hotkey()
    except Exception:
        log.exception("Could not unregister Escape cancellation hotkey")


def _hotkey_overlaps_learn_chord(hotkey: str | None) -> bool:
    """Return whether the toggle shortcut is inside the learn chord."""
    if not hotkey:
        return False
    toggle_keys = {
        canonical_hotkey_key(p.strip())
        for p in hotkey.casefold().split("+")
        if p.strip()
    }
    learn_keys = {
        canonical_hotkey_key(p.strip())
        for p in LEARN_HOTKEY.casefold().split("+")
        if p.strip()
    }
    return bool(toggle_keys) and toggle_keys.issubset(learn_keys)


def schedule_toggle(callback: Callable[[], Any]) -> None:
    """Delay only when the toggle hotkey overlaps the 3-key learn chord."""
    global TOGGLE_TIMER
    if not _hotkey_overlaps_learn_chord(HOTKEY):
        callback()
        return
    with TOGGLE_TIMER_LOCK:
        if TOGGLE_TIMER is not None:
            TOGGLE_TIMER.cancel()
        TOGGLE_TIMER = threading.Timer(HOTKEY_CHORD_GRACE_SECONDS, callback)
        TOGGLE_TIMER.daemon = True
        TOGGLE_TIMER.start()


def cancel_scheduled_toggle() -> None:
    """Cancel a pending delayed recording toggle timer."""
    global TOGGLE_TIMER
    with TOGGLE_TIMER_LOCK:
        if TOGGLE_TIMER is not None:
            TOGGLE_TIMER.cancel()
            TOGGLE_TIMER = None


def register_hotkey(callback: Callable[[], Any]) -> bool:
    """Register the configured start/stop toggle."""
    global TOGGLE_HOTKEY_HANDLE
    if not HOTKEY:
        TOGGLE_HOTKEY_HANDLE = None
        log.info("No recording shortcut configured; use the tray menu to set one")
        return False

    try:
        TOGGLE_HOTKEY_HANDLE = keyboard.add_hotkey(
            HOTKEY,
            lambda: schedule_toggle(callback),
            suppress=True,
            trigger_on_release=False,
        )
        log.info("Hotkey registered: %s", HOTKEY)
        return True
    except Exception:
        TOGGLE_HOTKEY_HANDLE = None
        log.exception("Could not register recording shortcut: %s", HOTKEY)
    raise RuntimeError(f"Could not register recording shortcut: {HOTKEY}")


def register_learn_hotkey(callback: Callable[[], Any]) -> None:
    """Register the correction-learning hotkey."""
    global LEARN_HOTKEY_HANDLE

    def run_learning_callback():
        cancel_scheduled_toggle()
        callback()

    try:
        LEARN_HOTKEY_HANDLE = keyboard.add_hotkey(
            LEARN_HOTKEY,
            run_learning_callback,
            suppress=True,
            trigger_on_release=False,
        )
        log.info("Learning hotkey registered: %s", LEARN_HOTKEY)
        return
    except Exception:
        log.exception(
            "Named learning hotkey %r failed, falling back to scan codes",
            LEARN_HOTKEY,
        )
    for combo in LEARN_HOTKEY_SCANCODES:
        try:
            LEARN_HOTKEY_HANDLE = keyboard.add_hotkey(
                combo,
                run_learning_callback,
                suppress=True,
                trigger_on_release=False,
            )
            log.info("Learning hotkey registered via scan codes: %s", combo)
            return
        except Exception:
            log.exception("Could not register learning hotkey scan codes: %s", combo)
    raise RuntimeError("Could not register correction-learning hotkey")


# ---------------------------------------------------------------
# Tray icon (pystray)
# ---------------------------------------------------------------
def make_icon_image(color: str = "grey") -> Image.Image:
    """Generate a simple 64x64 circle icon."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = {"grey": (140, 140, 140, 255), "red": (220, 50, 50, 255)}.get(
        color, (140, 140, 140, 255)
    )
    draw.ellipse([8, 8, 56, 56], fill=fill)
    return img


class TrayManager:
    """System tray icon with Start/Stop, Open log, and Quit."""

    def __init__(
        self,
        app: App,
        quit_callback: Callable[[], Any],
        restart_callback: Callable[[], Any] | None = None,
    ) -> None:
        """Build the system tray icon, submenus, and action handlers."""
        import pystray

        self._app = app
        self._quit_callback = quit_callback
        self._restart_callback = restart_callback or quit_callback
        self._pystray = pystray
        self._icon_idle = make_icon_image("grey")
        self._icon_rec = make_icon_image("red")
        self._microphone_signature = None
        self._microphone_default_name = None
        self._microphone_devices = ()
        self._tray_update_lock = threading.Lock()
        self._recording_update_pending = False
        self._recording_requested = False
        self._menu_update_pending = False
        self._menu_update_version = 0
        model_items = [
            pystray.MenuItem(
                model_label(model),
                self._make_model_handler(model),
                checked=self._make_model_checker(model),
                radio=True,
            )
            for model in dict.fromkeys((*MODEL_OPTIONS, PREFERRED_MODEL))
        ]
        minimum_recording_items = [
            pystray.MenuItem(
                f"Minimum recording: {seconds:.1f} seconds",
                self._make_min_seconds_handler(seconds),
                checked=self._make_min_seconds_checker(seconds),
                radio=True,
            )
            for seconds in MIN_RECORDING_OPTIONS
        ]
        settings_menu = pystray.Menu(
            *minimum_recording_items,
            pystray.MenuItem("Recording shortcut...", self._on_open_shortcut_settings),
            pystray.MenuItem("API keys...", self._on_open_api_keys),
            pystray.MenuItem(
                "Microphone",
                pystray.Menu(self._build_microphone_menu),
            ),
            pystray.MenuItem(
                "Launch at startup",
                self._on_toggle_startup,
                checked=self._is_startup_checked,
            ),
            pystray.MenuItem("Open transcript backups", self._on_open_transcripts),
            pystray.MenuItem("Open log", self._on_open_log),
        )
        retry_menu = pystray.Menu(
            *[
                pystray.MenuItem(label, self._make_retry_handler(rank))
                for rank, label in enumerate(
                    ("Newest recording", "2nd newest recording", "3rd newest recording")
                )
            ]
        )
        self._icon = pystray.Icon(
            "STT",
            self._icon_idle,
            "STT",
            menu=pystray.Menu(
                pystray.MenuItem(
                    "Start / Stop recording",
                    self._on_toggle,
                    default=True,
                ),
                pystray.MenuItem("Retry transcript", retry_menu),
                pystray.MenuItem("Manage vocabulary", self._on_open_vocabulary),
                pystray.MenuItem("Usage stats", self._on_open_stats),
                pystray.MenuItem("Model", pystray.Menu(*model_items)),
                pystray.MenuItem("Settings", settings_menu),
                pystray.MenuItem("Restart", self._on_restart),
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

    def _make_model_handler(self, model: str) -> Callable[[Any, Any], None]:
        """Create a menu callback to select a transcription model."""
        return lambda _icon, _item: self._set_model(model)

    def _make_model_checker(self, model: str) -> Callable[[Any], bool]:
        """Create a radio-check predicate for a transcription model."""
        return lambda _item: model == PREFERRED_MODEL

    def _build_microphone_menu(self) -> Iterator["pystray.MenuItem"]:
        """Build from the latest device snapshot, without querying Windows here."""
        default_name = self._microphone_default_name
        default_label = "System default"
        if default_name:
            default_label = f"System default ({default_name})"
        yield self._pystray.MenuItem(
            default_label,
            self._make_microphone_handler(DEFAULT_MICROPHONE),
            checked=self._make_microphone_checker(DEFAULT_MICROPHONE),
            radio=True,
        )

        devices = self._microphone_devices
        if not devices:
            yield self._pystray.MenuItem(
                "No active microphones found",
                lambda _icon, _item: None,
                enabled=False,
            )
            return

        for device in devices:
            yield self._pystray.MenuItem(
                device["label"],
                self._make_microphone_handler(device["key"]),
                checked=self._make_microphone_checker(device["key"]),
                radio=True,
            )

        with SETTINGS_LOCK:
            current_input = INPUT_DEVICE
        if current_input != DEFAULT_MICROPHONE and not any(
            self._make_microphone_checker(device["key"])(None) for device in devices
        ):
            yield self._pystray.MenuItem(
                "Selected microphone is unavailable",
                lambda _icon, _item: None,
                enabled=False,
            )

    def _make_microphone_handler(
        self, input_device: str
    ) -> Callable[[Any, Any], None]:
        """Create a menu callback to switch the input microphone."""
        def handler(icon, _item):
            self._app.set_input_device(input_device)
            self.refresh_microphone_menu(force=True, icon=icon)

        return handler

    def _make_microphone_checker(self, input_device: str) -> Callable[[Any], bool]:
        """Create a radio-check predicate for a microphone entry."""
        selected_name = input_device.split(" | ", 1)[-1]

        def is_selected(_item):
            with SETTINGS_LOCK:
                current_input = INPUT_DEVICE
            if input_device == current_input:
                return True
            if current_input == DEFAULT_MICROPHONE:
                return False
            saved_name = current_input.split(" | ", 1)[-1]
            return Recorder._same_microphone(saved_name, selected_name)

        return is_selected

    def load_microphone_snapshot(
        self,
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Read microphone state off the Tk thread for responsive UI updates."""
        default_name = _windows_default_microphone_name() or ""
        devices = list_input_devices()
        return default_name, tuple(devices)

    def refresh_microphone_menu(
        self,
        force: bool = False,
        icon: "pystray.Icon | None" = None,
        snapshot: tuple[str, tuple[dict[str, Any], ...]] | None = None,
    ) -> None:
        """Apply a cached device snapshot and update the tray menu if needed."""
        if snapshot is not None:
            default_name, devices = snapshot
            self._microphone_default_name = default_name
            self._microphone_devices = tuple(devices)
            signature = (
                default_name,
                tuple((device["key"], device["label"]) for device in devices),
            )
        else:
            signature = self._microphone_signature
        if not force and signature == self._microphone_signature:
            return
        self._microphone_signature = signature
        self._schedule_menu_update(icon or self._icon)

    def _schedule_menu_update(self, icon: "pystray.Icon") -> None:
        """Coalesce and run tray menu refreshes on a background thread."""
        with self._tray_update_lock:
            self._menu_update_version += 1
            if self._menu_update_pending:
                return
            self._menu_update_pending = True

        def update_menu():
            while True:
                with self._tray_update_lock:
                    version = self._menu_update_version
                try:
                    icon.update_menu()
                except Exception:
                    log.exception("Could not refresh the microphone menu")
                    with self._tray_update_lock:
                        self._menu_update_pending = False
                    return
                with self._tray_update_lock:
                    if version == self._menu_update_version:
                        self._menu_update_pending = False
                        return

        try:
            threading.Thread(
                target=update_menu,
                daemon=True,
                name="tray-menu-refresh",
            ).start()
        except Exception:
            with self._tray_update_lock:
                self._menu_update_pending = False
            log.exception("Could not start tray menu refresh")

    def _make_retry_handler(self, rank: int) -> Callable[[Any, Any], None]:
        """Create a menu callback to retry an archived recording."""
        return lambda _icon, _item: self._app.retry_recording(rank)

    def _make_min_seconds_handler(self, seconds: float) -> Callable[[Any, Any], None]:
        """Create a menu callback to update minimum recording length."""
        return lambda _icon, _item: self._set_min_seconds(seconds)

    @staticmethod
    def _make_min_seconds_checker(seconds: float) -> Callable[[Any], bool]:
        """Create a radio-check predicate for minimum recording length."""
        def is_selected(_item):
            with SETTINGS_LOCK:
                return MIN_SECONDS == seconds

        return is_selected

    def _set_model(self, model: str) -> None:
        """Persist and apply a newly chosen transcription model."""
        global PREFERRED_MODEL
        with SETTINGS_LOCK:
            PREFERRED_MODEL = model
            SETTINGS["model"] = model
        if self._app.transcriber is not None:
            self._app.transcriber.set_model(model)
        save_settings()

    def _set_min_seconds(self, seconds: float) -> None:
        """Persist and apply the minimum valid recording duration."""
        global MIN_SECONDS
        with SETTINGS_LOCK:
            MIN_SECONDS = seconds
            SETTINGS["min_seconds"] = seconds
        save_settings()
        log.info("Minimum recording duration: %.1f seconds", seconds)

    def start(self) -> None:
        """Start the pystray icon loop in a detached background thread."""
        self._icon.run_detached()

    def set_recording(self, recording: bool) -> None:
        """Update the tray icon image to reflect recording state."""
        with self._tray_update_lock:
            self._recording_requested = recording
            if self._recording_update_pending:
                return
            self._recording_update_pending = True

        def update_icon():
            while True:
                with self._tray_update_lock:
                    recording = self._recording_requested
                try:
                    self._icon.icon = self._icon_rec if recording else self._icon_idle
                except Exception:
                    log.exception("Could not update tray icon")
                    with self._tray_update_lock:
                        self._recording_update_pending = False
                    return
                with self._tray_update_lock:
                    if recording == self._recording_requested:
                        self._recording_update_pending = False
                        return

        try:
            threading.Thread(
                target=update_icon,
                daemon=True,
                name="tray-icon-refresh",
            ).start()
        except Exception:
            with self._tray_update_lock:
                self._recording_update_pending = False
            log.exception("Could not start tray icon refresh")

    def stop(self) -> None:
        """Stop the system tray icon loop."""
        try:
            self._icon.stop()
        except Exception:
            log.exception("Could not stop tray icon")

    def _on_toggle(self, _icon: Any, _item: Any) -> None:
        """Handle the tray Start/Stop recording menu action."""
        self._app.toggle()

    def _on_open_vocabulary(self, _icon: Any, _item: Any) -> None:
        """Handle the tray Manage vocabulary menu action."""
        self._app.open_vocabulary()

    def _on_open_stats(self, _icon: Any, _item: Any) -> None:
        """Handle the tray Usage stats menu action."""
        self._app.open_stats()

    def _on_open_shortcut_settings(self, _icon: Any, _item: Any) -> None:
        """Handle the tray Recording shortcut menu action."""
        self._app.open_shortcut_settings()

    def _on_open_api_keys(self, _icon: Any, _item: Any) -> None:
        """Handle the tray API keys menu action."""
        self._app.open_api_keys()

    @staticmethod
    def _is_startup_checked(_item: Any) -> bool:
        """Return whether launching automatically at Windows login is enabled."""
        return is_startup_enabled()

    def _on_toggle_startup(self, icon: Any, _item: Any) -> None:
        """Toggle launching STT automatically when Windows starts."""
        enable = not is_startup_enabled()
        if set_startup_enabled(enable):
            state_label = "enabled" if enable else "disabled"
            self._app._emit("flash", text=f"Launch at startup {state_label}")
        else:
            self._app._emit("error", text="Could not update startup setting")
        self._schedule_menu_update(icon or self._icon)

    def _on_open_log(self, _icon: Any, _item: Any) -> None:
        """Open the application log file in the default viewer."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if not os.path.exists(LOG_FILE):
                with open(LOG_FILE, "a", encoding="utf-8"):
                    pass
            os.startfile(LOG_FILE)
        except OSError:
            try:
                subprocess.Popen(["notepad.exe", LOG_FILE])
            except OSError:
                log.exception("Could not open log file")

    def _on_open_transcripts(self, _icon: Any, _item: Any) -> None:
        """Open the transcript backups folder in File Explorer."""
        try:
            os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
            os.startfile(TRANSCRIPT_DIR)
        except OSError:
            log.exception("Could not open transcript backup folder")

    def _on_quit(self, _icon: Any, _item: Any) -> None:
        """Trigger application shutdown from the tray menu."""
        self._quit_callback()

    def _on_restart(self, _icon: Any, _item: Any) -> None:
        """Trigger application restart from the tray menu."""
        self._restart_callback()


def _enable_windows_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows when supported."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32")
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        if hasattr(user32, "SetProcessDPIAware"):
            user32.SetProcessDPIAware()
    except Exception:
        pass


def _focus_tk_window(window: tk.Misc) -> None:
    """Raise and focus a Tkinter window on a best-effort basis."""
    try:
        window.lift()
        window.focus_force()
    except Exception:
        pass


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main() -> None:
    """Initialize services, hotkeys, tray controls, and the Tk loop."""
    wait_for_existing = "--wait-for-existing" in sys.argv[1:]
    instance_deadline = (
        time.monotonic() + RESTART_INSTANCE_WAIT_SECONDS if wait_for_existing else None
    )
    try:
        while not acquire_single_instance():
            if instance_deadline is None or time.monotonic() >= instance_deadline:
                return
            time.sleep(0.2)
    except Exception as exc:
        _startup_error_box(
            "STT Startup Error",
            f"Could not initialize the Windows single-instance guard:\n\n{exc}",
        )
        return

    try:
        setup_logging()
    except Exception as exc:
        with contextlib.suppress(Exception):
            release_single_instance()
        _startup_error_box(
            "STT Startup Error",
            f"Could not create the app log folder:\n\n{LOG_DIR}\n\n{exc}",
        )
        return
    _enable_windows_dpi_awareness()
    log.info(
        "Environment files: local=%s (present=%s), appdata=%s (present=%s)",
        DOTENV_FILE,
        os.path.isfile(DOTENV_FILE),
        APP_DOTENV_FILE,
        os.path.isfile(APP_DOTENV_FILE),
    )
    if HOTKEY_SETTING_RESET:
        log.warning("Invalid saved recording shortcut; no shortcut is assigned")
    if INPUT_DEVICE_SETTING_RESET:
        log.warning("Invalid saved microphone selection; using System default")
    if HOTKEY_SETTING_MIGRATED:
        log.info("Migrated the existing recording shortcut into saved settings")
    if MODEL_SETTING_MIGRATED:
        log.info("Updated saved model selection to %s", PREFERRED_MODEL)
    if OBSOLETE_SETTINGS_MIGRATED:
        log.info("Removed deprecated tray settings from saved configuration")
    if (
        MODEL_SETTING_MIGRATED
        or OBSOLETE_SETTINGS_MIGRATED
        or HOTKEY_SETTING_RESET
        or HOTKEY_SETTING_MIGRATED
        or INPUT_DEVICE_SETTING_RESET
    ):
        save_settings()
    log.info("Starting (dry_run=%s, python=%s)", DRY_RUN, sys.version.split()[0])
    log.info(
        "API configuration: Gemini key=%s, Groq key=%s, ElevenLabs key=%s, selected model=%s",
        bool(API_KEY.strip()),
        bool(GROQ_API_KEY.strip()),
        bool(ELEVENLABS_API_KEY.strip()),
        PREFERRED_MODEL,
    )

    try:
        recorder = Recorder(input_device=INPUT_DEVICE)
    except Exception:
        log.exception("Could not open the microphone")
        return

    transcriber: Transcriber | None = None
    if not DRY_RUN:
        try:
            transcriber = Transcriber(API_KEY, GROQ_API_KEY, ELEVENLABS_API_KEY)
            threading.Thread(
                target=transcriber.prepare_backend, daemon=True, name="backend-prewarm"
            ).start()
        except Exception:
            log.exception("Could not create Transcriber; stopping startup")
            return

    app = App(recorder, transcriber)

    root = tk.Tk()
    root.withdraw()
    for _icon_base in (_app_dir(), getattr(sys, "_MEIPASS", "")):
        if _icon_base:
            _window_icon_path = os.path.join(_icon_base, "STT.ico")
            if os.path.isfile(_window_icon_path):
                with contextlib.suppress(Exception):
                    root.iconbitmap(default=_window_icon_path)
                break

    def report_tk_callback_exception(
        exc_type: type[BaseException], exc_value: BaseException, traceback: Any
    ) -> None:
        """Log unhandled exceptions raised inside Tkinter callbacks."""
        log.error(
            "Unhandled tkinter callback exception",
            exc_info=(exc_type, exc_value, traceback),
        )

    root.report_callback_exception = report_tk_callback_exception
    overlay = Overlay(root, recorder.volume_level)
    learning_window = None
    vocabulary_window = None
    vocab_refresh_callback = None
    shortcut_capture_close = None
    api_keys_window = None

    def show_learning_dialog(
        initial_wrong: str = "",
        initial_correct: str = "",
        on_saved: Callable[[str, str], Any] | None = None,
    ) -> None:
        """Open the dialog to add or edit a vocabulary correction."""
        nonlocal learning_window
        if learning_window is not None and learning_window.winfo_exists():
            _focus_tk_window(learning_window)
            return

        learning_window = tk.Toplevel(root)
        learning_window.title("Learn correction")
        learning_window.attributes("-topmost", True)
        learning_window.resizable(False, False)
        learning_window.protocol("WM_DELETE_WINDOW", learning_window.destroy)

        frame = tk.Frame(learning_window, padx=14, pady=12)
        frame.pack()
        tk.Label(frame, text="Incorrect word:").grid(row=0, column=0, sticky="w")
        wrong_entry = tk.Entry(frame, width=32)
        wrong_entry.grid(row=1, column=0, pady=(2, 8))
        wrong_entry.insert(0, initial_wrong)
        tk.Label(frame, text="Correct word:").grid(row=2, column=0, sticky="w")
        correct_entry = tk.Entry(frame, width=32)
        correct_entry.grid(row=3, column=0, pady=(2, 10))
        correct_entry.insert(0, initial_correct)

        def save_correction():
            wrong = wrong_entry.get().strip()
            correct = correct_entry.get().strip()
            if not wrong or not correct:
                overlay.flash("Enter both words", 1500)
                return
            with SETTINGS_LOCK:
                VOCABULARY[wrong.casefold()] = correct
            save_vocabulary()
            log.info("Learned vocabulary correction: %r -> %r", wrong, correct)
            learning_window.destroy()
            if on_saved is not None:
                on_saved(wrong, correct)
            if vocab_refresh_callback is not None:
                with contextlib.suppress(Exception):
                    vocab_refresh_callback()
            overlay.flash("Correction saved", 1200)

        tk.Button(frame, text="Save", command=save_correction).grid(
            row=4, column=0, sticky="e"
        )
        learning_window.bind("<Return>", lambda _event: save_correction())
        learning_window.bind("<Escape>", lambda _event: learning_window.destroy())
        _focus_tk_window(learning_window)
        wrong_entry.focus_set()

    def show_api_keys_dialog() -> None:
        """Open a dialog for viewing and saving provider API keys."""
        nonlocal api_keys_window
        if api_keys_window is not None and api_keys_window.winfo_exists():
            _focus_tk_window(api_keys_window)
            return

        win = tk.Toplevel(root)
        api_keys_window = win
        win.title("STT — API Keys")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Enter keys for the providers you want to use (saved in %LOCALAPPDATA%\\STT\\.env):",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        fields = [
            ("Gemini API key:", API_KEY),
            ("Groq API key:", GROQ_API_KEY),
            ("ElevenLabs API key:", ELEVENLABS_API_KEY),
        ]
        entries: list[ttk.Entry] = []
        for idx, (label_text, initial_val) in enumerate(fields, start=1):
            ttk.Label(frame, text=label_text).grid(
                row=idx, column=0, sticky="w", padx=(0, 12), pady=4
            )
            entry = ttk.Entry(frame, width=44, show="*")
            entry.grid(row=idx, column=1, sticky="ew", pady=4)
            entry.insert(0, initial_val)
            entries.append(entry)

        show_var = tk.BooleanVar(value=False)

        def toggle_visibility() -> None:
            mask = "" if show_var.get() else "*"
            for entry in entries:
                entry.configure(show=mask)

        ttk.Checkbutton(
            frame, text="Show keys", variable=show_var, command=toggle_visibility
        ).grid(row=4, column=1, sticky="w", pady=(4, 10))

        def save_keys() -> None:
            gem, groq_k, el_k = (entry.get() for entry in entries)
            if not save_api_keys(gem, groq_k, el_k):
                messagebox.showerror("Save failed", "Could not save API keys.", parent=win)
                return
            if transcriber is not None:
                transcriber.update_api_keys(API_KEY, GROQ_API_KEY, ELEVENLABS_API_KEY)
                threading.Thread(
                    target=transcriber.prepare_backend,
                    daemon=True,
                    name="backend-prewarm",
                ).start()
            win.destroy()
            overlay.flash("API keys saved", 1500)

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(buttons, text="Save", command=save_keys).pack(side="right")

        win.bind("<Return>", lambda _event: save_keys())
        win.bind("<Escape>", lambda _event: win.destroy())
        _focus_tk_window(win)
        entries[0].focus_set()

    stats_window_ref = [None]

    def show_stats_dialog() -> None:
        """Display aggregate and daily dictation statistics."""
        if stats_window_ref[0] is not None and stats_window_ref[0].winfo_exists():
            _focus_tk_window(stats_window_ref[0])
            return

        entries = load_stats()
        stats = compute_stats(entries)

        win = tk.Toplevel(root)
        stats_window_ref[0] = win
        win.title("STT — Usage Stats")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        frame = tk.Frame(win, padx=22, pady=18)
        frame.pack()

        tk.Label(
            frame, text="Usage Stats", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        rows = [
            ("Total words dictated", f"{stats['total_words']:,}"),
            ("Total transcriptions", f"{stats['total_transcriptions']:,}"),
            ("Avg. words per minute", f"{stats['avg_wpm']:,}"),
            ("Total recording time", _format_duration(stats["total_seconds"])),
            ("Current streak", f"{stats['current_streak']} day{'s' if stats['current_streak'] != 1 else ''}"),
        ]
        for i, (label, value) in enumerate(rows, start=1):
            tk.Label(
                frame, text=label, font=("Segoe UI", 10), anchor="w"
            ).grid(row=i, column=0, sticky="w", padx=(0, 24))
            tk.Label(
                frame, text=value, font=("Segoe UI", 10, "bold"), anchor="e"
            ).grid(row=i, column=1, sticky="e")

        sep_row = len(rows) + 1
        ttk.Separator(frame, orient="horizontal").grid(
            row=sep_row, column=0, columnspan=2, sticky="ew", pady=10
        )

        today_rows = [
            ("Today's words", f"{stats['today_words']:,}"),
            ("Today's transcriptions", f"{stats['today_transcriptions']:,}"),
        ]
        for i, (label, value) in enumerate(today_rows, start=sep_row + 1):
            tk.Label(
                frame, text=label, font=("Segoe UI", 10), anchor="w"
            ).grid(row=i, column=0, sticky="w", padx=(0, 24))
            tk.Label(
                frame, text=value, font=("Segoe UI", 10, "bold"), anchor="e"
            ).grid(row=i, column=1, sticky="e")

        close_row = sep_row + len(today_rows) + 1
        ttk.Button(frame, text="Close", command=win.destroy).grid(
            row=close_row, column=1, sticky="e", pady=(14, 0)
        )

        win.bind("<Escape>", lambda _event: win.destroy())
        _focus_tk_window(win)

    def show_vocabulary_manager() -> None:
        """Open the vocabulary management window with CRUD actions."""
        nonlocal vocabulary_window, vocab_refresh_callback
        if vocabulary_window is not None and vocabulary_window.winfo_exists():
            _focus_tk_window(vocabulary_window)
            return

        vocabulary_window = tk.Toplevel(root)
        vocabulary_window.title("STT vocabulary")
        vocabulary_window.attributes("-topmost", True)
        vocabulary_window.geometry("480x300")
        vocabulary_window.minsize(360, 220)

        frame = ttk.Frame(vocabulary_window, padding=12)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame,
            columns=("incorrect", "correct"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("incorrect", text="Incorrect word")
        tree.heading("correct", text="Correct word")
        tree.column("incorrect", width=210, anchor="w")
        tree.column("correct", width=210, anchor="w")
        tree.pack(fill="both", expand=True)

        def refresh_entries():
            if not vocabulary_window.winfo_exists():
                return
            for item in tree.get_children():
                tree.delete(item)
            with SETTINGS_LOCK:
                items = sorted(VOCABULARY.items())
            for wrong, correct in items:
                tree.insert("", "end", values=(wrong, correct))

        vocab_refresh_callback = refresh_entries

        def close_vocab():
            nonlocal vocab_refresh_callback
            vocab_refresh_callback = None
            vocabulary_window.destroy()

        def selected_entry():
            selected = tree.selection()
            if not selected:
                return None
            values = tree.item(selected[0], "values")
            return values if len(values) == 2 else None

        def add_entry():
            show_learning_dialog(on_saved=lambda _wrong, _correct: refresh_entries())

        def edit_entry():
            entry = selected_entry()
            if entry is None:
                overlay.flash("Select a correction first", 1500)
                return
            old_wrong, _old_correct = entry

            def save_edit(wrong, _correct):
                if wrong.casefold() != old_wrong.casefold():
                    with SETTINGS_LOCK:
                        VOCABULARY.pop(old_wrong.casefold(), None)
                    save_vocabulary()
                refresh_entries()

            show_learning_dialog(entry[0], entry[1], save_edit)

        def remove_entry():
            entry = selected_entry()
            if entry is None:
                overlay.flash("Select a correction first", 1500)
                return
            with SETTINGS_LOCK:
                VOCABULARY.pop(str(entry[0]).casefold(), None)
            save_vocabulary()
            refresh_entries()
            overlay.flash("Correction removed", 1200)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Add", command=add_entry).pack(side="left")
        ttk.Button(buttons, text="Edit", command=edit_entry).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Remove", command=remove_entry).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Close", command=close_vocab).pack(side="right")
        tree.bind("<Double-1>", lambda _event: edit_entry())
        vocabulary_window.protocol("WM_DELETE_WINDOW", close_vocab)
        refresh_entries()
        _focus_tk_window(vocabulary_window)

    def show_shortcut_settings() -> None:
        """Open the dialog to capture a new recording shortcut."""
        nonlocal shortcut_capture_close
        global TOGGLE_HOTKEY_HANDLE, LEARN_HOTKEY_HANDLE
        if app.state != State.IDLE:
            overlay.flash("Finish the current operation first", 1800)
            return
        if shortcut_capture_close is not None:
            return

        capture_window = tk.Toplevel(root)
        capture_window.title("Recording shortcut")
        capture_window.attributes("-topmost", True)
        capture_window.resizable(False, False)
        capture_window.protocol("WM_DELETE_WINDOW", lambda: close_capture())
        frame = tk.Frame(capture_window, padx=18, pady=14)
        frame.pack()
        current = HOTKEY or "Not set"
        tk.Label(frame, text=f"Current shortcut: {current}").pack(anchor="w")
        tk.Label(
            frame,
            text="Press and release the shortcut you want to use.\n"
            "Use at least one modifier; Esc cancels.",
            justify="left",
            pady=8,
        ).pack(anchor="w")
        tk.Button(frame, text="Cancel", command=lambda: close_capture()).pack(
            anchor="e"
        )
        _focus_tk_window(capture_window)

        capture = {
            "closed": False,
            "pending": False,
            "pressed": set(),
            "candidate": None,
            "hook": None,
        }
        modifier_order = {
            name: index
            for index, name in enumerate(("ctrl", "alt", "alt gr", "shift", "win"))
        }

        def restore_shortcut_hooks():
            if shutting_down:
                return
            with HOTKEY_LOCK:
                if HOTKEY and TOGGLE_HOTKEY_HANDLE is None:
                    try:
                        register_hotkey(app.toggle)
                    except Exception:
                        log.exception("Could not restore recording shortcut")
                if LEARN_HOTKEY_HANDLE is None:
                    try:
                        register_learn_hotkey(app.learn_correction)
                    except Exception:
                        log.exception("Could not restore learning shortcut")
            if HOTKEY and TOGGLE_HOTKEY_HANDLE is None:
                overlay.flash("Recording shortcut unavailable", 2500)

        def close_capture(restore_hooks=True, unhook=True):
            nonlocal shortcut_capture_close
            if capture["closed"]:
                return
            capture["closed"] = True
            if unhook and capture["hook"] is not None:
                try:
                    keyboard.unhook(capture["hook"])
                except Exception:
                    log.exception("Could not stop shortcut capture listener")
            if restore_hooks:
                restore_shortcut_hooks()
            if capture_window.winfo_exists():
                capture_window.destroy()
            if shortcut_capture_close is close_capture:
                shortcut_capture_close = None

        shortcut_capture_close = close_capture

        def on_key_event(event):
            if capture["closed"] or capture["pending"]:
                return
            key = str(event.name or "").casefold()
            if not key:
                return

            if key in HOTKEY_MODIFIERS:
                if event.event_type == "down":
                    capture["pressed"].add(key)
                elif event.event_type == "up":
                    capture["pressed"].discard(key)
                return

            if event.event_type == "down":
                active_modifiers = {
                    canonical_hotkey_key(pressed)
                    for pressed in capture["pressed"]
                    if pressed in HOTKEY_MODIFIERS
                }
                if key in {"esc", "escape"} and not active_modifiers:
                    capture["pending"] = True
                    app._emit("shortcut_capture_cancel")
                elif active_modifiers and capture["candidate"] is None:
                    modifiers = sorted(
                        active_modifiers,
                        key=lambda name: modifier_order.get(name, 99),
                    )
                    capture["candidate"] = (modifiers, key)
            elif (
                event.event_type == "up"
                and capture["candidate"] is not None
                and capture["candidate"][1] == key
            ):
                modifiers, main_key = capture["candidate"]
                capture["pending"] = True
                app._emit(
                    "shortcut_capture_key",
                    hotkey="+".join([*modifiers, main_key]),
                )

        try:
            with HOTKEY_LOCK:
                if TOGGLE_HOTKEY_HANDLE is not None:
                    keyboard.remove_hotkey(TOGGLE_HOTKEY_HANDLE)
                    TOGGLE_HOTKEY_HANDLE = None
                if LEARN_HOTKEY_HANDLE is not None:
                    keyboard.remove_hotkey(LEARN_HOTKEY_HANDLE)
                    LEARN_HOTKEY_HANDLE = None
            capture["hook"] = keyboard.hook(on_key_event, suppress=False)
        except Exception as exc:  # noqa: BLE001 - keyboard backend errors vary by Windows setup
            close_capture()
            messagebox.showerror("Shortcut capture unavailable", str(exc), parent=root)

    def finish_shortcut_capture(hotkey: str) -> None:
        """Apply a captured shortcut or show a validation error."""
        try:
            saved_hotkey = app.change_recording_hotkey(hotkey)
        except (RuntimeError, ValueError) as exc:
            close_capture = shortcut_capture_close
            if close_capture is not None:
                close_capture()
            messagebox.showerror("Shortcut not changed", str(exc), parent=root)
            return
        close_capture = shortcut_capture_close
        if close_capture is not None:
            close_capture()
        overlay.flash(f"Shortcut saved: {saved_hotkey}", 1800)

    tray = None
    power_monitor = None
    shutting_down = False
    resume_recovery_pending = False
    microphone_refresh_pending = threading.Event()
    resource_cleanup_lock = threading.Lock()
    resource_cleanup_started = False
    exit_request_lock = threading.Lock()
    exit_requested = False

    def recover_hotkeys(attempt: int = 1) -> None:
        """Re-register global keyboard hooks after system wake."""
        nonlocal resume_recovery_pending
        if shutting_down:
            resume_recovery_pending = False
            return

        cancel_scheduled_toggle()

        def refresh_hotkeys():
            global ESCAPE_HOTKEY, TOGGLE_HOTKEY_HANDLE, LEARN_HOTKEY_HANDLE
            succeeded = True
            try:
                restore_escape = ESCAPE_HOTKEY is not None
                with app._lock:
                    restore_escape = restore_escape or app.state in {
                        State.RECORDING,
                        State.STOPPING,
                        State.TRANSCRIBING,
                    }
                with HOTKEY_LOCK:
                    keyboard.unhook_all()
                    ESCAPE_HOTKEY = None
                    TOGGLE_HOTKEY_HANDLE = None
                    LEARN_HOTKEY_HANDLE = None
                    try:
                        register_hotkey(app.toggle)
                    except Exception:
                        succeeded = False
                        log.exception("Recording shortcut unavailable after resume")
                    try:
                        register_learn_hotkey(app.learn_correction)
                    except Exception:
                        succeeded = False
                        log.exception("Learning shortcut unavailable after resume")
                    if restore_escape:
                        try:
                            register_escape_hotkey(app.cancel_current_operation)
                        except Exception:
                            succeeded = False
                            log.exception(
                                "Escape cancellation unavailable after resume"
                            )
            except Exception:
                succeeded = False
                log.exception(
                    "Could not refresh keyboard shortcuts after system resume"
                )

            if succeeded:
                log.info("Keyboard shortcuts refreshed after system resume")
            app._emit(
                "hotkey_recovery_result",
                attempt=attempt,
                succeeded=succeeded,
            )

        try:
            threading.Thread(
                target=refresh_hotkeys,
                daemon=True,
                name="hotkey-resume-recovery",
            ).start()
        except Exception:
            log.exception("Could not start keyboard shortcut recovery")
            app._emit(
                "hotkey_recovery_result",
                attempt=attempt,
                succeeded=False,
            )

    def schedule_hotkey_recovery() -> None:
        """Schedule delayed hotkey recovery after a resume event."""
        nonlocal resume_recovery_pending
        if shutting_down or resume_recovery_pending:
            return
        resume_recovery_pending = True
        root.after(1200, recover_hotkeys)

    def cleanup_resources() -> None:
        """Release tray, monitor, hooks, audio, and API clients once."""
        nonlocal resource_cleanup_started
        with resource_cleanup_lock:
            if resource_cleanup_started:
                return
            resource_cleanup_started = True
        log.info("Shutting down")
        cleanup_steps = []
        if tray is not None:
            cleanup_steps.append(("stop tray icon", tray.stop))
        if power_monitor is not None:
            cleanup_steps.append(
                ("unregister power notifications", power_monitor.close)
            )
        cleanup_steps.extend(
            [
                ("unregister Escape hotkey", unregister_escape_hotkey),
                ("unhook keyboard listener", keyboard.unhook_all),
                ("restore background media", app.restore_background_media),
                ("close microphone", recorder.close),
            ]
        )
        if transcriber is not None:
            cleanup_steps.append(("close API clients", transcriber.close))
        cleanup_steps.append(
            ("release single-instance handle", release_single_instance)
        )
        for description, cleanup in cleanup_steps:
            try:
                cleanup()
            except Exception:
                log.exception("Could not %s", description)
        for handler in log.handlers:
            with contextlib.suppress(Exception):
                handler.flush()

    def shutdown() -> None:
        """Clean up resources and exit the Tkinter main loop."""
        nonlocal shutting_down, shortcut_capture_close
        if shutting_down:
            with contextlib.suppress(Exception):
                root.quit()
            return
        shutting_down = True
        if shortcut_capture_close is not None:
            try:
                shortcut_capture_close()
            except Exception:
                log.exception("Could not close shortcut capture during shutdown")
        cleanup_resources()
        with contextlib.suppress(Exception):
            root.quit()

    def request_application_exit(restart: bool = False) -> None:
        """Handle tray exit actions cleanly, with a forced-exit safety net."""
        nonlocal shutting_down, exit_requested
        with exit_request_lock:
            if exit_requested:
                return
            if restart:
                restart_cmd, restart_cwd = _restart_command()
                try:
                    subprocess.Popen(
                        restart_cmd,
                        cwd=restart_cwd,
                        close_fds=True,
                        creationflags=(
                            subprocess.DETACHED_PROCESS
                            | subprocess.CREATE_NEW_PROCESS_GROUP
                        ),
                    )
                except OSError:
                    log.exception("Could not start replacement STT process")
                    return
            exit_requested = True
            shutting_down = True

        def finish_exit():
            try:
                cleanup_thread = threading.Thread(
                    target=cleanup_resources,
                    daemon=True,
                    name="stt-shutdown-cleanup",
                )
                cleanup_thread.start()
                cleanup_thread.join(SHUTDOWN_CLEANUP_TIMEOUT_SECONDS)
            except Exception:
                log.exception("Could not run shutdown cleanup")
            with contextlib.suppress(Exception):
                root.after(0, root.quit)
            time.sleep(0.15)
            os._exit(0)

        try:
            threading.Thread(
                target=finish_exit,
                daemon=True,
                name="stt-exit-watchdog",
            ).start()
            log.info("Tray requested %s", "restart" if restart else "quit")
        except Exception:
            log.exception("Could not start exit watchdog; forcing process exit")
            os._exit(0)

    def restart_application() -> None:
        """Request a clean exit and spawn a replacement instance."""
        request_application_exit(restart=True)

    try:
        tray = TrayManager(
            app,
            lambda: request_application_exit(),
            lambda: request_application_exit(restart=True),
        )
        tray.start()
    except Exception:
        log.exception("Could not initialize tray controls")
        shutdown()
        root.destroy()
        return

    try:
        has_hotkey = register_hotkey(app.toggle)
        if not has_hotkey:
            overlay.flash("No shortcut set — open Tray > Settings", 3500)
    except Exception:
        log.exception("Recording shortcut unavailable; use the tray menu to change it")
        overlay.flash("Recording shortcut unavailable", 2500)

    try:
        register_learn_hotkey(app.learn_correction)
    except Exception:
        log.exception("Learning shortcut unavailable")

    if not DRY_RUN:
        selected_model = transcriber.get_model() if transcriber else PREFERRED_MODEL
        missing_key = (
            (
                selected_model == ELEVENLABS_MODEL
                and (not ELEVENLABS_API_KEY or not API_KEY)
            )
            or (selected_model == GROQ_MODEL and not GROQ_API_KEY)
            or (selected_model not in (ELEVENLABS_MODEL, GROQ_MODEL) and not API_KEY)
        )
        if not API_KEY and not GROQ_API_KEY and not ELEVENLABS_API_KEY:
            overlay.flash("Enter an API key in Settings > API keys", 3500)
            root.after(250, app.open_api_keys)
        elif missing_key:
            overlay.flash("API key missing — Tray > Settings > API keys", 3500)

    try:
        power_monitor = WindowsResumeMonitor(
            lambda: app.ui_events.put(("power_resume", {}))
        )
        log.info("Registered Windows suspend/resume notifications")
    except Exception:
        log.exception("Could not register Windows suspend/resume notifications")

    def read_microphone_snapshot() -> None:
        """Fetch current microphone devices off the main thread."""
        try:
            if tray is not None:
                app._emit(
                    "microphone_menu_refresh",
                    snapshot=tray.load_microphone_snapshot(),
                )
        except Exception:
            log.exception("Could not check for microphone changes")
        finally:
            microphone_refresh_pending.clear()

    def refresh_microphone_menu() -> None:
        """Periodically poll microphone device changes for the tray."""
        if shutting_down:
            return
        if tray is not None and not microphone_refresh_pending.is_set():
            microphone_refresh_pending.set()
            try:
                threading.Thread(
                    target=read_microphone_snapshot,
                    daemon=True,
                    name="microphone-device-refresh",
                ).start()
            except Exception:
                microphone_refresh_pending.clear()
                log.exception("Could not start microphone device refresh")
        root.after(MICROPHONE_REFRESH_INTERVAL_MS, refresh_microphone_menu)

    root.after(0, refresh_microphone_menu)

    if HOTKEY:
        log.info(
            "Ready. Press %s to start/stop dictation, %s to learn a correction; Esc to cancel recording.",
            HOTKEY,
            LEARN_HOTKEY,
        )
    else:
        log.info(
            "Ready. Use the tray menu to record or configure a shortcut; %s learns corrections.",
            LEARN_HOTKEY,
        )

    def poll_events() -> None:
        """Drain queued UI events on the Tkinter thread every 30 ms."""
        nonlocal resume_recovery_pending
        if shutting_down:
            return
        try:
            for _ in range(100):
                name, payload = app.ui_events.get_nowait()
                if name == "starting":
                    overlay.show_starting()
                elif name == "recording":
                    overlay.show_recording()
                    tray.set_recording(True)
                elif name == "stopping":
                    overlay.show_stopping()
                    tray.set_recording(False)
                elif name == "transcribing":
                    overlay.show_transcribing()
                    tray.set_recording(False)
                elif name == "done":
                    overlay.flash("✅ Done", 1200)
                elif name == "error":
                    overlay.flash(payload.get("text", "Error"), 2500)
                elif name == "flash":
                    overlay.flash(payload.get("text", ""), 1500)
                elif name == "cancelled":
                    text = payload.get("text")
                    overlay.flash(text or "Cancelled", 2500 if text else 1000)
                    tray.set_recording(False)
                elif name == "power_resume":
                    app.recover_from_system_resume()
                    schedule_hotkey_recovery()
                elif name == "hotkey_recovery_result":
                    attempt = payload.get("attempt", 1)
                    succeeded = payload.get("succeeded", False)
                    if succeeded:
                        resume_recovery_pending = False
                        if shortcut_capture_close is not None:
                            shortcut_capture_close(False, False)
                    elif attempt < 3 and not shutting_down:
                        resume_recovery_pending = True
                        root.after(
                            1000,
                            lambda next_attempt=attempt + 1: recover_hotkeys(
                                next_attempt
                            ),
                        )
                    else:
                        resume_recovery_pending = False
                        if shortcut_capture_close is not None:
                            shortcut_capture_close(False, False)
                        overlay.flash("Shortcut recovery failed", 2500)
                elif name == "microphone_menu_refresh":
                    if tray is not None:
                        tray.refresh_microphone_menu(snapshot=payload.get("snapshot"))
                elif name == "shortcut_settings":
                    show_shortcut_settings()
                elif name == "api_keys":
                    show_api_keys_dialog()
                elif name == "shortcut_capture_key":
                    finish_shortcut_capture(payload.get("hotkey", ""))
                elif name == "shortcut_capture_cancel":
                    if shortcut_capture_close is not None:
                        shortcut_capture_close()
                elif name == "learn":
                    show_learning_dialog()
                elif name == "vocabulary":
                    show_vocabulary_manager()
                elif name == "stats":
                    show_stats_dialog()
                elif name == "quit":
                    request_application_exit()
                    return
                elif name == "restart":
                    restart_application()
                    return
                elif name == "idle":
                    tray.set_recording(False)
        except queue.Empty:
            pass
        except Exception:
            log.exception("Error in UI event poll")
        root.after(30, poll_events)

    root.after(30, poll_events)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        with contextlib.suppress(Exception):
            root.destroy()
        log.info("Stopped")


if __name__ == "__main__":
    main()
