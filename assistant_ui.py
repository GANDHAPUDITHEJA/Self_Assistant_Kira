"""Loopback-only FastAPI bridge for the existing voice assistant."""

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import assistant_fast
from tools import configure_runtime, shutdown_browser

HOST = "127.0.0.1"
PORT = 8000

app = FastAPI(title="Local Voice Assistant")
subscribers: set[asyncio.Queue] = set()
subscribers_lock = threading.Lock()
voice_cancel = threading.Event()
confirmation: dict[str, Any] | None = None
confirmation_event = threading.Event()
confirmation_result = False
voice_listening = threading.Event()
voice_thread_lock = threading.Lock()
voice_thread_running = False


def publish(event: str, data: dict[str, Any]) -> None:
    payload = json.dumps(
        {"id": str(uuid.uuid4()), "event": event, "data": data},
        ensure_ascii=False,
    )
    with subscribers_lock:
        queues = list(subscribers)
    for queue in queues:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def ui_speak(text: str) -> None:
    publish("state.changed", {"state": "speaking", "detail": "Speaking"})
    publish("state.changed", {"state": "ready", "detail": "Ready"})


def ui_confirm(question: str) -> bool:
    global confirmation, confirmation_result
    confirmation_result = False
    confirmation_event.clear()
    confirmation = {"id": str(uuid.uuid4()), "question": question}
    publish("confirmation.requested", {"id": confirmation["id"], "question": question})
    confirmation_event.wait(timeout=60)
    result = confirmation_result
    confirmation = None
    return result


configure_runtime(
    ui_speak,
    assistant_fast.transcribe,
    assistant_fast.record_until_silence,
    False,
    ui_confirm,
)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ConfirmRequest(BaseModel):
    approved: bool


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class OpenSearchResultRequest(BaseModel):
    index: int = Field(ge=1, le=10)


@app.get("/api/health")
def health() -> dict[str, Any]:
    providers = assistant_fast._configured_provider_names()
    return {
        "status": "ok",
        "bind": HOST,
        "providers": providers,
        "voice": True,
    }


@app.get("/api/voice/status")
def voice_status() -> dict[str, Any]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_input = sd.default.device[0]
        input_available = default_input is not None and default_input >= 0
        return {
            "available": input_available,
            "defaultInput": None if default_input is None else str(default_input),
            "devices": len(devices),
        }
    except Exception as error:
        return {"available": False, "error": str(error)}


@app.get("/api/history")
def history() -> dict[str, Any]:
    turns = [
        {
            "id": str(index),
            "role": "user" if item["role"] == "user" else "assistant",
            "text": item["content"],
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        for index, item in enumerate(assistant_fast.history[-100:])
    ]
    return {"turns": turns}


@app.post("/api/search")
def search(request: SearchRequest) -> dict[str, Any]:
    import tools

    reply = tools.search_files(request.query)
    folder = tools.get_active_folder()
    publish("folder.changed", folder)
    saved_results_file = Path.home() / "AppData" / "Local" / "Temp" / "kira_search_results.json"
    results: list[dict[str, Any]] = []
    if saved_results_file.exists():
        try:
            paths = json.loads(saved_results_file.read_text(encoding="utf-8"))
            for index, value in enumerate(paths, start=1):
                path = Path(value)
                results.append({
                    "index": index,
                    "name": path.name,
                    "kind": "folder" if path.is_dir() else "file",
                    "path": str(path),
                })
        except Exception:
            results = []
    return {
        "query": request.query.strip(),
        "reply": reply,
        "results": results,
        "scope": folder,
    }


@app.get("/api/folder/status")
def folder_status() -> dict[str, Any]:
    import tools

    return tools.get_active_folder()


@app.post("/api/search/open")
def open_search_result(request: OpenSearchResultRequest) -> dict[str, Any]:
    import tools

    saved_results_file = Path.home() / "AppData" / "Local" / "Temp" / "kira_search_results.json"
    if not saved_results_file.exists():
        raise HTTPException(status_code=404, detail="No saved search results")
    try:
        paths = json.loads(saved_results_file.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Could not read search results")
    if request.index > len(paths):
        raise HTTPException(status_code=404, detail="Search result not found")
    target = Path(paths[request.index - 1])
    reply = tools.open_folder(str(target)) if target.is_dir() else tools.open_file(str(target))
    try:
        saved_results_file.unlink(missing_ok=True)
    except Exception:
        pass
    publish("response.final", {"text": reply})
    return {"opened": reply, "path": str(target), "name": target.name}


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, Any]:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    import re
    from pathlib import Path
    import tools

    numbered_open = re.match(
        r"^(?:please\s+)?open\s+(?:the\s+)?"
        r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+)"
        r"(?:\s+(?:file|folder|item|result))?[.!?]*$",
        text,
        re.IGNORECASE,
    )
    if numbered_open:
        import tools
        position = numbered_open.group(1).lower()
        reply = tools.open_visible_search_result(position)
        publish("transcript.final", {"text": text})
        publish("response.final", {"text": reply})
        publish("state.changed", {"state": "ready", "detail": "Ready"})
        return {"text": reply}
    wake = assistant_fast._extract_wake_command(text)
    if wake is not None:
        text = wake
    publish("transcript.final", {"text": text})
    publish("state.changed", {"state": "thinking", "detail": "Thinking"})

    def event(type_name: str, data: dict[str, Any]) -> None:
        publish(type_name, data)
        if type_name == "tool.started":
            publish("state.changed", {"state": "acting", "detail": data["name"]})

    reply = assistant_fast.assistant_turn(
        text,
        speak_callback=ui_speak,
        event_callback=event,
    )
    import tools
    publish("folder.changed", tools.get_active_folder())
    publish("response.final", {"text": reply})
    publish("state.changed", {"state": "ready", "detail": "Ready"})
    return {"text": reply}


