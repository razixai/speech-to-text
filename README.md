# Speech to Text

Fast Windows tray dictation that transcribes your speech and pastes clean English text straight at your cursor.

---

> **Tiny Overview** *(or watch 👉 **[Complete Overview](https://github.com/user-attachments/assets/fe42a181-57ea-437d-9f57-d9fc0b7f35d3)** 👈, in just 3 minutes)*



https://github.com/user-attachments/assets/9d309f4e-1d76-40ca-8863-2a6e1a61285d



---

### Highlights

- **Flexible AI backends** — Real-time live streaming with **ElevenLabs + Gemini**, sending audio in chunks for faster results.
- **Effortless dictation** — Global toggle shortcut, automatic media pause/resume, and instant cursor paste
- **Reliable & customizable** — Retries with audio backups, custom vocabulary fixes, and usage stats

---

### Quick Start

1. **Install**
   Download **`STT-Setup.exe`** from the **Releases** section and run the installer *(Windows 10/11, 64-bit)*.

2. **Add an API key**
   **GET YOUR FREE API KEYS THEN** Enter your key in the **API Keys** window on first launch *(or anytime via **Tray → Settings → API keys...**)*:

   | Model | Required Key(s) |
   | :--- | :--- |
   | **Gemini** | `GEMINI_API_KEY` |
   | **Groq Whisper** | `GROQ_API_KEY` |
   | **ElevenLabs + Gemini** *(Live)* | `ELEVENLABS_API_KEY` + `GEMINI_API_KEY` |

3. **Set a shortcut & dictate**
   Open **Tray → Settings → Recording shortcut...** to choose your hotkey. Press once to start recording, press again to stop and paste.

---

### Controls

| Action | Shortcut |
| :--- | :--- |
| **Start / Stop recording** | Your configured shortcut |
| **Cancel active recording** | `Esc` |

> Settings, API keys, logs, and recent audio backups are stored locally in `%LOCALAPPDATA%\STT`.
