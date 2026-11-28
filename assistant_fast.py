"""
Fast free voice assistant - cloud LLM version.

Run text mode:  python assistant_fast.py
Run voice mode: python assistant_fast.py --voice
"""

import argparse
import datetime
import io
import json
import os
import re
import time
import wave
from pathlib import Path

import pyttsx3
from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOLS, configure_runtime, run_tool, shutdown_browser

load_dotenv()

# ---------------- configuration ----------------
Provider = dict[str, str | None]


def _load_provider_config() -> tuple[list[str], dict[str, Provider]]:
    provider_order = [
        name.strip()
        for name in os.getenv(
            "PROVIDER_ORDER", "groq,openrouter,ollama"
        ).split(",")
        if name.strip()
    ]
    providers = {
        "groq": {
            "base_url": "https://api.groq.com/openai/v1",
            "key": os.getenv("GROQ_API_KEY"),
            "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key": os.getenv("OPENROUTER_API_KEY"),
            "model": os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        },
        "ollama": {
            "base_url": "http://localhost:11434/v1",
            "key": "ollama",
            "model": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        },
        "custom": {
            "base_url": os.getenv("CUSTOM_BASE_URL"),
            "key": os.getenv("CUSTOM_API_KEY"),
            "model": os.getenv("CUSTOM_MODEL"),
        },
    }
    return provider_order, providers


PROVIDER_ORDER, PROVIDERS = _load_provider_config()
STT_MODE = os.getenv("STT", "groq")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")
LOCAL_WHISPER_SIZE = "base"
KEYRING_SERVICE = "voice_assistant"
VOICE_MODE = False

# ---------------- output ----------------
_tts = pyttsx3.init()
_tts.setProperty("rate", 180)


def speak(text: str) -> None:
    print(f"Assistant: {text}")
    _tts.say(text)
    _tts.runAndWait()


# ---------------- brain ----------------
SYSTEM = (
    "You are a voice assistant running on the user's laptop. "
    "Reply in one short sentence. Use the tools whenever the user asks you "
    "to do something on the computer. Never invent tool results. "
    "Never ask for, say, or repeat any password."
)
history: list[dict[str, str]] = []
_clients: dict[str, OpenAI] = {}


def _client(name: str, config: Provider) -> OpenAI:
    if name not in _clients:
        _clients[name] = OpenAI(
            base_url=config["base_url"],
            api_key=config["key"],
            timeout=20,
            max_retries=0,
        )
    return _clients[name]


def chat(messages: list[dict[str, str]]):
    last_error = None
    for name in PROVIDER_ORDER:
        config = PROVIDERS.get(name)
        if (
            not config
            or not config["key"]
            or not config["base_url"]
            or not config["model"]
        ):
            continue
        try:
            response = _client(name, config).chat.completions.create(
                model=config["model"],
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
            )
            return response.choices[0].message, name
        except Exception as error:
            print(f"[{name} failed: {str(error)[:120]}] trying next provider...")
            last_error = error
    raise last_error or RuntimeError(
        "No provider configured. Add a key to .env (see .env.example)."
    )


def _run_direct_tool(name: str, arguments: dict, event_callback=None) -> str:
    if event_callback:
        event_callback("tool.started", {"name": name})
    result = run_tool(name, arguments)
    if event_callback:
        event_callback("tool.completed", {"name": name, "result": result})
    return result


def _deliver_reply(reply: str, speak_callback=None) -> None:
    if speak_callback:
        speak_callback(reply)
    else:
        speak(reply)


def _generate_written_content(request: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "Generate the exact requested text for the user. "
                "Do not add explanations, greetings, or notes outside the document. "
                "For code, output code only. For a letter or email, output a complete ready-to-use document."
            ),
        },
        {"role": "user", "content": request},
    ]
    message, _ = chat(messages)
    return (message.content or "").strip()


