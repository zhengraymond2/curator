from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, TextIO, Union


ProgressMessage = Union[str, Callable[[], str]]


class ProgressReporter:
    def __init__(self, stream: TextIO | None = None, *, enabled: bool = True) -> None:
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self._lock = threading.Lock()

    @classmethod
    def disabled(cls) -> "ProgressReporter":
        return cls(enabled=False)

    def log(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.stream.write(f"[curator] {message}\n")
            self.stream.flush()

    @contextmanager
    def step(
        self,
        message: str,
        *,
        done: ProgressMessage | None = None,
        failed: ProgressMessage | None = None,
    ) -> Iterator[None]:
        if not self.enabled:
            yield
            return

        spinner = _Spinner(self.stream, self._lock, message)
        spinner.start()
        try:
            yield
        except Exception:
            spinner.stop("Failed", _resolve_message(failed) or message)
            raise
        else:
            spinner.stop("Done", _resolve_message(done) or message)


class _Spinner:
    def __init__(self, stream: TextIO, lock: threading.Lock, message: str) -> None:
        self.stream = stream
        self.lock = lock
        self.message = message
        self.start_time = 0.0
        self.is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.start_time = time.monotonic()
        if not self.is_tty:
            with self.lock:
                self.stream.write(f"[curator] Starting: {self.message}\n")
                self.stream.flush()
            return

        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, status: str, message: str) -> None:
        elapsed = time.monotonic() - self.start_time
        if self._thread is not None:
            self._stop.set()
            self._thread.join()

        with self.lock:
            if self.is_tty:
                self.stream.write("\r\x1b[K")
            self.stream.write(f"[curator] {status}: {message} ({elapsed:.1f}s)\n")
            self.stream.flush()

    def _spin(self) -> None:
        frames = "|/-\\"
        index = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - self.start_time
            with self.lock:
                self.stream.write(f"\r[curator] {frames[index % len(frames)]} {self.message} ({elapsed:.1f}s)")
                self.stream.flush()
            index += 1
            self._stop.wait(0.1)


def _resolve_message(message: ProgressMessage | None) -> str | None:
    if message is None:
        return None
    if callable(message):
        return message()
    return message
