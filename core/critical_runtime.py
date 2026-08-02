from __future__ import annotations

import threading
from typing import Any, Callable


def describe_error(error: BaseException) -> str:
    """Describe a failure without ever failing. An exception whose own repr raises must still be
    recorded, because that record is what stops the daemon."""
    try:
        name = type(error).__name__
    except BaseException:
        name = "UnknownError"
    try:
        message = str(error)
    except BaseException:
        message = "<error description unavailable>"
    return f"{name}: {message}"


class CriticalComponents:
    """The single fail-fast path shared by every background unit the daemon cannot serve without.

    A component that stops running leaves the daemon unable to do its job, so the first such
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
        self.lock = threading.Lock()

    def guard(self, component: str, run: Callable[[], None]) -> Callable[[], None]:
        def target() -> None:
            try:
                run()
            except BaseException as error:
                # Interrupts count too: the component is just as gone, and the daemon must not be
                # left reporting itself healthy. The original failure still propagates so the
                # thread reports how it died.
                self.fail(component, error)
                raise
            if not self.stopping.is_set():
                # Returning is only legitimate while the daemon is shutting down. Any other return
                # is a component that quietly stopped working.
                error = RuntimeError(f"the {component} loop returned while the daemon was running")
                self.fail(component, error)
                raise error

        return target

    def fail(self, component: str, error: BaseException) -> None:
        description = describe_error(error)
        with self.lock:
            # Both halves of the first failure are committed together so they always describe the
            # same component.
            if "component" not in self.failure:
                self.failure["component"] = component
                self.failure["error"] = description
        self.stopping.set()
        try:
            self.emit_log(
                self.style("COMPONENT FATAL", "1;31"),
                fields={
                    "component": component,
                    "type": type(error).__name__,
                    "reason": description.splitlines()[0],
                },
            )
        except BaseException:
            # Reporting is best effort; losing the log must not cost the shutdown or mask the
            # failure that caused it.
            pass
        self.on_fatal()