def assistant_turn(
    user_text: str,
    speak_callback=None,
    event_callback=None,
) -> str:
    started = time.time()
    text = user_text.strip()
    text = re.sub(r"^\s*plat\b", "play", text, count=1, flags=re.IGNORECASE)

    clear_text = re.match(
        r"^(?:please\s+)?(?:clear|delete)\s+(?:all\s+)?(?:the\s+)?"
        r"(?:text|content|everything)(?:\s+in\s+(?:the\s+)?notepad)?[.!?]*$",
        text,
        flags=re.IGNORECASE,
    )
    if clear_text:
        result = _run_direct_tool("clear_text", {}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct clear text, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    save_file = re.match(
        r"^(?:please\s+)?(?:save\s+(?:(?:the\s+)?(?:file|document)\s*)?"
        r"(?:as\s+|with\s+(?:the\s+)?name\s+|named\s+)|"
        r"save\s+)(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if save_file:
        filename = save_file.group(1).strip().rstrip(".!?")
        result = _run_direct_tool(
            "save_file",
            {"filename": filename},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct save file, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    literal_write = re.match(
        r"^(?:please\s+)?(?:write|type)\s+text(?:\s+in\s+(?:the\s+)?notepad)?\s*:?\s*(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if literal_write:
        literal_text = literal_write.group(1).strip()
        open_result = _run_direct_tool("open_app", {"name": "notepad"}, event_callback)
        result = _run_direct_tool("type_text", {"text": literal_text}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct literal text, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    generated_write = re.match(
        r"^(?:please\s+)?(?:write|type|create|draft|generate)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if generated_write:
        request = generated_write.group(1).strip()
        needs_generation = bool(re.search(
            r"\b(?:program|code|hello world|java|python|c\+\+|c#|javascript|"
            r"letter|application|email|report|essay|poem|paragraph|"
            r"due to|fever|sick|leave|hr|human resources)\b",
            request,
            re.IGNORECASE,
        ))
        if needs_generation:
            open_result = _run_direct_tool("open_app", {"name": "notepad"}, event_callback)
            generated = _generate_written_content(request)
            if not generated:
                result = "I couldn't generate the requested text."
            else:
                result = _run_direct_tool(
                    "type_text",
                    {"text": generated},
                    event_callback,
                )
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[generated text, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    type_text = re.match(
        r"^(?:please\s+)?(?:write|type)\s+(?:in\s+(?:the\s+)?notepad\s*)?(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if type_text and not re.search(r"\b(?:whatsapp|message)\b", text, re.IGNORECASE):
        typed_text = type_text.group(1).strip()
        result = _run_direct_tool(
            "type_text",
            {"text": typed_text},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct type text, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    whatsapp_send = re.match(
        r"^(?:please\s+)?(?:send|write)\s+(?:a\s+)?(?:whatsapp\s+)?message\s+"
        r"(?:to\s+)?(.+?)\s+(?:saying|that)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if whatsapp_send:
        contact = whatsapp_send.group(1).strip().rstrip(".")
        message = whatsapp_send.group(2).strip().rstrip(".")
        result = _run_direct_tool(
            "whatsapp_send_message",
            {"contact": contact, "message": message},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct WhatsApp send, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    whatsapp_search = re.match(
        r"^(?:please\s+)?(?:open|search(?:\s+for)?|find)\s+(?:the\s+)?(?:whatsapp\s+)?"
        r"(?:chat\s+(?:with|for)\s+|contact\s+)(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if whatsapp_search:
        contact = whatsapp_search.group(1).strip().rstrip(".")
        result = _run_direct_tool(
            "whatsapp_search_contact",
            {"contact": contact},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct WhatsApp search, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    direct_website = re.search(
        r"\b(?:whats\s*app(?:\s+web)?|web\s+whats\s*app)\b",
        text,
        flags=re.IGNORECASE,
    )
    if direct_website and re.search(r"\b(?:open|open\s+the|open\s+the\s+site|open\s+the\s+website|and\s+open)\b", text, re.IGNORECASE):
        result = _run_direct_tool("open_app", {"name": "whatsapp web"}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct website, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    website_request = re.match(
        r"^(?:please\s+)?(?:search\s+)?(?:for\s+)?"
        r"(whats\s*app(?:\s+web)?|web\s+whats\s*app|gmail|google|youtube|facebook|instagram|twitter|x)\b"
        r".*?\b(?:open(?:\s+(?:the|a))?\s+(?:site|website)|open\s+(?:it|that)|and\s+open)\b",
        text,
        flags=re.IGNORECASE,
    )
    if website_request:
        site_name = re.sub(r"\s+", " ", website_request.group(1).lower()).strip()
        if "whats" in site_name:
            app_name = "whatsapp web"
        else:
            app_name = site_name
        result = _run_direct_tool("open_app", {"name": app_name}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct website, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    latest_movie_request = re.search(
        r"(?:youtube|you tube).*(?:search|find|look).*(latest)\s+(hindi|tamil|telugu|malayalam|kannada|english|punjabi|bengali)\s+(?:movie|film)",
        text,
        flags=re.IGNORECASE,
    ) or re.search(
        r"(?:search|find|look).*(latest)\s+(hindi|tamil|telugu|malayalam|kannada|english|punjabi|bengali)\s+(?:movie|film)",
        text,
        flags=re.IGNORECASE,
    )
    if latest_movie_request:
        language = latest_movie_request.group(2)
        year = datetime.datetime.now().year
        query = f"latest {language} movie {year} full movie"
        result = _run_direct_tool("play_youtube", {"song": query}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct YouTube latest movie, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    youtube_full_movie = re.match(
        r"^(?:please\s+)?(?:search(?:\s+(?:for|about))?|find|look\s+for)?\s*"
        r"(?:the\s+)?latest\s+([a-z]+)\s+(?:movie|film)\s*(?:and\s+play(?:\s+(?:the\s+)?(?:movie|film|it)))?$",
        text,
        flags=re.IGNORECASE,
    )
    if youtube_full_movie:
        language = youtube_full_movie.group(1).strip()
        year = datetime.datetime.now().year
        query = f"latest {language} movie {year} full movie"
        result = _run_direct_tool("play_youtube", {"song": query}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct YouTube latest movie, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    local_file_search = re.match(
        r"^(?:please\s+)?(?:search|find)\s+(?:for\s+)?(?:the\s+)?"
        r"(?:file|folder|item)?\s*(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if local_file_search:
        query = local_file_search.group(1).strip().rstrip(".!? ")
        if query and not re.search(
            r"\b(?:on\s+(?:the\s+)?(?:web|internet)|google|youtube)\b",
            text,
            re.IGNORECASE,
        ):
            result = _run_direct_tool("search_in_explorer", {"query": query}, event_callback)
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": result})
            print(f"[direct local file search, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    direct_search = re.match(
        r"^(?:please\s+)?(?:google|look\s+up|search\s+(?:the\s+)?(?:web|internet))(?:\s+(?:for|about|what\s+is))?\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if direct_search:
        query = direct_search.group(1).strip().strip(".!? ")
        if query:
            result = _run_direct_tool("web_search", {"query": query}, event_callback)
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": result})
            print(f"[direct search, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    explicit_youtube_search = re.match(
        r"^(?:please\s+)?search\s+(?:on\s+|in\s+)?(?:youtube|you\s*tube)\s*(?:for)?\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if explicit_youtube_search:
        query = explicit_youtube_search.group(1).strip().strip(".!? ")
        if query:
            result = _run_direct_tool(
                "search_youtube",
                {"query": query},
                event_callback,
            )
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct YouTube search, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    youtube_search_request = re.match(
        r"^(?:please\s+)?(?:open\s+(?:youtube|you\s*tube)\s+(?:and\s+)?"
        r"|search(?:\s+(?:for|about))?\s+)"
        r"(?:search\s+)?(.+?)(?:\s+(?:in|on)\s+(?:youtube|you\s*tube))?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if youtube_search_request and re.search(
        r"\byou(?:\s*)tube\b|\bsearch\b",
        text,
        flags=re.IGNORECASE,
    ):
        query = youtube_search_request.group(1).strip().strip(".!? ")
        query = re.sub(
            r"^(?:and\s+)?search(?:\s+(?:for|about))?\s+",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        if query:
            result = _run_direct_tool(
                "search_youtube",
                {"query": query},
                event_callback,
            )
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct YouTube search, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    first_youtube_result = re.match(
        r"^(?:please\s+)?(?:open|play)\s+(?:the\s+)?(?:first|top)\s+"
        r"(?:video|result)(?:\b.*(?:youtube|you\s*tube|search).*)?$",
        text,
        flags=re.IGNORECASE,
    )
    if first_youtube_result:
        result = _run_direct_tool(
            "play_youtube",
            {"song": "__first_youtube_result__"},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct YouTube first result, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    youtube_ordinal = re.match(
        r"^(?:please\s+)?(?:open|play)\s+(?:the\s+)?"
        r"(?:(\d+)(?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
        r"\s+(?:video|result)(?:\s+(?:on\s+(?:youtube|you\s*tube)))?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if youtube_ordinal:
        words = {
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
        }
        ordinal_text = youtube_ordinal.group(1)
        if ordinal_text:
            position = int(ordinal_text)
        else:
            position = words.get(text.lower().split()[-3] if len(text.lower().split()) >= 3 else "", 0)
            if position == 0:
                for word, number in words.items():
                    if re.search(rf"\b{word}\b", text, re.IGNORECASE):
                        position = number
                        break
        if position > 0:
            result = _run_direct_tool(
                "play_youtube",
                {"song": f"__youtube_result__{position}__"},
                event_callback,
            )
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct YouTube ordinal result, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    open_youtube_home = re.match(
        r"^(?:please\s+)?(?:open|go\s+to)\s+(?:the\s+)?(?:youtube|you\s*tube)\s*(?:home(?:page)?)?[.!?]*$",
        text,
        flags=re.IGNORECASE,
    )
    if open_youtube_home:
        result = _run_direct_tool("open_app", {"name": "youtube"}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct YouTube home, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    youtube_play = re.match(
        r"^(?:please\s+)?(?:play|plat|open)\s+(.+?)(?:\s+on\s+(?:youtube|you tube))?$",
        text,
        flags=re.IGNORECASE,
    )
    if (
        youtube_play
        and re.search(r"\byou(?:\s*)tube\b|^play\b", text, re.IGNORECASE)
        and not re.match(r"^(?:please\s+)?open\s+(?:the\s+)?you(?:\s*)tube$", text, re.IGNORECASE)
    ):
        song = youtube_play.group(1).strip()
        if song:
            result = _run_direct_tool("play_youtube", {"song": song}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct YouTube, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    search_match = re.match(
        r"^(?:please\s+)?(?:search|find)\s+(?:for\s+)?(?:the\s+)?(?:file|folder|item)?\s*(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if search_match:
        query = search_match.group(1).strip().rstrip(".!? ")
        if query and not re.search(r"\b(?:google|youtube|web)\b", query, re.IGNORECASE):
            result = _run_direct_tool("search_in_explorer", {"query": query}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct file search, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    scroll_match = re.match(
        r"^(?:please\s+)?(?:scroll\s+(?:to\s+)?|go\s+)?(down|up|top|bottom)"
        r"(?:\s+(?:of\s+)?(?:the\s+)?(?:youtube|youtube\s*page|page|results|window|screen))?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if scroll_match:
        direction = scroll_match.group(1).lower()
        youtube_scroll = bool(re.search(r"\b(?:youtube|youtube\s*page|results)\b", text, re.IGNORECASE))
        if youtube_scroll:
            action = "scroll_down" if direction == "down" else "scroll_up" if direction == "up" else direction
            result = _run_direct_tool("youtube_control", {"action": action}, event_callback)
        else:
            result = _run_direct_tool("scroll_page", {"action": direction}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct scroll, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    folder_navigation_match = re.match(
        r"^(?:please\s+)?(?:go\s+|come\s+)?(back|forward)(?:\s+(?:a\s+)?(?:folder|page|window))?[.!?]*$",
        text,
        flags=re.IGNORECASE,
    )
    if folder_navigation_match:
        direction = folder_navigation_match.group(1).lower()
        tool_name = "explorer_forward" if direction == "forward" else "explorer_back"
        result = _run_direct_tool(tool_name, {}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct Explorer navigation, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    open_numbered_item = re.match(
        r"^(?:please\s+)?open\s+(?:the\s+)?"
        r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th)?)"
        r"(?:\s+(?:item|file|folder|directory))?"
        r"(?:\s+(?:from\s+top|at\s+top))?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if open_numbered_item:
        position = re.sub(r"(st|nd|rd|th)$", "", open_numbered_item.group(1).lower())
        kind_match = re.search(r"\b(file|folder|directory)\b", text, re.IGNORECASE)
        kind = kind_match.group(1).lower() if kind_match else "any"
        if kind == "directory":
            kind = "folder"
        result = _run_direct_tool(
            "open_visible_search_result",
            {"position": position},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct visible search result, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result
        saved_results_file = Path.home() / "AppData" / "Local" / "Temp" / "kira_search_results.json"
        saved_paths: list[str] = []
        if saved_results_file.exists():
            try:
                saved_paths = json.loads(saved_results_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                saved_paths = []
        words = {
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
        }
        index = words.get(position, int(position) if position.isdigit() else 0)
        if index >= 1 and index <= len(saved_paths):
            tool_name = "open_folder" if Path(saved_paths[index - 1]).is_dir() else "open_file"
            result = _run_direct_tool(
                tool_name,
                {"name": saved_paths[index - 1]},
                event_callback,
            )
            try:
                saved_results_file.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            result = _run_direct_tool(
                "open_item",
                {"position": position, "kind": kind},
                event_callback,
            )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct numbered item, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    youtube_seek_match = re.match(
        r"^(?:please\s+)?(?:(?:go|skip|fast)\s+)?"
        r"(?P<direction>forward|back|backward|rewind)"
        r"(?:\s+10\s*(?:seconds?|secs?))?"
        r"(?:\s+(?:by\s+)?(?P<count>\d+|once|twice|thrice|one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s*(?:times?|x)?)?"
        r"(?:\s+(?:in|on)\s+(?:the\s+)?(?:video|youtube))?$",
        text,
        flags=re.IGNORECASE,
    )
    if youtube_seek_match:
        direction = youtube_seek_match.group("direction").lower()
        action = "back" if direction in {"back", "backward", "rewind"} else "forward"
        count_text = youtube_seek_match.group("count")
        count_words = {
            "once": 1,
            "twice": 2,
            "thrice": 3,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
        }
        count = (
            count_words.get(count_text.lower(), int(count_text) if count_text.isdigit() else 1)
            if count_text
            else 1
        )
        result = _run_direct_tool(
            "youtube_control",
            {"action": action, "count": count},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct YouTube seek, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    youtube_control_match = re.match(
        r"^(?:please\s+)?(pause|stop|resume|play|mute|unmute|full\s*screen|fullscreen|forward|back|next|previous)(?:\s+(?:the\s+)?(?:video|youtube|song))?$",
        text,
        flags=re.IGNORECASE,
    )
    if youtube_control_match:
        action = youtube_control_match.group(1).lower().replace(" ", "")
        action = "fullscreen" if action in {"fullscreen", "fullscreeen"} else action
        action = "pause" if action == "stop" else action
        action = "play" if action == "resume" else action
        action = "back" if action == "previous" else action
        action = "forward" if action == "forward" else action
        if action in {"pause", "play", "mute", "unmute", "fullscreen", "forward", "back", "next"}:
            result = _run_direct_tool("youtube_control", {"action": action}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct YouTube control, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    brightness_match = re.match(
        r"^(?:please\s+)?(?:(?:turn\s+|set\s+)?(?:(?:the\s+)?(?:laptop|screen|display)\s+)?brightness\s+(?P<direction>up|down|higher|lower)"
        r"|(?P<verb>increase|raise|decrease|lower)\s+(?:(?:the\s+)?(?:laptop|screen|display)\s+)?brightness)"
        r"(?:\s+(?:by\s+)?(?P<count>\d+)(?:\s*(?:times?|steps?))?)?$",
        text,
        flags=re.IGNORECASE,
    )
    if brightness_match:
        direction = (
            brightness_match.group("direction")
            or brightness_match.group("verb")
        ).lower()
        action = "down" if direction in {"down", "lower", "decrease"} else "up"
        count = int(brightness_match.group("count") or 1)
        result = _run_direct_tool(
            "set_brightness",
            {"action": action, "count": count},
            event_callback,
        )
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct brightness, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    open_explicit_app_match = re.match(
        r"^(?:please\s+)?open\s+(?:the\s+)?app(?:lication)?\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if open_explicit_app_match:
        app_name = open_explicit_app_match.group(1).strip().rstrip(".!?")
        result = _run_direct_tool("open_app", {"name": app_name}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct open app, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    open_app_match = re.match(
        r"^(?:please\s+)?open\s+(?:the\s+)?(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if open_app_match:
        app_name = open_app_match.group(1).strip().rstrip(".!?")
        is_folder = bool(re.search(r"\b(?:desktop|documents|downloads|pictures|music|videos)(?:\s+folder)?$", app_name, re.IGNORECASE))
        is_file = bool(re.search(r"\.(?:txt|pdf|docx?|xlsx?|pptx?|png|jpe?g|csv|zip|exe)$", app_name, re.IGNORECASE))
        if app_name and is_file:
            result = _run_direct_tool("open_file", {"name": app_name}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct open file, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result
        if app_name and not is_folder:
            folder_result = _run_direct_tool("open_folder", {"name": app_name}, event_callback)
            if re.match(r"^Opened\b", folder_result, re.IGNORECASE):
                history.extend([
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": folder_result},
                ])
                print(f"[direct open active-folder item, {time.time() - started:.1f}s]")
                _deliver_reply(folder_result, speak_callback)
                return folder_result
            result = _run_direct_tool("open_app", {"name": app_name}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct open app, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    close_app_match = re.match(
        r"^(?:please\s+)?close\s+(?:the\s+)?(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if close_app_match:
        app_name = close_app_match.group(1).strip().rstrip(".!?")
        result = _run_direct_tool("close_app", {"name": app_name}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct close app, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    volume_match = re.match(
        r"^(?:please\s+)?(?:turn\s+)?(volume\s+(?:up|down)|mute|unmute)\b",
        text,
        flags=re.IGNORECASE,
    )
    if volume_match:
        volume_action = volume_match.group(1).lower()
        action = (
            "mute" if volume_action == "mute"
            else "unmute" if volume_action == "unmute"
            else "down" if "down" in volume_action
            else "up"
        )
        result = _run_direct_tool("set_volume", {"action": action}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct volume, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    open_folder_match = re.match(
        r"^(?:please\s+)?open\s+(?:my\s+)?(.+?)(?:\s+folder)?$",
        text,
        flags=re.IGNORECASE,
    )
    if open_folder_match:
        folder_name = open_folder_match.group(1).strip()
        if re.search(r"\.[a-z0-9]{1,10}$", folder_name, re.IGNORECASE):
            folder_name = ""
        if folder_name.lower() in {"desktop", "documents", "downloads", "pictures", "music", "videos"}:
            folder_name = folder_name.capitalize()
        if folder_name:
            result = _run_direct_tool("open_folder", {"name": folder_name}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct folder, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    open_file_match = re.match(
        r"^(?:please\s+)?(?:open|find)\s+(?:the\s+)?(?:file\s+)?(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if open_file_match:
        file_name = open_file_match.group(1).strip().rstrip(".!?")
        if re.search(r"\.[a-z0-9]{1,10}$", file_name, re.IGNORECASE) or re.search(r"\bresume\b", file_name, re.IGNORECASE):
            result = _run_direct_tool("open_file", {"name": file_name}, event_callback)
            history.extend([
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": result},
            ])
            print(f"[direct file, {time.time() - started:.1f}s]")
            _deliver_reply(result, speak_callback)
            return result

    power_match = re.match(
        r"^(?:please\s+)?(sleep|restart|shut\s*down)(?:\s+(?:the\s+)?(?:computer|laptop|pc))?$",
        text,
        flags=re.IGNORECASE,
    )
    if power_match:
        action = power_match.group(1).lower().replace(" ", "")
        action = "shutdown" if action == "shutdown" else action
        result = _run_direct_tool("power", {"action": action}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct power, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    if re.search(r"\b(?:what(?:'s|\s+is)\s+(?:the\s+)?time|tell\s+me\s+(?:the\s+)?time)\b", text, re.IGNORECASE):
        result = _run_direct_tool("get_time", {}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct time, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    if re.search(r"^(?:please\s+)?(?:take\s+(?:a\s+)?screenshot|capture\s+(?:the\s+)?screen)\b", text, re.IGNORECASE):
        result = _run_direct_tool("take_screenshot", {}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct screenshot, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    if re.search(r"^(?:please\s+)?lock(?:\s+the)?\s*(?:screen|computer)?$", text, re.IGNORECASE):
        result = _run_direct_tool("lock_screen", {}, event_callback)
        history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": result},
        ])
        print(f"[direct lock, {time.time() - started:.1f}s]")
        _deliver_reply(result, speak_callback)
        return result

    history.append({"role": "user", "content": user_text})
    message, provider = chat([{"role": "system", "content": SYSTEM}] + history[-10:])
    if message.tool_calls:
        results = []
        for call in message.tool_calls:
            if event_callback:
                event_callback("tool.started", {"name": call.function.name})
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            results.append(run_tool(call.function.name, arguments))
            if event_callback:
                event_callback(
                    "tool.completed",
                    {"name": call.function.name, "result": results[-1]},
                )
        reply = " ".join(results)
    else:
        reply = message.content or "Sorry, I didn't get that."
    history.append({"role": "assistant", "content": reply})
    print(f"[{provider}, {time.time() - started:.1f}s]")
    if speak_callback:
        speak_callback(reply)
    else:
        speak(reply)
    return reply


# ---------------- speech-to-text ----------------
_whisper = None


def record_until_silence(
    max_seconds: float = 15,
    silence_seconds: float = 0.9,
    minimum_seconds: float = 5.0,
    rate: int = 16000,
):
    import numpy as np
    import sounddevice as sd

    chunk = int(rate * 0.1)
    frames, silent, started = [], 0, False
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32") as stream:
        noise = np.mean([np.abs(stream.read(chunk)[0]).mean() for _ in range(3)])
        threshold = max(noise * 3, 0.01)
        print("Listening...")
        elapsed_seconds = 0.0
        for _ in range(int(max_seconds / 0.1)):
            data, _ = stream.read(chunk)
            frames.append(data.copy())
            elapsed_seconds += 0.1
            if np.abs(data).mean() > threshold:
                started, silent = True, 0
            elif started and elapsed_seconds >= minimum_seconds:
                silent += 1
                if silent >= silence_seconds / 0.1:
                    break
    return np.concatenate(frames).flatten()


def _to_wav_bytes(audio, rate: int = 16000) -> bytes:
    import numpy as np

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _transcribe_local(audio) -> str:
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(LOCAL_WHISPER_SIZE, device="cpu", compute_type="int8")
    options = {"language": STT_LANGUAGE} if STT_LANGUAGE else {}
    segments, _ = _whisper.transcribe(audio, vad_filter=True, **options)
    return " ".join(segment.text for segment in segments).strip()


def transcribe(audio) -> str:
    key = os.getenv("GROQ_API_KEY")
    if STT_MODE == "groq" and key:
        try:
            client = OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=key,
                timeout=20,
                max_retries=0,
            )
            options = {"language": STT_LANGUAGE} if STT_LANGUAGE else {}
            response = client.audio.transcriptions.create(
                model=GROQ_STT_MODEL,
                file=("speech.wav", _to_wav_bytes(audio)),
                **options,
            )
            return response.text.strip()
        except Exception as error:
            print(f"[cloud STT failed: {str(error)[:100]}] using local Whisper...")
    return _transcribe_local(audio)


# ---------------- application entry point ----------------
def _configured_provider_names() -> list[str]:
    return [
        name
        for name in PROVIDER_ORDER
        if PROVIDERS.get(name)
        and PROVIDERS[name]["key"]
        and PROVIDERS[name]["base_url"]
        and PROVIDERS[name]["model"]
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the voice assistant.")
    parser.add_argument("--voice", action="store_true", help="use microphone")
    parser.add_argument("--ui", action="store_true", help="run the local browser UI")
    parser.add_argument("--host", default="127.0.0.1", help="UI host")
    parser.add_argument("--port", type=int, default=8000, help="UI port")
    parser.add_argument(
        "--save-password",
        metavar="LABEL",
        help="securely store a password, e.g. --save-password gmail",
    )
    return parser.parse_args()


def _save_password(label: str) -> None:
    import getpass
    import keyring

    keyring.set_password(
        KEYRING_SERVICE,
        label,
        getpass.getpass("Password (hidden): "),
    )
    print(f"Saved '{label}' in your system keyring.")


def _read_user_text(voice_mode: bool) -> str:
    if voice_mode:
        text = transcribe(record_until_silence(max_seconds=12))
        print(f"You: {text}")
        return text
    return input("\nYou: ").strip()


def _extract_wake_command(text: str) -> str | None:
    """Return command text if Kira or a legacy Jarvis wake word was detected."""
    cleaned = text.lower().strip().strip(".!?, ")
    match = re.match(
        r"^(?:hey\s+)?(?:kira|jarvis|jarvic|jervis)\b[\s,.]*(.*)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip()

def _wait_for_voice_wake() -> str:
    while True:
        text = transcribe(record_until_silence(max_seconds=12, minimum_seconds=5))
        print(f"You: {text}")
        command = _extract_wake_command(text)
        if command is not None:
            speak("Yes?")
            if command:
                return command

            # Wake word alone: listen once for the actual command.
            text = transcribe(record_until_silence(max_seconds=12))
            print(f"You: {text}")
            return text.strip()

def _run_loop(voice_mode: bool) -> None:
    speak("Hello, I'm ready.")
    while True:
        try:
            if voice_mode:
                print("Waiting for 'Hey Kira'...")
                text = _wait_for_voice_wake()
            else:
                text = _read_user_text(False)
                # Text mode also accepts the wake word for a consistent interface.
                wake_command = _extract_wake_command(text)
                if wake_command is not None:
                    text = wake_command
            if not text:
                continue
            if text.lower().strip(".! ") in {"quit", "exit", "bye", "stop"}:
                speak("Goodbye.")
                return
            assistant_turn(text)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"Error: {error}")
            
def main() -> None:
    global VOICE_MODE
    args = _parse_args()
    if args.ui:
        from assistant_ui import run_server

        run_server(host=args.host, port=args.port)
        return
    VOICE_MODE = args.voice
    configure_runtime(speak, transcribe, record_until_silence, VOICE_MODE)
    if args.save_password:
        _save_password(args.save_password)
        return

    ready = _configured_provider_names()
    print(f"Providers in use (in order): {', '.join(ready) or 'NONE'}")
    try:
        _run_loop(args.voice)
    finally:
        shutdown_browser()


if __name__ == "__main__":
    main()
