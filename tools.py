"""Browser and desktop tools used by the voice assistant."""

import datetime
import functools
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import pyautogui

OS = platform.system()
CDP_PORT = 9222
KEYRING_SERVICE = "voice_assistant"
pyautogui.FAILSAFE = True
_recent_app = ""
_active_folder: Path | None = None
_search_results: list[Path] = []
_last_search_query = ""
_last_explorer_query = ""
_search_results_file = Path.home() / "AppData" / "Local" / "Temp" / "kira_search_results.json"


def _focus_notepad() -> bool:
    if OS != "Windows":
        return False
    try:
        import win32gui

        windows = []

        def enum_windows(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).lower()
                if "notepad" in title:
                    windows.append(hwnd)

        win32gui.EnumWindows(enum_windows, None)
        if windows:
            win32gui.ShowWindow(windows[-1], 9)
            win32gui.SetForegroundWindow(windows[-1])
            time.sleep(0.4)
            return True
    except Exception:
        pass
    return False


def _chrome_user_data_dir() -> str:
    """Return Chrome's normal profile directory, unless explicitly overridden."""
    override = os.getenv("CHROME_PROFILE_DIR")
    if override:
        return os.path.abspath(override)
    if OS == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return os.path.join(local_app_data, "Google", "Chrome", "User Data")
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    if OS == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return os.getenv(
        "XDG_CONFIG_HOME",
        os.path.expanduser("~/.config"),
    ) + "/google-chrome"


PROFILE_DIR = _chrome_user_data_dir()


def _last_used_chrome_profile() -> str:
    """Choose Chrome's most recently used profile instead of the profile chooser."""
    state_path = os.path.join(PROFILE_DIR, "Local State")
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        last_used = state.get("profile", {}).get("last_used")
        if (
            isinstance(last_used, str)
            and last_used
            and os.path.isdir(os.path.join(PROFILE_DIR, last_used))
        ):
            return last_used

        info_cache = state.get("profile", {}).get("info_cache", {})
        candidates = [
            (details.get("active_time", 0), name)
            for name, details in info_cache.items()
            if isinstance(details, dict)
            and os.path.isdir(os.path.join(PROFILE_DIR, name))
        ]
        if candidates:
            return max(candidates)[1]
    except (OSError, json.JSONDecodeError):
        pass
    return "Default"

ToolFunction = Callable[..., str]
_speak: Callable[[str], None] | None = None
_transcribe: Callable[..., str] | None = None
_record_until_silence: Callable[..., Any] | None = None
_voice_mode = False
_confirm_callback: Callable[[str], bool] | None = None


def configure_runtime(
    speak: Callable[[str], None],
    transcribe: Callable[..., str],
    record_until_silence: Callable[..., Any],
    voice_mode: bool,
    confirm_callback: Callable[[str], bool] | None = None,
) -> None:
    """Provide speech callbacks without coupling this module to the CLI."""
    global _speak, _transcribe, _record_until_silence, _voice_mode
    global _confirm_callback
    _speak = speak
    _transcribe = transcribe
    _record_until_silence = record_until_silence
    _voice_mode = voice_mode
    _confirm_callback = confirm_callback


_pw = None
_context = None
_page = None

BROWSER_STATE_FILE = Path(__file__).resolve().parent / ".browser_state.json"
MAX_RECENT_TABS = 3


def _load_browser_state() -> dict[str, Any]:
    try:
        with BROWSER_STATE_FILE.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
            return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_browser_state(state: dict[str, Any]) -> None:
    try:
        BROWSER_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _remember_tab(url: str, title: str = "") -> None:
    if not url or not url.startswith(("http://", "https://")):
        return
    state = _load_browser_state()
    recent = state.get("recent_tabs", [])
    if not isinstance(recent, list):
        recent = []
    recent = [item for item in recent if not (
        isinstance(item, dict) and item.get("url") == url
    )]
    recent.insert(0, {"url": url, "title": title or url})
    state["recent_tabs"] = recent[:MAX_RECENT_TABS]
    if "youtube.com/results" in url:
        state["last_youtube_search"] = url
    _save_browser_state(state)


def _find_existing_page(url_fragment: str = ""):
    if _context is None:
        return None
    try:
        pages = list(_context.pages)
    except Exception:
        return None
    for page in pages:
        try:
            if not page.is_closed() and (not url_fragment or url_fragment in page.url):
                return page
        except Exception:
            continue
    return None


def _find_or_create_recent_page(url_fragment: str = "", fallback_url: str = ""):
    page = _find_existing_page(url_fragment)
    if page is not None:
        return page
    page = _get_page()
    if fallback_url:
        page.goto(fallback_url, wait_until="domcontentloaded", timeout=15000)
    return page


def _restore_recent_tabs() -> None:
    state = _load_browser_state()
    recent = state.get("recent_tabs", [])
    if not isinstance(recent, list):
        return
    try:
        page = _get_page()
        existing_urls = {open_page.url for open_page in _context.pages}
        restored = 0
        for item in recent[:MAX_RECENT_TABS]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in existing_urls:
                continue
            try:
                if restored == 0 and len(_context.pages) == 1:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    _remember_tab(url, page.title())
                else:
                    restored_page = _context.new_page()
                    restored_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    _remember_tab(url, restored_page.title())
            except Exception:
                continue
            restored += 1
    except Exception:
        return


def _chrome_path() -> str:
    env = os.getenv("CHROME_PATH")
    if env:
        return env
    if OS == "Windows":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                path = os.path.join(
                    base, "Google", "Chrome", "Application", "chrome.exe"
                )
                if os.path.exists(path):
                    return path
    elif OS == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    else:
        found = shutil.which("google-chrome") or shutil.which("chromium")
        if found:
            return found
    raise RuntimeError("Chrome not found. Set CHROME_PATH in .env")


