# 🧭 ATLAS

### Almost Thinking Local AI System — a local-first voice and command assistant for secure desktop automation.

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-API%20Layer-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Ollama-Local%20LLM-111111?logo=ollama&logoColor=white" alt="Ollama">
  <img src="https://img.shields.io/badge/ChromaDB-Semantic%20Memory-5A67D8" alt="ChromaDB">
  <img src="https://img.shields.io/badge/Version-0.1.0-22C55E" alt="Version 0.1.0">
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white" alt="Windows 10/11">
</p>

## 🚀 Overview

ATLAS is a local AI command system that turns natural language (text or voice) into safe, verifiable desktop actions. It combines a fast rule-based classifier with local LLM fallback, enforces a security pipeline (validation, risk gating, confirmations, PIN checks), and keeps context across sessions with a sliding window plus semantic memory in ChromaDB. It is built for developers and power users who want private, low-latency automation without sending control logic to cloud services.

## ✨ Key Highlights

- Local-first orchestration with [Ollama](https://ollama.com/) and offline-friendly defaults.
- Dual interaction modes: CLI command execution and FastAPI endpoints (including WebSocket streaming).
- Voice pipeline with push-to-talk and wake-word detection via [openWakeWord](https://github.com/dscripka/openWakeWord).
- Secure action execution through a fixed action map, validation, and risk-based confirmation gates.
- Persistent semantic memory with [ChromaDB](https://www.trychroma.com/) plus background context pruning.
- Macro/chain support that still routes through the same security and execution pipeline.

## 📋 Features

### Frontend Features (HUD/API Client Ready)

- 🧩 Real-time WebSocket event stream for live assistant state updates.
- 📡 Client-agnostic API design for CLI, voice loop, and future HUD integration.
- 🔐 Token-authenticated endpoints for protected command/control access.

### Backend Features

- 🤖 Hybrid intent routing: fast regex classifier first, LLM fallback for unknown prompts.
- 🧠 Three-layer memory model: semantic facts/summaries + sliding context + background pruner.
- 🎙️ Voice command capture with Whisper transcription and TTS playback support.
- 🛡️ Risk-tiered action gates (low/medium/high/critical) with confirmation and PIN protection.
- ⚙️ Action execution + verification pipeline with rollback logging for safety.
- 🔁 Macros/chains with parameter injection and per-step failure handling.

## 🛠️ Tech Stack

### Frontend

- [WebSocket](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API) (for HUD/client streaming)
- [JSON](https://www.json.org/json-en.html) payload contracts

### Backend

- [Python 3.11+](https://www.python.org/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Uvicorn](https://www.uvicorn.org/)
- [Pydantic](https://docs.pydantic.dev/)
- [SlowAPI](https://slowapi.readthedocs.io/) (rate limiting)
- [Ollama](https://ollama.com/) (local model inference)
- [ChromaDB](https://www.trychroma.com/)
- [Sentence Transformers](https://www.sbert.net/)
- [openWakeWord](https://github.com/dscripka/openWakeWord)
- [OpenAI Whisper](https://github.com/openai/whisper)
- [SoundDevice](https://python-sounddevice.readthedocs.io/)

### DevOps / Tools

- [setuptools](https://setuptools.pypa.io/)
- [Rich](https://rich.readthedocs.io/)
- [bcrypt](https://pypi.org/project/bcrypt/)
- [edge-tts](https://github.com/rany2/edge-tts)
- [FFmpeg / ffplay](https://ffmpeg.org/)

## 📦 Installation

### Prerequisites

- Python 3.11+
- Windows 10/11
- [Ollama](https://ollama.com/) installed and running
- A local model pulled in Ollama (default: mistral:7b)
- Optional voice dependencies:
  - Working microphone/audio drivers
  - FFmpeg (for ffplay)
  - edge-tts

### Step-by-Step Setup

1. Clone the repo

```bash
git clone https://github.com/<your-username>/atlas.git
cd atlas
```

2. Create and activate a virtual environment

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install ATLAS and dependencies

```bash
pip install -e .
pip install fastapi uvicorn requests rich slowapi pydantic bcrypt keyboard numpy sounddevice chromadb sentence-transformers ollama openai-whisper openwakeword edge-tts pyreadline3
```

4. Pull and start local LLM

```bash
ollama pull mistral:7b
ollama serve
```

5. Setup environment variables (.env example)

```env
# .env.example (optional shell-level overrides)
TRANSFORMERS_OFFLINE=1
HF_HUB_DISABLE_PROGRESS_BARS=1
ATLAS_MODEL=mistral:7b
ATLAS_API_HOST=127.0.0.1
ATLAS_API_PORT=8000
```

6. Configure runtime settings

- Update config.json for voice, wake-word, allowed paths, and API token.
- Run initial secure setup:

```bash
atlas --setup
```

7. Run the project

```bash
atlas
```

## 📖 Usage Guide

### Flow 1: Quick CLI Command

1. Start ATLAS with atlas.
2. Enter a direct command such as open chrome or what time is it.
3. ATLAS classifies, executes, verifies, and returns the result panel.

### Flow 2: Safe File Operation with Risk Gating

1. Run a high-risk command such as delete C:/Users/<user>/Desktop/test.txt.
2. ATLAS validates path and risk tier.
3. You must pass confirmation/PIN gate before execution proceeds.

### Flow 3: Voice-Driven Command (Wake Word)

1. Enable wake_word_enabled in config.json.
2. Start atlas and wait for wake listener activation.
3. Say wake phrase + command (for example: hey atlas, open notepad).
4. ATLAS transcribes speech, executes the action, and speaks response.

### Flow 4: API + Real-Time Events

1. Start atlas (or API server via uvicorn).
2. Send authenticated requests to /status, /command, /history, and /macros/run.
3. Connect to /ws to stream user/action/done/error events in real time.

## 📁 Project Structure

```text
.
├── api/                    # FastAPI service layer and websocket manager
│   ├── server.py           # REST + websocket endpoints, auth, rate limiting
│   └── ws_manager.py       # Broadcast manager for connected clients
├── main.py                 # CLI entrypoint, startup orchestration, REPL loop
├── memory.py               # Sliding + semantic memory (ChromaDB integration)
├── context_pruner.py       # Background summarizer for long-session compression
├── wake_word.py            # openWakeWord listener and voice command capture
├── voice.py                # Push-to-talk, transcription, and TTS playback
├── executor.py             # Secure action dispatch via ACTION_MAP
├── validator.py            # Parameter/action validation layer
├── verifier.py             # Post-execution verification checks
├── security.py             # Confirmation gates + PIN setup/verification
├── macros.py               # Macro/chain storage and execution
├── history.py              # Command history logging and rerun support
├── pc_control.py           # System-level desktop action implementations
├── settings.py             # Config loading/normalization/persistence
├── config.json             # Runtime configuration
├── macros.json             # User-defined macro chains
├── pyproject.toml          # Packaging + console script definition
└── test_*.py               # Self-tests and integration checks
```

## 🧪 Development

### Run tests / self-checks

```bash
python phase2_selftest.py
python openwakeword_migration_selftest.py
python test_voice_integration.py
python test_wake_tuning.py
python test_audio.py
```

### Build project

```bash
pip install build
python -m build
```

### Run locally

```bash
atlas
```

### Run API server only

```bash
uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload
```

### Docker

No official Dockerfile is included yet. Local Python + Ollama runtime is the supported development path today.

## 📄 License

License is currently not declared in the repository.

<p align="center">
  Made with ❤️ by me.
</p>
