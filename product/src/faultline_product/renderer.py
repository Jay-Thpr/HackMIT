from collections.abc import Callable


class TerminalRenderer:
    def __init__(self, write: Callable[[str], None] = print):
        self._write = write

    def event(self, label: str, message: str) -> None:
        self._write(f"[{label}] {message}")