def _port_open() -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", CDP_PORT)) == 0


def _get_page():
    """Attach to the user's normal Chrome profile through the CDP port."""
    global _pw, _context, _page
    if _page is not None:
        try:
            if not _page.is_closed():
                _page.evaluate("1")
                return _page
        except Exception:
            pass
        _page = None

    from playwright.sync_api import sync_playwright

    if _pw is None:
        _pw = sync_playwright().start()
    if not _port_open():
        profile_directory = _last_used_chrome_profile()
        subprocess.Popen(
            [
                _chrome_path(),
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                f"--profile-directory={profile_directory}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        for _ in range(40):
            if _port_open():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Chrome did not start.")
    browser = _pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    _context = browser.contexts[0]
    _page = _context.pages[0] if _context.pages else _context.new_page()
    return _page


def shutdown_browser() -> None:
    """Stop Playwright without closing the user's Chrome window."""
    global _pw, _context, _page
    if _pw:
        _pw.stop()
    _pw = _context = _page = None


def _reset_browser() -> None:
    global _context, _page
    _context = _page = None


def _retry_if_closed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as error:
            message = str(error).lower()
            if any(word in message for word in ("closed", "disconnected", "target")):
                _reset_browser()
                return func(*args, **kwargs)
            raise

    return wrapper


def _first_youtube_watch_url(search_query: str) -> str:
    import urllib.request

    search_url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(search_query)
    )
    with urllib.request.urlopen(search_url, timeout=15) as response:
        html = response.read().decode("utf-8", "ignore")
    video_ids = list(dict.fromkeys(
        re.findall(r"/watch\?v=([A-Za-z0-9_-]{11})", html)
    ))
    if not video_ids:
        return ""
    return "https://www.youtube.com/watch?v=" + video_ids[0]


def _youtube_watch_url(search_query: str, result_index: int = 0) -> str:
    import urllib.request

    search_url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(search_query)
    )
    with urllib.request.urlopen(search_url, timeout=15) as response:
        html = response.read().decode("utf-8", "ignore")
    video_ids = list(dict.fromkeys(
        re.findall(r"/watch\?v=([A-Za-z0-9_-]{11})", html)
    ))
    if result_index < 0 or result_index >= len(video_ids):
        return ""
    return "https://www.youtube.com/watch?v=" + video_ids[result_index]


@_retry_if_closed
def play_youtube(song: str) -> str:
    ordinal_match = re.match(
        r"^__youtube_result__(\d+)__$",
        song.strip().lower(),
    )
    if ordinal_match:
        state = _load_browser_state()
        last_search = state.get("last_youtube_search")
        if isinstance(last_search, str) and "youtube.com/results" in last_search:
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(last_search).query
            ).get("search_query", [""])[0]
        else:
            query = ""
        if not query:
            return "Search for YouTube videos first."
        result_index = int(ordinal_match.group(1)) - 1
        watch_url = _youtube_watch_url(query, result_index)
        if not watch_url:
            return f"I could not find video number {result_index + 1}."
        subprocess.Popen([_chrome_path(), watch_url])
        _remember_tab(watch_url)
        return f"Opened YouTube video number {result_index + 1}."

    if song.strip().lower() in {
        "__first_youtube_result__",
        "first video",
        "top video",
        "first result",
        "top result",
    }:
        try:
            state = _load_browser_state()
            last_search = state.get("last_youtube_search")
            if isinstance(last_search, str) and "youtube.com/results" in last_search:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(last_search).query
                ).get("search_query", [""])[0]
            else:
                query = ""

            if query:
                watch_url = _first_youtube_watch_url(query)
                if watch_url:
                    subprocess.Popen([_chrome_path(), watch_url])
                    _remember_tab(watch_url)
                    return "Opened and started the first YouTube result."

            page = _find_or_create_recent_page(
                "youtube.com",
                "https://www.youtube.com",
            )
            page.bring_to_front()
            if isinstance(last_search, str) and "youtube.com/results" in last_search:
                page.goto(last_search, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector(
                "ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title",
                timeout=15000,
            )
            title_locator = page.locator(
                "ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title"
            ).first
            title_locator.click()
            page.wait_for_selector("video", timeout=15000)
            page.evaluate("document.querySelector('video')?.play()")
            _remember_tab(page.url, page.title())
            return "Opened the first YouTube result."
        except Exception as error:
            return f"Could not open the first YouTube result: {error}"

    search_url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(song)
    )
    if OS == "Windows":
        if _port_open():
            try:
                page = _get_page()
                page.bring_to_front()
                page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_selector("ytd-video-renderer a#video-title", timeout=15000)
                page.locator("ytd-video-renderer a#video-title").first.click()
                page.wait_for_selector("video", timeout=15000)
                page.evaluate("document.querySelector('video')?.play()")
                _remember_tab(page.url, page.title())
                return f"Playing {song} on YouTube."
            except Exception:
                pass
        try:
            watch_url = _first_youtube_watch_url(song)
            if watch_url:
                subprocess.Popen([_chrome_path(), watch_url])
                _remember_tab(watch_url)
                return f"Opened and started the top YouTube result for {song}."
        except Exception:
            pass
        _remember_tab(search_url)
        return f"Opened YouTube and searched for {song}."

    page = _get_page()
    page.goto(
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(song)
    )
    page.wait_for_selector("ytd-video-renderer a#video-title", timeout=15000)
    page.locator("ytd-video-renderer a#video-title").first.click()
    for _ in range(8):
        time.sleep(1)
        skip = page.locator(".ytp-skip-ad-button, .ytp-ad-skip-button-modern")
        if skip.count() > 0:
            try:
                skip.first.click(timeout=1000)
                break
            except Exception:
                pass
    return f"Playing {song} on YouTube."