@app.post("/api/voice/start")
def voice_start() -> dict[str, Any]:
    global voice_thread_running
    if voice_listening.is_set():
        return {"started": True, "continuous": True}
    with voice_thread_lock:
        if voice_thread_running:
            return {"started": True, "continuous": True}
        voice_thread_running = True
    voice_cancel.clear()
    voice_listening.set()
    publish("state.changed", {"state": "listening", "detail": "Listening"})

    def listen() -> None:
        global voice_thread_running
        try:
            while not voice_cancel.is_set():
                try:
                    audio = assistant_fast.record_until_silence(
                        max_seconds=12,
                        minimum_seconds=0.5,
                    )
                except Exception as error:
                    raise RuntimeError(f"Microphone capture failed: {error}") from error
                if voice_cancel.is_set():
                    break
                publish("state.changed", {"state": "thinking", "detail": "Transcribing"})
                try:
                    text = assistant_fast.transcribe(audio)
                except Exception as error:
                    raise RuntimeError(f"Voice transcription failed: {error}") from error
                if not text:
                    publish("state.changed", {"state": "listening", "detail": "Listening"})
                    continue
                normalized = text.strip().lower().strip(".!? ")
                if normalized in {
                    "stop listening",
                    "cancel listening",
                    "exit listening",
                    "go to sleep",
                    "goodbye",
                    "bye",
                }:
                    publish("state.changed", {"state": "ready", "detail": "Ready"})
                    break
                publish("transcript.final", {"text": text})
                wake = assistant_fast._extract_wake_command(text)
                if wake is not None:
                    text = wake
                if not text:
                    continue
                chat(ChatRequest(text=text))
                publish("state.changed", {"state": "listening", "detail": "Listening"})
        except Exception as error:
            publish("state.changed", {"state": "error", "detail": str(error)[:200]})
        finally:
            voice_cancel.set()
            voice_listening.clear()
            with voice_thread_lock:
                voice_thread_running = False
            publish("state.changed", {"state": "ready", "detail": "Ready"})

    threading.Thread(target=listen, daemon=True).start()
    return {"started": True, "continuous": True}


@app.post("/api/voice/cancel")
def voice_cancel_endpoint() -> dict[str, Any]:
    voice_cancel.set()
    voice_listening.clear()
    publish("state.changed", {"state": "ready", "detail": "Ready"})
    return {"cancelled": True}


@app.post("/api/confirm")
def confirm(request: ConfirmRequest) -> dict[str, Any]:
    global confirmation, confirmation_result
    if confirmation is None:
        raise HTTPException(status_code=404, detail="No pending confirmation")
    confirmation_result = request.approved
    confirmation_event.set()
    return {"approved": request.approved}


@app.get("/api/events")
async def events():
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    with subscribers_lock:
        subscribers.add(queue)

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                payload = await queue.get()
                yield f"data: {payload}\n\n"
        finally:
            with subscribers_lock:
                subscribers.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


dist = Path(__file__).resolve().parent / "web" / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")


def run_server(host: str = HOST, port: int = PORT) -> None:
    if host != "127.0.0.1":
        raise RuntimeError("The assistant UI only permits loopback binding")
    print(f"Voice Assistant UI: http://127.0.0.1:{port}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutdown_browser()


if __name__ == "__main__":
    run_server()
