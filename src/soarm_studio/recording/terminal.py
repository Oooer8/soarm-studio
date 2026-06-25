"""Terminal interaction utilities for interactive recording sessions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import select
import sys
import termios
import threading
import tty

from .session import (
    EpisodeDecision,
    EpisodeResultInfo,
    EpisodeStartInfo,
    RecordingControls,
    RecordingLoopControl,
)

KEY_LEFT = "left"
KEY_RIGHT = "right"


@contextmanager
def _raw_mode() -> Iterator[None]:
    """Temporarily set stdin to raw (cbreak) mode, restoring on exit."""
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_raw_char(timeout: float = 0.0) -> str | None:
    if not sys.stdin.isatty():
        return None
    readable, _, _ = select.select([sys.stdin], [], [], timeout)
    if readable:
        return sys.stdin.read(1)
    return None


def _read_key_nonblocking(timeout: float = 0.0) -> str | None:
    """Read a key token from stdin without blocking."""
    key = _read_raw_char(timeout)
    if key != "\x1b":
        return key
    second = _read_raw_char(0.05)
    if second != "[":
        return key
    third = _read_raw_char(0.05)
    if third == "D":
        return KEY_LEFT
    if third == "C":
        return KEY_RIGHT
    return key


def countdown(seconds: int, message: str = "") -> None:
    """Display a countdown timer, skippable by pressing Enter.

    Parameters
    ----------
    seconds:
        Number of seconds to count down.
    message:
        Optional message to display before the countdown.
    """
    if message:
        print(message)
    if seconds <= 0:
        return

    with _raw_mode():
        for remaining in range(seconds, 0, -1):
            sys.stdout.write(f"\r  倒计时: {remaining}... (按 Enter 跳过)")
            sys.stdout.flush()
            # Check for Enter key every 100ms during each second
            for _ in range(10):
                key = _read_key_nonblocking(timeout=0.1)
                if key in ("\r", "\n"):
                    sys.stdout.write("\r" + " " * 50 + "\r")
                    sys.stdout.flush()
                    return
        sys.stdout.write("\r" + " " * 50 + "\r")
        sys.stdout.flush()


def wait_for_key(
    prompt: str,
    valid_keys: str | tuple[str, ...] | set[str],
    *,
    default: str | None = None,
) -> str:
    """Wait for the user to press one of the valid keys.

    Parameters
    ----------
    prompt:
        The prompt to display.
    valid_keys:
        A string of valid key characters (e.g. ``"sdq"``).
    default:
        If provided, pressing Enter returns this key.

    Returns
    -------
    The key that was pressed (lowercase).
    """
    print(prompt, end="", flush=True)
    valid = set(valid_keys) if not isinstance(valid_keys, str) else set(valid_keys)
    if not sys.stdin.isatty():
        if default is None:
            raise RuntimeError("interactive prompt requires a TTY")
        print()
        return default
    with _raw_mode():
        while True:
            key = _read_key_nonblocking(timeout=0.2)
            if key is None:
                continue
            if key in ("\r", "\n") and default is not None:
                print()
                return default
            key_lower = key.lower()
            if key_lower in valid:
                print()
                return key_lower


class KeyboardListener:
    """Background thread that listens for keyboard input during recording.

    The listener polls stdin for key presses. When a configured key is detected,
    ``stop_requested`` is set to ``True`` and the matching callback is called.

    Usage::

        listener = KeyboardListener(key_actions={"left": on_left})
        listener.start()
        # ... do work, periodically check listener.stop_requested ...
        listener.stop()
    """

    def __init__(
        self,
        stop_key: str | None = "q",
        on_stop: Callable[[], None] | None = None,
        key_actions: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        self.stop_key = None if stop_key is None else stop_key.lower()
        self.on_stop = on_stop
        self.key_actions = {
            key.lower(): action
            for key, action in (key_actions or {}).items()
        }
        self.stop_requested = False
        self.last_key: str | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the background key listener thread."""
        if self._thread is not None:
            return
        self._running = True
        self.stop_requested = False
        self.last_key = None
        self._thread = threading.Thread(target=self._listen, daemon=True, name="key-listener")
        self._thread.start()

    def stop(self) -> None:
        """Stop the background key listener thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _listen(self) -> None:
        """Poll stdin for configured keys."""
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                key = _read_key_nonblocking(timeout=0.1)
                if key is None:
                    continue
                key_lower = key.lower()
                if self.stop_key is not None and key_lower == self.stop_key:
                    self._trigger(key_lower)
                    return
                action = self.key_actions.get(key_lower)
                if action is not None:
                    self._trigger(key_lower, action=action)
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _trigger(self, key: str, *, action: Callable[[], None] | None = None) -> None:
        self.stop_requested = True
        self.last_key = key
        if action is not None:
            action()
        elif self.on_stop is not None:
            self.on_stop()


def create_manual_recording_controls() -> RecordingControls:
    """Create CLI controls for manually accepting or retrying recorded episodes."""
    if not sys.stdin.isatty():
        raise RuntimeError("--save-policy manual requires an interactive terminal")

    def before_episode(info: EpisodeStartInfo) -> bool:
        attempt = int(info.attempt)
        episode_label = f"Episode {info.episode_number}/{info.total_episodes}"
        if attempt > 1:
            episode_label = f"{episode_label} (第 {attempt} 次尝试)"
        print(f"\n{episode_label}")
        print(f"任务: {info.task}")
        if info.warmup > 0:
            print(f"会先 warmup {info.warmup:g}s，然后录制最多 {info.seconds:g}s。")
        else:
            print(f"录制最多 {info.seconds:g}s。")
        key = wait_for_key("摆好起始姿态后按 Enter 开始，按 q 放弃录制会话: ", "q", default="s")
        if key == "q":
            return False
        countdown(3, "准备开始录制。")
        return True

    def after_episode(info: EpisodeResultInfo) -> EpisodeDecision:
        frames = int(info.quality.get("frames", 0))
        elapsed_s = float(info.metrics.get("elapsed_s", 0.0))
        stop_reason = str(info.metrics.get("stop_reason") or "")
        if stop_reason == "retry":
            print("已请求重新录制，本次采集会丢弃。")
            return "retry"
        suffix = "，已提前结束" if info.metrics.get("stopped_early") else ""
        print(
            f"Episode {info.episode_number}/{info.total_episodes} "
            f"完成: {frames} frames, {elapsed_s:.2f}s{suffix}。"
        )
        if frames <= 0:
            key = wait_for_key(
                "没有采集到帧。按右方向键舍弃并重新录制: ",
                (KEY_RIGHT,),
            )
            return "retry" if key == KEY_RIGHT else "abort"
        key = wait_for_key(
            "按左方向键保存并进入下一集；按右方向键舍弃并重新录制: ",
            (KEY_LEFT, KEY_RIGHT),
        )
        return "save" if key == KEY_LEFT else "retry"

    @contextmanager
    def recording_context(loop: RecordingLoopControl) -> Iterator[None]:
        print("录制中：按左方向键提前结束并进入选择；按右方向键舍弃并重新录制。")

        def request_select() -> None:
            loop.stop_reason = "select"
            loop.stop_requested = True

        def request_retry() -> None:
            loop.stop_reason = "retry"
            loop.stop_requested = True

        listener = KeyboardListener(
            stop_key=None,
            key_actions={
                KEY_LEFT: request_select,
                KEY_RIGHT: request_retry,
            },
        )
        listener.start()
        try:
            yield
        finally:
            listener.stop()
            if listener.last_key == KEY_LEFT:
                print("已收到提前结束请求，本 episode 已停止采集。")
            elif listener.last_key == KEY_RIGHT:
                print("已收到重新录制请求，本 episode 已停止采集。")

    return RecordingControls(
        before_episode=before_episode,
        after_episode=after_episode,
        recording_context=recording_context,
    )