@_retry_if_closed
def youtube_control(action: str) -> str:
    keys = {
        "pause": "k",
        "play": "k",
        "mute": "m",
        "unmute": "m",
        "fullscreen": "f",
        "forward": "l",
        "back": "j",
        "next": "Shift+n",
        "scroll_down": "pagedown",
        "scroll_up": "pageup",
        "top": "home",
        "bottom": "end",
    }
    if action not in keys:
        return f"I don't know the action {action}."
    if OS == "Windows" and not _port_open():
        try:
            chrome_hwnd = None
            chrome_rect = None
            try:
                import win32gui

                def enum_chrome(hwnd, _):
                    nonlocal chrome_hwnd, chrome_rect
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd).lower()
                    if "chrome" in title or "youtube" in title:
                        chrome_hwnd = hwnd
                        chrome_rect = win32gui.GetWindowRect(hwnd)
                        return False
                    return True

                win32gui.EnumWindows(enum_chrome, None)
            except Exception:
                pass

            if chrome_hwnd:
                try:
                    import win32gui
                    win32gui.SetForegroundWindow(chrome_hwnd)
                    time.sleep(0.15)
                except Exception:
                    pass

            if action in {"pause", "play"}:
                pyautogui.press("k")
            elif action in {"mute", "unmute"}:
                pyautogui.press("m")
            elif action == "fullscreen":
                pyautogui.press("f")
            elif action == "forward":
                pyautogui.press("l")
            elif action == "back":
                pyautogui.press("j")
            elif action == "next":
                pyautogui.hotkey("shift", "n")
            elif action in {"scroll_down", "scroll_up"}:
                if chrome_rect:
                    left, top, right, bottom = chrome_rect
                    pyautogui.moveTo(
                        max(0, (left + right) // 2),
                        max(0, (top + bottom) // 2),
                    )
                pyautogui.scroll(-700 if action == "scroll_down" else 700)
            elif action == "top":
                pyautogui.press("home")
            elif action == "bottom":
                pyautogui.press("end")
            return f"Done: {action}."
        except Exception as error:
            return f"Could not control YouTube: {error}"
    try:
        page = _find_existing_page("youtube.com")
        if page is None:
            page = _get_page()
        if "youtube.com" in page.url:
            page.bring_to_front()
            if action == "pause" or action == "play":
                player = page.locator("video")
                if player.count() > 0:
                    if action == "pause":
                        page.evaluate("document.querySelector('video')?.pause()")
                    else:
                        page.evaluate("document.querySelector('video')?.play()")
                else:
                    page.keyboard.press(keys[action])
            else:
                page.keyboard.press(keys[action])
        else:
            page.goto("https://www.youtube.com")
            page.keyboard.press(keys[action])
    except Exception as error:
        return f"Could not control YouTube: {error}"
    return f"Done: {action}."


@_retry_if_closed
def web_search(query: str) -> str:
    search_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    if OS == "Windows":
        if _port_open():
            try:
                page = _get_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                _remember_tab(page.url, page.title())
                return f"Searched for {query}."
            except Exception:
                pass
        subprocess.Popen(
            [
                _chrome_path(),
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                f"--profile-directory={_last_used_chrome_profile()}",
                "--no-first-run",
                "--no-default-browser-check",
                search_url,
            ]
        )
        _remember_tab(search_url)
        return f"Searched for {query}."
    _get_page().goto(search_url, wait_until="domcontentloaded", timeout=15000)
    return f"Searched for {query}."


def open_app(name: str) -> str:
    global _recent_app
    try:
        normalized_name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if normalized_name in {"notepad", "text editor"}:
            if _focus_notepad():
                _recent_app = "notepad"
                return "Opened Notepad."
            result = subprocess.run(
                ["cmd", "/c", "start", "", "notepad.exe"],
                capture_output=True,
            )
            for _ in range(20):
                time.sleep(0.25)
                if _focus_notepad():
                    _recent_app = "notepad"
                    return "Opened Notepad."
            _recent_app = "notepad"
            return "Opened Notepad." if result.returncode == 0 else "Could not open Notepad."
        website_urls = {
            "whatsapp": "https://web.whatsapp.com",
            "whatsapp web": "https://web.whatsapp.com",
            "gmail": "https://mail.google.com",
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "you tube": "https://www.youtube.com",
            "facebook": "https://www.facebook.com",
            "instagram": "https://www.instagram.com",
            "twitter": "https://x.com",
            "x": "https://x.com",
        }
        if normalized_name in website_urls:
            url = website_urls[normalized_name]
            if OS == "Windows" and _port_open():
                try:
                    page = _find_existing_page(
                        "youtube.com" if normalized_name in {"youtube", "you tube"} else ""
                    )
                    if page is None:
                        page = _get_page()
                    page.bring_to_front()
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    _remember_tab(page.url, page.title())
                    return f"Opened {normalized_name.title()}."
                except Exception:
                    pass
            subprocess.Popen([
                _chrome_path(),
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                f"--profile-directory={_last_used_chrome_profile()}",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ])
            _remember_tab(url)
            return f"Opened {normalized_name.title()}."
        if normalized_name in {"youtube", "you tube"}:
            subprocess.Popen(
                [
                    _chrome_path(),
                    f"--remote-debugging-port={CDP_PORT}",
                    f"--user-data-dir={PROFILE_DIR}",
                    f"--profile-directory={_last_used_chrome_profile()}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "https://www.youtube.com",
                ]
            )
            return "Opened YouTube."
        if normalized_name in {"chrome", "google chrome"}:
            if _port_open():
                try:
                    page = _get_page()
                    page.bring_to_front()
                    _restore_recent_tabs()
                    return "Chrome is already open with your recent account."
                except Exception:
                    pass
            subprocess.Popen(
                [
                    _chrome_path(),
                    f"--remote-debugging-port={CDP_PORT}",
                    f"--user-data-dir={PROFILE_DIR}",
                    f"--profile-directory={_last_used_chrome_profile()}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            )
            for _ in range(40):
                if _port_open():
                    _restore_recent_tabs()
                    break
                time.sleep(0.5)
            return "Opened Chrome with your most recently used Google account."
        if OS == "Darwin":
            if subprocess.run(["open", "-a", name], capture_output=True).returncode == 0:
                return f"Opened {name}."
        elif OS == "Windows":
            if subprocess.run(
                ["cmd", "/c", "start", "", name], capture_output=True
            ).returncode == 0:
                return f"Opened {name}."
            pyautogui.press("win")
            time.sleep(0.6)
            pyautogui.write(name, interval=0.04)
            time.sleep(0.8)
            pyautogui.press("enter")
            return f"Opened {name}."
        else:
            executable = shutil.which(name.lower())
            if executable:
                subprocess.Popen([executable])
                return f"Opened {name}."
    except Exception as error:
        return f"Could not open {name}: {error}"
    return f"I couldn't find an app called {name}."


def get_time() -> str:
    return datetime.datetime.now().strftime("It's %I:%M %p on %A, %d %B.")


def confirm(question: str) -> bool:
    """Ask yes/no before an operation that can change the computer."""
    if _confirm_callback is not None:
        return bool(_confirm_callback(question))
    if _speak is None:
        raise RuntimeError("Tools are not configured with a speech callback.")
    _speak(question)
    try:
        if _voice_mode:
            if _transcribe is None or _record_until_silence is None:
                raise RuntimeError("Voice callbacks are not configured.")
            answer = _transcribe(
                _record_until_silence(max_seconds=6)
            ).lower()
            print(f"You: {answer}")
        else:
            answer = input("(yes/no) ").lower()
    except Exception:
        return False
    if re.search(r"\b(no|nope|don't|dont|stop|cancel|wait)\b", answer):
        return False
    return bool(
        re.search(
            r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|confirm)\b",
            answer,
        )
    )


_HOME = Path.home()
_FOLDERS = {
    name: (
        [_HOME / "OneDrive" / name, _HOME / name]
        if name == "Documents"
        else [_HOME / name, _HOME / "OneDrive" / name]
    )
    for name in (
        "Desktop",
        "Documents",
        "Downloads",
        "Pictures",
        "Music",
        "Videos",
    )
}


def _find_file(query: str, limit: int = 1) -> list[str]:
    hits = []
    for paths in _FOLDERS.values():
        for base in paths:
            if not base.exists():
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [
                    directory
                    for directory in dirs
                    if not directory.startswith(".")
                    and directory not in ("node_modules", "__pycache__")
                ]
                if len(Path(root).relative_to(base).parts) > 4:
                    dirs[:] = []
                    continue
                for filename in files:
                    if query.lower() in filename.lower():
                        hits.append(os.path.join(root, filename))
                        if len(hits) >= limit:
                            return hits
    return hits


def _open_path(path: str) -> None:
    if OS == "Windows":
        os.startfile(path)
    elif OS == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_file(name: str) -> str:
    global _active_folder
    clean_name = name.strip().strip("\"'")
    if os.path.isabs(clean_name):
        path = Path(clean_name)
        if path.is_file():
            _open_path(str(path))
            _active_folder = path.parent
            return f"Opened {path.name}."
        return f"I couldn't find the file at {clean_name}."
    hits = []
    if _active_folder is not None and _active_folder.exists():
        query = name.lower().replace("_", " ").replace("-", " ")
        for root, dirs, files in os.walk(_active_folder):
            dirs[:] = [directory for directory in dirs if not directory.startswith(".")]
            for filename in files:
                normalized = filename.lower().replace("_", " ").replace("-", " ")
                if query in normalized or filename.lower() in query:
                    hits.append(os.path.join(root, filename))
                    if len(hits) >= 1:
                        break
            if hits:
                break
    if not hits:
        hits = _find_file(name)
    if not hits:
        return f"I couldn't find a file named {name}."
    _open_path(hits[0])
    return f"Opened {os.path.basename(hits[0])}."


def open_folder(name: str) -> str:
    global _active_folder
    clean_name = name.strip().strip("\"'")
    if os.path.isabs(clean_name):
        path = Path(clean_name)
        if path.is_dir():
            _open_path(str(path))
            _active_folder = path
            return f"Opened {path.name or str(path)}."
        return f"I couldn't find the folder at {clean_name}."
    if _active_folder is not None and _active_folder.exists():
        direct = _active_folder / clean_name
        if direct.is_dir():
            _open_path(str(direct))
            _active_folder = direct
            return f"Opened {direct.name}."
        query = clean_name.lower().replace("_", " ").replace("-", " ")
        for entry in _active_folder.iterdir():
            normalized = entry.name.lower().replace("_", " ").replace("-", " ")
            if entry.is_dir() and (query in normalized or normalized in query):
                _open_path(str(entry))
                _active_folder = entry
                return f"Opened {entry.name}."
    key = clean_name.capitalize()
    for path in _FOLDERS.get(key, []):
        if path.exists():
            _open_path(str(path))
            _active_folder = path
            return f"Opened your {key} folder."
    if os.path.isabs(name):
        path = Path(name)
        if path.exists():
            _open_path(str(path))
            _active_folder = path if path.is_dir() else path.parent
            return f"Opened {path.name or str(path)}."
    query = name.strip().rstrip("\\/")
    candidates = [
        _HOME / query,
        _HOME / "OneDrive" / query,
    ]
    for path in candidates:
        if path.exists():
            _open_path(str(path))
            _active_folder = path
            return f"Opened {path.name or name}."
    return f"I don't know a folder called {name}."


def close_app(name: str) -> str:
    try:
        if OS == "Windows":
            normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            executable_names = {
                "chrome": "chrome.exe",
                "google chrome": "chrome.exe",
                "notepad": "notepad.exe",
                "calculator": "CalculatorApp.exe",
                "code": "Code.exe",
                "visual studio code": "Code.exe",
                "youtube": "chrome.exe",
            }
            executable = executable_names.get(normalized, name)
            if not executable.lower().endswith(".exe"):
                executable += ".exe"
            result = subprocess.run(["taskkill", "/IM", executable], capture_output=True)
        elif OS == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", f'quit app "{name}"'], capture_output=True
            )
        else:
            result = subprocess.run(["pkill", "-f", name], capture_output=True)
        return f"Closed {name}." if result.returncode == 0 else f"I couldn't close {name}."
    except Exception as error:
        return f"Could not close {name}: {error}"


def set_volume(action: str) -> str:
    key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute"}.get(action)
    if not key:
        return "I can do up, down, mute or unmute."
    for _ in range(1 if action == "mute" else 5):
        pyautogui.press(key)
    return f"Volume {action}."


def type_text(text: str) -> str:
    if _recent_app == "notepad":
        _focus_notepad()
    time.sleep(1)
    pyautogui.write(text, interval=0.02)
    return "Typed it."


def save_file(filename: str) -> str:
    clean_name = os.path.basename(filename.strip().strip("\"'")).strip()
    if not clean_name:
        return "I need a file name."
    if "." not in clean_name:
        clean_name += ".txt"
    documents = next(
        (str(path) for path in _FOLDERS["Documents"] if path.exists()),
        str(_HOME),
    )
    os.makedirs(documents, exist_ok=True)

    if OS == "Windows" and _recent_app == "notepad":
        _focus_notepad()
        pyautogui.hotkey("ctrl", "shift", "s")
        time.sleep(1)
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.2)
        pyautogui.write(documents, interval=0.02)
        pyautogui.press("enter")
        time.sleep(0.8)
        pyautogui.hotkey("alt", "n")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write(clean_name, interval=0.02)
        pyautogui.press("enter")
        time.sleep(0.8)
        return f"Saved the file as {clean_name}."
    return f"I couldn't save the file. Open Notepad first."


def clear_text() -> str:
    if _recent_app == "notepad":
        _focus_notepad()
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        return "Cleared the text in Notepad."
    return "Open Notepad first."


def scroll_page(action: str) -> str:
    action = action.lower().strip()
    if action in {"down", "up"}:
        if OS == "Windows":
            try:
                import win32gui

                scope = get_active_folder()
                folder_name = scope["name"]
                explorer_hwnd = None

                def find_explorer(hwnd, _):
                    nonlocal explorer_hwnd
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    title = win32gui.GetWindowText(hwnd)
                    if folder_name and folder_name.lower() in title.lower():
                        explorer_hwnd = hwnd

                win32gui.EnumWindows(find_explorer, None)
                if explorer_hwnd is not None:
                    rect = win32gui.GetWindowRect(explorer_hwnd)
                    try:
                        win32gui.SetForegroundWindow(explorer_hwnd)
                    except Exception:
                        pass
                    pyautogui.moveTo(
                        max(0, (rect[0] + rect[2]) // 2),
                        max(0, (rect[1] + rect[3]) // 2),
                    )
                    time.sleep(0.15)
                else:
                    foreground = win32gui.GetForegroundWindow()
                    if foreground:
                        rect = win32gui.GetWindowRect(foreground)
                        pyautogui.moveTo(
                            max(0, (rect[0] + rect[2]) // 2),
                            max(0, (rect[1] + rect[3]) // 2),
                        )
                        time.sleep(0.15)
            except Exception:
                pass

    if action == "down":
        for _ in range(2):
            pyautogui.scroll(-220)
            time.sleep(0.18)
    elif action == "up":
        for _ in range(2):
            pyautogui.scroll(220)
            time.sleep(0.18)
    elif action == "top":
        pyautogui.press("home")
    elif action == "bottom":
        pyautogui.press("end")
    else:
        return f"I don't know the scroll action {action}."
    return f"Scrolled {action}."


def get_active_folder() -> dict[str, str | None]:
    if _active_folder is not None and _active_folder.exists():
        return {"name": _active_folder.name, "path": str(_active_folder)}
    return {"name": None, "path": None}


def explorer_back() -> str:
    global _active_folder
    if OS == "Windows":
        try:
            import win32gui

            scope = get_active_folder()
            folder_name = scope["name"]
            explorer_hwnd = None

            def find_explorer(hwnd, _):
                nonlocal explorer_hwnd
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                if folder_name and folder_name.lower() in title.lower():
                    explorer_hwnd = hwnd

            win32gui.EnumWindows(find_explorer, None)
            if explorer_hwnd is not None:
                win32gui.ShowWindow(explorer_hwnd, 9)
                try:
                    win32gui.SetForegroundWindow(explorer_hwnd)
                except Exception:
                    pass
                time.sleep(0.2)
            pyautogui.hotkey("alt", "left")
            time.sleep(0.3)

            if _active_folder is not None:
                parent = _active_folder.parent
                if parent.exists():
                    _active_folder = parent
            return f"Went back from {_active_folder.name if _active_folder else 'the folder'}."
        except Exception as error:
            return f"Could not go back: {error}"
    pyautogui.hotkey("alt", "left")
    return "Went back."


def explorer_forward() -> str:
    if OS == "Windows":
        try:
            import win32gui

            explorer_hwnds: list[int] = []

            def find_explorer(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if "explorer" in title.lower() or "\\" in title:
                        explorer_hwnds.append(hwnd)

            win32gui.EnumWindows(find_explorer, None)
            if explorer_hwnds:
                hwnd = explorer_hwnds[-1]
                win32gui.ShowWindow(hwnd, 9)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                time.sleep(0.2)
            pyautogui.hotkey("alt", "right")
            return "Went forward."
        except Exception as error:
            return f"Could not go forward: {error}"
    pyautogui.hotkey("alt", "right")
    return "Went forward."


def search_in_explorer(query: str) -> str:
    global _last_explorer_query
    clean_query = re.sub(
        r"^(?:the\s+)?(?:file|folder)\s+",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()
    if not clean_query:
        return "Tell me what to search for."
    _last_explorer_query = clean_query
    if OS != "Windows":
        return "Explorer search is only available on Windows."
    try:
        import win32gui

        scope = get_active_folder()
        target_name = scope["name"]
        explorer_hwnd = None

        def find_explorer(hwnd, _):
            nonlocal explorer_hwnd
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if "explorer.exe" in (win32gui.GetWindowModuleFileName(hwnd) if hasattr(win32gui, "GetWindowModuleFileName") else "").lower():
                if target_name and target_name.lower() in title.lower():
                    explorer_hwnd = hwnd
            elif target_name and target_name.lower() in title.lower():
                explorer_hwnd = hwnd

        win32gui.EnumWindows(find_explorer, None)
        if explorer_hwnd is not None:
            win32gui.ShowWindow(explorer_hwnd, 9)
            try:
                win32gui.SetForegroundWindow(explorer_hwnd)
            except Exception:
                pass
            time.sleep(0.25)

        pyautogui.hotkey("ctrl", "e")
        time.sleep(0.25)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write(clean_query, interval=0.03)
        pyautogui.press("enter")
        return f"Searching in {target_name or 'the open folder'} for {clean_query}."
    except Exception as error:
        return f"Could not search in Explorer: {error}"


def open_visible_search_result(position: str) -> str:
    clean_position = position.lower().strip()
    words = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    }
    if clean_position.isdigit():
        index = int(clean_position)
    else:
        index = words.get(clean_position, 0)
    if index < 1 or index > 10:
        return "Choose an item from first through tenth."
    if _last_explorer_query:
        scope = get_active_folder()
        search_root = (
            Path(scope["path"])
            if scope["path"] and Path(scope["path"]).exists()
            else next(
                (path for path in _FOLDERS["Documents"] if path.exists()),
                Path.home(),
            )
        )
        terms = [
            term
            for term in re.split(r"[\s_-]+", _last_explorer_query.lower())
            if term
        ]
        matches: list[Path] = []
        try:
            for root, dirs, files in os.walk(search_root):
                dirs[:] = [
                    directory for directory in dirs
                    if not directory.startswith(".")
                    and directory not in {"node_modules", "__pycache__", ".git", "venv"}
                ]
                for name in dirs + files:
                    path = Path(root) / name
                    normalized = re.sub(r"[\s_-]+", " ", path.name.lower())
                    if all(term in normalized for term in terms):
                        matches.append(path)
            matches.sort(
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception as error:
            return f"Could not resolve the search result: {error}"
        matches = list(dict.fromkeys(matches))[:10]
        if index <= len(matches):
            target = matches[index - 1]
            if target.is_dir():
                return open_folder(str(target))
            _open_path(str(target))
            return f"Opened {target.name}."
    if OS != "Windows":
        return "Visible Explorer results are only available on Windows."
    try:
        import win32gui

        scope = get_active_folder()
        target_name = scope["name"]
        explorer_hwnd = None

        def find_explorer(hwnd, _):
            nonlocal explorer_hwnd
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if target_name and target_name.lower() in title.lower():
                explorer_hwnd = hwnd

        win32gui.EnumWindows(find_explorer, None)
        if explorer_hwnd is not None:
            win32gui.ShowWindow(explorer_hwnd, 9)
            try:
                win32gui.SetForegroundWindow(explorer_hwnd)
            except Exception:
                pass
            time.sleep(0.25)
        else:
            pyautogui.hotkey("alt", "tab")
            time.sleep(0.2)

        pyautogui.press("escape")
        time.sleep(0.15)
        pyautogui.press("f6")
        time.sleep(0.15)
        pyautogui.press("f6")
        time.sleep(0.15)
        pyautogui.press("tab")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "home")
        time.sleep(0.3)
        for _ in range(index - 1):
            pyautogui.press("down")
            time.sleep(0.08)
        pyautogui.press("enter")
        return f"Opened the {clean_position} visible search result."
    except Exception as error:
        return f"Could not open the visible search result: {error}"


def search_files(query: str) -> str:
    global _search_results, _last_search_query
    clean_query = re.sub(r"^(?:the\s+)?(?:file|folder)\s+", "", query, flags=re.IGNORECASE).strip()
    if not clean_query:
        return "Tell me the file or folder name to search."
    search_root = _active_folder
    if search_root is None or not search_root.exists():
        search_root = next(
            (path for path in _FOLDERS["Documents"] if path.exists()),
            None,
        )
    if search_root is None:
        return "I couldn't find the Documents folder."
    terms = [term for term in re.split(r"[\s_-]+", clean_query.lower()) if term]
    matches: list[Path] = []
    try:
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [
                directory for directory in dirs
                if not directory.startswith(".")
                and directory not in {"node_modules", "__pycache__", ".git", "venv"}
            ]
            for name in dirs + files:
                path = Path(root) / name
                normalized = re.sub(r"[\s_-]+", " ", path.name.lower())
                if all(term in normalized for term in terms) and path not in matches:
                    matches.append(path)
            if len(matches) >= 10:
                break
    except Exception as error:
        return f"Could not search: {error}"
    unique_matches: dict[str, Path] = {}
    for path in matches:
        unique_matches.setdefault(path.name.lower(), path)
    _search_results = sorted(unique_matches.values(), key=lambda path: path.name.lower())[:10]
    _last_search_query = clean_query
    try:
        _search_results_file.write_text(
            json.dumps([str(path) for path in _search_results]),
            encoding="utf-8",
        )
    except Exception:
        pass
    if not _search_results:
        return f"I found no files or folders matching {clean_query}."
    lines = []
    for index, item in enumerate(_search_results, start=1):
        kind = "folder" if item.is_dir() else "file"
        lines.append(f"{index}. {item.name} ({kind})")
    return (
        f"Found {len(_search_results)} result(s) for \"{clean_query}\":\n"
        + "\n".join(lines)
        + "\n\nSay \"Open first\", \"Open second\", or the result number to open an item."
    )


def open_search_result(position: str) -> str:
    global _active_folder, _search_results
    if not _search_results and _search_results_file.exists():
        try:
            saved_paths = json.loads(_search_results_file.read_text(encoding="utf-8"))
            _search_results = [Path(saved) for saved in saved_paths if Path(saved).exists()]
        except Exception:
            _search_results = []
    if not _search_results and _last_search_query:
        search_files(_last_search_query)
    words = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    }
    index = words.get(position.lower().strip(), int(position) if position.isdigit() else 0)
    if index < 1 or index > len(_search_results):
        return "Search for a name first, or choose a result from one to ten."
    target = _search_results[index - 1]
    _open_path(str(target))
    if target.is_dir():
        _active_folder = target
    _search_results = []
    try:
        _search_results_file.unlink(missing_ok=True)
    except Exception:
        pass
    return f"Opened {target.name}."


def open_item(position: str, kind: str = "any") -> str:
    if _search_results:
        return open_search_result(position)
    if _active_folder is None or not _active_folder.exists():
        return "Open a folder first."
    words = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
    }
    index = words.get(position.lower().strip(), 0)
    if position.isdigit():
        index = int(position)
    if index < 1:
        return f"I don't know item number {position}."
    try:
        entries = sorted(
            (entry for entry in _active_folder.iterdir() if not entry.name.startswith(".")),
            key=lambda entry: entry.name.lower(),
        )
        if kind in {"file", "files"}:
            entries = [entry for entry in entries if entry.is_file()]
        elif kind in {"folder", "folders", "directory", "directories"}:
            entries = [entry for entry in entries if entry.is_dir()]
        if index > len(entries):
            return f"There is no {kind} number {index} in {_active_folder.name}."
        target = entries[index - 1]
        _open_path(str(target))
        return f"Opened {target.name}."
    except Exception as error:
        return f"Could not open item number {index}: {error}"


def press_keys(keys: str) -> str:
    parts = [key.strip().lower() for key in keys.split("+") if key.strip()]
    pyautogui.hotkey(*parts)
    return f"Pressed {keys}."


def take_screenshot() -> str:
    folder = next((path for path in _FOLDERS["Pictures"] if path.exists()), _HOME)
    path = folder / f"screenshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    pyautogui.screenshot(str(path))
    return f"Screenshot saved in {folder.name}."


def lock_screen() -> str:
    if OS == "Windows":
        import ctypes

        ctypes.windll.user32.LockWorkStation()
    elif OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"])
    else:
        subprocess.run(["loginctl", "lock-session"])
    return "Locked."


def power(action: str) -> str:
    if action not in ("sleep", "restart", "shutdown"):
        return "I can sleep, restart or shut down."
    if not confirm(f"Do you really want me to {action} the computer?"):
        return "Okay, cancelled."
    if OS == "Windows":
        command = {
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "restart": ["shutdown", "/r", "/t", "5"],
            "shutdown": ["shutdown", "/s", "/t", "5"],
        }[action]
    elif OS == "Darwin":
        command = {
            "sleep": ["pmset", "sleepnow"],
            "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
            "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
        }[action]
    else:
        command = ["systemctl", {"sleep": "suspend", "restart": "reboot", "shutdown": "poweroff"}[action]]
    subprocess.Popen(command)
    return f"Doing {action}."


def run_command(command: str) -> str:
    if not confirm(f"I want to run this command: {command}. Should I go ahead?"):
        return "Okay, I didn't run it."
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = (result.stdout or result.stderr).strip().replace("\n", " ")
        return output[:200] or "Command finished."
    except Exception as error:
        return f"Command failed: {error}"


def type_password(label: str) -> str:
    try:
        import keyring

        secret = keyring.get_password(KEYRING_SERVICE, label)
    except Exception as error:
        return f"Keyring problem: {error}"
    if not secret:
        return f"I have no saved password called {label}."
    if not confirm(f"Click the password box now. Should I type the {label} password?"):
        return "Okay, I didn't type it."
    time.sleep(3)
    pyautogui.write(secret, interval=0.03)
    return f"Typed the {label} password."


@_retry_if_closed
def whatsapp_search_contact(contact: str) -> str:
    """Open WhatsApp Web and search for a chat by contact name."""
    if not _port_open():
        subprocess.Popen([
            _chrome_path(),
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            f"--profile-directory={_last_used_chrome_profile()}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://web.whatsapp.com",
        ])
        return f"Opened WhatsApp Web. Searching for {contact} after it finishes loading."

    page = _get_page()
    page.bring_to_front()
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=15000)
    try:
        search = page.locator(
            '[data-testid="chat-list-search"], '
            '[title="Search input textbox"], '
            'div[contenteditable="true"][data-tab="3"]'
        ).first
        search.wait_for(state="visible", timeout=30000)
        search.click()
        search.fill(contact)
        time.sleep(1)
        result = page.get_by_text(contact, exact=True).first
        result.wait_for(state="visible", timeout=10000)
        result.click()
        return f"Opened the WhatsApp chat with {contact}."
    except Exception as error:
        return f"I opened WhatsApp Web, but could not find {contact}: {error}"


def whatsapp_send_message(contact: str, message: str) -> str:
    search_result = whatsapp_search_contact(contact)
    if "could not find" in search_result.lower():
        return search_result
    if not confirm(
        f"Send this WhatsApp message to {contact}: {message}"
    ):
        return f"Cancelled the WhatsApp message to {contact}."
    try:
        page = _get_page()
        compose = page.locator(
            '[data-testid="conversation-compose-box-input"], '
            'footer div[contenteditable="true"][data-tab="10"]'
        ).first
        compose.wait_for(state="visible", timeout=15000)
        compose.fill(message)
        send = page.locator(
            '[data-testid="send"], '
            'button[aria-label="Send"]'
        ).first
        if send.count() > 0:
            send.click()
        else:
            compose.press("Enter")
        return f"Sent the WhatsApp message to {contact}."
    except Exception as error:
        return f"Could not send the WhatsApp message: {error}"


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOL_FUNCS: dict[str, ToolFunction] = {
    "play_youtube": play_youtube,
    "youtube_control": youtube_control,
    "web_search": web_search,
    "open_app": open_app,
    "get_time": get_time,
    "open_file": open_file,
    "open_folder": open_folder,
    "close_app": close_app,
    "set_volume": set_volume,
    "type_text": type_text,
    "save_file": save_file,
    "clear_text": clear_text,
    "scroll_page": scroll_page,
    "explorer_back": explorer_back,
    "explorer_forward": explorer_forward,
    "search_in_explorer": search_in_explorer,
    "open_visible_search_result": open_visible_search_result,
    "open_item": open_item,
    "search_files": search_files,
    "open_search_result": open_search_result,
    "press_keys": press_keys,
    "take_screenshot": take_screenshot,
    "lock_screen": lock_screen,
    "power": power,
    "run_command": run_command,
    "type_password": type_password,
    "whatsapp_search_contact": whatsapp_search_contact,
    "whatsapp_send_message": whatsapp_send_message,
}

TOOLS = [
    _tool("play_youtube", "Search YouTube and play a song or video.", {"song": {"type": "string"}}, ["song"]),
    _tool("youtube_control", "Control the playing YouTube video or scroll YouTube.", {"action": {"type": "string", "enum": ["pause", "play", "mute", "unmute", "fullscreen", "forward", "back", "next", "scroll_down", "scroll_up", "top", "bottom"]}}, ["action"]),
    _tool("web_search", "Search Google in the browser.", {"query": {"type": "string"}}, ["query"]),
    _tool("open_app", "Open an application on the laptop.", {"name": {"type": "string"}}, ["name"]),
    _tool("get_time", "Get the current time and date."),
    _tool("open_file", "Find and open a file by (part of) its name.", {"name": {"type": "string"}}, ["name"]),
    _tool("open_folder", "Open a folder on the laptop.", {"name": {"type": "string"}}, ["name"]),
    _tool("close_app", "Close a running application.", {"name": {"type": "string"}}, ["name"]),
    _tool("set_volume", "Change the system volume.", {"action": {"type": "string", "enum": ["up", "down", "mute", "unmute"]}}, ["action"]),
    _tool("type_text", "Type text into the focused window.", {"text": {"type": "string"}}, ["text"]),
    _tool("save_file", "Save the currently open Notepad document with a file name.", {"filename": {"type": "string"}}, ["filename"]),
    _tool("clear_text", "Clear all text in the currently open Notepad document."),
    _tool("scroll_page", "Scroll the active window.", {"action": {"type": "string", "enum": ["up", "down", "top", "bottom"]}}, ["action"]),
    _tool("explorer_back", "Go back in the active Windows folder/Explorer window."),
    _tool("explorer_forward", "Go forward in the active Windows folder/Explorer window."),
    _tool("search_in_explorer", "Search in the currently open Windows Explorer folder.", {"query": {"type": "string"}}, ["query"]),
    _tool("open_visible_search_result", "Open the numbered item in the visible Windows Explorer search results.", {"position": {"type": "string"}}, ["position"]),
    _tool("open_item", "Open a numbered file or folder from the top of the most recently opened folder.", {"position": {"type": "string"}, "kind": {"type": "string", "enum": ["any", "file", "folder"]}}, ["position"]),
    _tool("search_files", "Search for files or folders by name.", {"query": {"type": "string"}}, ["query"]),
    _tool("open_search_result", "Open a numbered result from the latest file or folder search.", {"position": {"type": "string"}}, ["position"]),
    _tool("press_keys", "Press a keyboard shortcut.", {"keys": {"type": "string"}}, ["keys"]),
    _tool("take_screenshot", "Take a screenshot of the screen."),
    _tool("lock_screen", "Lock the computer screen."),
    _tool("power", "Sleep, restart or shut down the computer.", {"action": {"type": "string", "enum": ["sleep", "restart", "shutdown"]}}, ["action"]),
    _tool("run_command", "Run a terminal command.", {"command": {"type": "string"}}, ["command"]),
    _tool("type_password", "Type a saved password using its label.", {"label": {"type": "string"}}, ["label"]),
    _tool("whatsapp_search_contact", "Open WhatsApp Web and search for a contact or chat.", {"contact": {"type": "string"}}, ["contact"]),
    _tool("whatsapp_send_message", "Search for a WhatsApp contact and send a message after confirmation.", {"contact": {"type": "string"}, "message": {"type": "string"}}, ["contact", "message"]),
]


def run_tool(name: str, args: dict[str, Any] | None) -> str:
    function = TOOL_FUNCS.get(name)
    if not function:
        return f"Unknown tool {name}."
    try:
        return function(**(args or {}))
    except Exception as error:
        return f"{name} failed: {error}"
