import sys
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import quote

import tools


class YouTubeNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = Mock()
        self.page.url = "https://www.youtube.com/results"
        self.page.title.return_value = "YouTube"
        self.page.locator.return_value.first = Mock()

    def test_search_reuses_youtube_tab_and_commits_navigation(self) -> None:
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=True),
            patch.object(tools, "_find_existing_page", return_value=self.page),
            patch.object(tools, "_remember_tab"),
            patch.object(tools.subprocess, "Popen") as popen,
        ):
            result = tools.search_youtube("nattu nattu song")

        self.assertIn("Searched YouTube", result)
        self.page.goto.assert_called_once_with(
            "https://www.youtube.com/results?search_query="
            + quote("nattu nattu song"),
            wait_until="commit",
            timeout=5000,
        )
        popen.assert_not_called()

    def test_search_waits_for_starting_chrome_before_opening_another_tab(self) -> None:
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", side_effect=[False, True]) as port_open,
            patch.object(tools, "_navigate_existing_youtube_window", return_value=False),
            patch.object(tools, "_find_existing_page", return_value=self.page),
            patch.object(tools, "_remember_tab"),
            patch.object(tools.time, "sleep"),
            patch.object(tools.subprocess, "Popen") as popen,
        ):
            result = tools.search_youtube("nattu nattu song")

        self.assertIn("Searched YouTube", result)
        self.assertEqual(port_open.call_count, 2)
        popen.assert_not_called()

    def test_search_without_connection_starts_the_controlled_profile(self) -> None:
        search_url = (
            "https://www.youtube.com/results?search_query="
            + quote("nattu nattu song")
        )
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=False),
            patch.object(tools, "_navigate_existing_youtube_window", return_value=False),
            patch.object(tools, "_wait_for_browser_port", return_value=False),
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab"),
        ):
            result = tools.search_youtube("nattu nattu song")

        self.assertIn("Searched YouTube", result)
        launch.assert_called_once_with(search_url)

    def test_search_reuses_existing_youtube_window_without_debugging_port(self) -> None:
        search_url = (
            "https://www.youtube.com/results?search_query="
            + quote("nattu nattu song")
        )
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=False),
            patch.object(tools, "_navigate_existing_youtube_window", return_value=True) as navigate,
            patch.object(tools, "_wait_for_browser_port") as wait_for_port,
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab"),
        ):
            result = tools.search_youtube("nattu nattu song")

        self.assertIn("Searched YouTube", result)
        navigate.assert_called_once_with(search_url)
        wait_for_port.assert_not_called()
        launch.assert_not_called()

    def test_open_then_search_does_not_launch_a_second_chrome_window(self) -> None:
        search_url = (
            "https://www.youtube.com/results?search_query="
            + quote("nattu nattu song")
        )
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=False),
            patch.object(tools, "_navigate_existing_youtube_window", side_effect=[False, True]) as navigate,
            patch.object(tools, "_wait_for_browser_port", return_value=False) as wait_for_port,
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab"),
        ):
            tools.open_app("youtube")
            result = tools.search_youtube("nattu nattu song")

        self.assertIn("Searched YouTube", result)
        launch.assert_called_once_with("https://www.youtube.com")
        navigate.assert_has_calls([
            call("https://www.youtube.com"),
            call(search_url),
        ])
        wait_for_port.assert_not_called()

    def test_open_youtube_reuses_existing_window_without_debugging_port(self) -> None:
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=False),
            patch.object(tools, "_navigate_existing_youtube_window", return_value=True) as navigate,
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab"),
        ):
            result = tools.open_app("youtube")

        self.assertIn("Opened Youtube", result)
        navigate.assert_called_once_with("https://www.youtube.com")
        launch.assert_not_called()

    def test_existing_youtube_window_is_searched_via_its_address_bar(self) -> None:
        search_url = "https://www.youtube.com/results?search_query=nattu+nattu+song"
        win32gui = Mock()
        win32gui.GetForegroundWindow.return_value = 11
        win32gui.IsWindowVisible.return_value = True
        win32gui.GetWindowText.side_effect = {
            11: "YouTube - Google Chrome",
            22: "Kira - Google Chrome",
        }.get
        win32gui.SetForegroundWindow.return_value = True

        def enumerate_windows(callback, extra):
            callback(11, extra)
            callback(22, extra)

        win32gui.EnumWindows.side_effect = enumerate_windows
        with (
            patch.object(tools, "OS", "Windows"),
            patch.dict(sys.modules, {"win32gui": win32gui}),
            patch.object(tools.pyautogui, "hotkey") as hotkey,
            patch.object(tools.pyautogui, "write") as write,
            patch.object(tools.pyautogui, "press") as press,
            patch.object(tools.time, "sleep"),
        ):
            result = tools._navigate_existing_youtube_window(search_url)

        self.assertTrue(result)
        win32gui.ShowWindow.assert_called_once_with(11, 9)
        win32gui.SetForegroundWindow.assert_called_once_with(11)
        hotkey.assert_called_once_with("ctrl", "l")
        write.assert_called_once_with(search_url, interval=0.001)
        press.assert_called_once_with("enter")

    def test_search_selects_a_background_youtube_tab_before_navigating(self) -> None:
        search_url = "https://www.youtube.com/results?search_query=nattu+nattu+song"
        titles = {11: "Kira - Google Chrome"}
        win32gui = Mock()
        win32gui.GetForegroundWindow.return_value = 11
        win32gui.IsWindowVisible.return_value = True
        win32gui.GetWindowText.side_effect = lambda hwnd: titles.get(hwnd, "")
        win32gui.SetForegroundWindow.return_value = True

        def enumerate_windows(callback, extra):
            callback(11, extra)

        win32gui.EnumWindows.side_effect = enumerate_windows
        tab_search_open = True

        def press(key):
            nonlocal tab_search_open
            if key == "enter" and tab_search_open:
                titles[11] = "YouTube - Google Chrome"
                tab_search_open = False

        with (
            patch.object(tools, "OS", "Windows"),
            patch.dict(sys.modules, {"win32gui": win32gui}),
            patch.object(tools.pyautogui, "hotkey") as hotkey,
            patch.object(tools.pyautogui, "write") as write,
            patch.object(tools.pyautogui, "press", side_effect=press) as press_mock,
            patch.object(tools.time, "sleep"),
        ):
            result = tools._navigate_existing_youtube_window(search_url)

        self.assertTrue(result)
        hotkey.assert_has_calls([
            call("ctrl", "shift", "a"),
            call("ctrl", "l"),
        ])
        write.assert_has_calls([
            call("YouTube", interval=0.001),
            call(search_url, interval=0.001),
        ])
        press_mock.assert_has_calls([call("down"), call("enter"), call("enter")])

    def test_search_does_not_navigate_when_chrome_has_no_youtube_tab(self) -> None:
        search_url = "https://www.youtube.com/results?search_query=nattu+nattu+song"
        win32gui = Mock()
        win32gui.GetForegroundWindow.return_value = 11
        win32gui.IsWindowVisible.return_value = True
        win32gui.GetWindowText.return_value = "Kira - Google Chrome"
        win32gui.SetForegroundWindow.return_value = True

        def enumerate_windows(callback, extra):
            callback(11, extra)

        win32gui.EnumWindows.side_effect = enumerate_windows
        with (
            patch.object(tools, "OS", "Windows"),
            patch.dict(sys.modules, {"win32gui": win32gui}),
            patch.object(tools.pyautogui, "hotkey") as hotkey,
            patch.object(tools.pyautogui, "write") as write,
            patch.object(tools.pyautogui, "press") as press,
            patch.object(tools.time, "sleep"),
        ):
            result = tools._navigate_existing_youtube_window(search_url)

        self.assertFalse(result)
        hotkey.assert_called_once_with("ctrl", "shift", "a")
        write.assert_called_once_with("YouTube", interval=0.001)
        press.assert_has_calls([call("down"), call("enter"), call("esc")])

    def test_open_youtube_uses_fast_navigation_on_existing_tab(self) -> None:
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=True),
            patch.object(tools, "_find_existing_page", return_value=self.page),
            patch.object(tools, "_remember_tab"),
            patch.object(tools, "_launch_controlled_chrome") as launch,
        ):
            result = tools.open_app("youtube")

        self.assertIn("Opened Youtube", result)
        self.page.goto.assert_called_once_with(
            "https://www.youtube.com",
            wait_until="commit",
            timeout=5000,
        )
        launch.assert_not_called()

    def test_play_reuses_youtube_tab_without_slow_http_fallback(self) -> None:
        result_selector = (
            "ytd-video-renderer a#video-title, "
            "ytd-rich-item-renderer a#video-title"
        )
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_port_open", return_value=True),
            patch.object(tools, "_find_existing_page", return_value=self.page),
            patch.object(tools, "_get_page") as get_page,
            patch.object(tools, "_remember_tab"),
            patch.object(tools, "_first_youtube_watch_url") as scrape_search,
            patch.object(tools.subprocess, "Popen") as popen,
        ):
            result = tools.play_youtube("nattu nattu song")

        self.assertEqual(result, "Playing nattu nattu song on YouTube.")
        self.page.goto.assert_called_once_with(
            "https://www.youtube.com/results?search_query="
            + quote("nattu nattu song"),
            wait_until="commit",
            timeout=5000,
        )
        self.page.wait_for_selector.assert_has_calls([
            call(result_selector, timeout=5000),
            call("video", timeout=3000),
        ])
        self.page.locator.return_value.first.click.assert_called_once_with(timeout=3000)
        self.page.evaluate.assert_called_once()
        get_page.assert_not_called()
        scrape_search.assert_not_called()
        popen.assert_not_called()

    def test_play_uses_first_result_in_existing_youtube_tab_without_cdp(self) -> None:
        watch_url = "https://www.youtube.com/watch?v=abcdefghijk"
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_wait_for_browser_port", return_value=False),
            patch.object(tools, "_first_youtube_watch_url", return_value=watch_url) as first_result,
            patch.object(tools, "_navigate_existing_youtube_window", return_value=True) as navigate,
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab") as remember,
        ):
            result = tools.play_youtube("nattu nattu song")

        self.assertIn("first YouTube result", result)
        first_result.assert_called_once_with("nattu nattu song")
        navigate.assert_called_once_with(watch_url + "&autoplay=1")
        remember.assert_called_once_with(watch_url + "&autoplay=1")
        launch.assert_not_called()

    def test_play_opens_first_result_when_no_youtube_tab_exists(self) -> None:
        watch_url = "https://www.youtube.com/watch?v=abcdefghijk"
        with (
            patch.object(tools, "OS", "Windows"),
            patch.object(tools, "_wait_for_browser_port", return_value=False),
            patch.object(tools, "_first_youtube_watch_url", return_value=watch_url),
            patch.object(tools, "_navigate_existing_youtube_window", return_value=False),
            patch.object(tools, "_launch_controlled_chrome") as launch,
            patch.object(tools, "_remember_tab") as remember,
        ):
            result = tools.play_youtube("nattu nattu song")

        self.assertIn("first YouTube result", result)
        launch.assert_called_once_with(watch_url + "&autoplay=1")
        remember.assert_called_once_with(watch_url + "&autoplay=1")


if __name__ == "__main__":
    unittest.main()
