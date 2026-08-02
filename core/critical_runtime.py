from __future__ import annotations

import threading
from typing import Any, Callable


class CriticalComponents:
    """The single fail-fast path shared by every background unit the daemon cannot serve without.

    A component that exits unexpectedly leaves the daemon unable to do its job, so the first such
    failure is recorded, reported, and turned into the same shutdown, whatever the component was.
    """

    def __init__(
        self,
        *,
        failure: dict[str, Any],
        stopping: threading.Event,
        emit_log: Callable[..., None],
        style: Callable[[str, str], str],
        on_fatal: Callable[[], None],
    ) -> None:
        self.failure = failure
        self.stopping = stopping
        self.emit_log = emit_log
        self.style = style
        self.on_fatal = on_fatal

    def guard(self, component: str, run: Callable[[], None]) -> Callable[[], None]:
        def target() -> None:
            try:
                run()
            except BaseException as error:
                # Interrupts count too: the component is just as gone, and the daemon must not be
                # left reporting itself healthy. The failure still propagates so the thread reports
                # how it died.
                self.fail(component, error)
                raise

        return target

    def fail(self, component: str, error: BaseException) -> None:
        # Only the first failure is kept: it is the one that explains the shutdown.
        self.failure.setdefault("component", component)
        self.failure.setdefault("error", f"{type(error).__name__}: {error}")
        self.stopping.set()
        try:
            self.emit_log(
                self.style("COMPONENT FATAL", "1;31"),
                fields={
                    "component": component,
                    "type": type(error).__name__,
                    "reason": str(error).splitlines()[0] if str(error) else "",
                },
            )
        finally:
            # The shutdown is owed even when reporting the failure fails.
            self.on_fatal()
