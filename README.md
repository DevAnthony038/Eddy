# Eddy

A local AI chat app. `eddy.html` is the frontend; a small Python backend
(`server.py`) serves it and connects it to a Llama model running in Ollama on
your own PC. Everything stays on localhost — no cloud, no API keys, no
telemetry, no uploads.

```
http://localhost:8000        - eddy.html (the chat UI)
http://localhost:8000/api/chat  - server.py -> Ollama 127.0.0.1:11434 -> Llama
```

## Requirements

- Windows
- Python 3 (3.10 or newer; either `python` on PATH or the `py` launcher)
- Ollama (https://ollama.com/download)

## Starting Eddy

Double-click `start_eddy.bat`.

The script:

1. Checks that Python and Ollama are installed.
2. Installs Python dependencies (there are none today — the backend is
   standard-library only).
3. Starts `server.py`, which checks Ollama, pulls the model on first run, and
   starts the local server.
4. Opens `http://localhost:8000` in your default browser.

The console window stays open while Eddy runs so you can see what is
happening. Close the window (or press Ctrl+C in it) to stop Eddy.

### Manual start

With Ollama running:

```
python server.py
```

Then open `http://localhost:8000`.

## Model

Eddy uses **llama3.2:3b**.

Why that model:

- Smallest Llama 3.2 variant that still holds a real conversation
  (roughly 2 GB download / ~4–6 GB RAM while in use).
- Runs fast on a normal desktop PC without a high-end GPU.
- Solid general chat/code quality for a 3B model.

Ollama downloads and manages the model itself (`ollama pull llama3.2:3b`).
You can change it with the `EDDY_MODEL` environment variable, e.g.:

```
set EDDY_MODEL=qwen3:4b
python server.py
```

## Answer modes

Fast / Balanced / Deep only change the generation limits (token budget and
context window). They are not different models.

## Conversation log

Every exchange is echoed live to the terminal and appended to
`chat_log.json` (a JSON array next to `server.py`) — the messages Eddy was
sent and the reply. Set `EDDY_LOG=0` to disable the file, or
`EDDY_LOG_FILE` to point it somewhere else.

## Chat API

`POST /api/chat`

```json
{
  "messages": [
    { "role": "user", "content": "Explain Linux permissions." }
  ],
  "mode": "default",
  "stream": true
}
```

- `messages` — the conversation, roles `system`, `user`, `assistant`.
- `mode` — `quick`, `default` or `complex`.
- `stream` — `true` streams an `application/x-ndjson` reply
  (`{"content": ...}` deltas, a final `{"done": true}`), `false` returns a
  single `{"message": "..."}` document.
- Errors come back as JSON with an `error.code` and `error.message`.

## Troubleshooting

- **"Ollama was not found"** — install Ollama and run `start_eddy.bat` again.
- **"Ollama is installed but not running"** — start the Ollama app
  (or run `ollama serve`) and restart Eddy.
- **"Ollama is not running"/timeouts in the browser** — Ollama quit after
  Eddy started. Start it again and send another message.
- **Port 8000 busy** — another Eddy instance is probably running. Stop it,
  or set `EDDY_PORT` to another port before starting.

## Files

| File            | Purpose                                             |
|-----------------|-----------------------------------------------------|
| `eddy.html`     | The chat UI (backend hook, favicon, brand mark)     |
| `icon.svg`      | Brand mark and browser favicon                      |
| `server.py`     | Local backend: static file serving + `/api/chat`    |
| `start_eddy.bat`| One-click launcher                                  |
| `requirements.txt` | Empty by design — no third-party packages needed |
| `chat_log.json` | Conversation log (created on first request)         |