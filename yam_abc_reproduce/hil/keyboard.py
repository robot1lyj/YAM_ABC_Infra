"""Local terminal events; no dependency on desktop keyboard hooks."""

import select
import sys
import threading


class Keyboard:
    def __init__(self, callback):
        self.callback = callback
        self._stop = threading.Event()
        self._saved = None
        self._thread = None

    def __enter__(self):
        if sys.stdin.isatty():
            import termios
            import tty

            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        mapping = {
            "s": "start",
            "i": "takeover",
            " ": "hold",
            "q": "quit",
            "1": "mode:teleop",
            "2": "mode:inference",
            "3": "mode:hil",
            "4": "mode:collect",
            "r": "record",
            "x": "discard",
            "g": "success",
            "f": "failure",
        }
        while not self._stop.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                key = sys.stdin.read(1).lower()
                if key in mapping:
                    try:
                        self.callback(mapping[key])
                    except Exception as exc:
                        print(f"Event rejected: {exc}", flush=True)

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(0.3)
        if self._saved:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)
