from __future__ import annotations

from typing import Any


TERMINAL_TURN_STATUSES = {
    "completed",
    "success",
    "failed",
    "interrupted",
    "cancelled",
    "canceled",
}


class CodexThreadStream:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.initialized = False

    def apply(self, change: dict[str, Any]) -> None:
        if change.get("type") == "snapshot":
            entries = (
                change.get("conversationState", {})
                .get("turnHistory", {})
                .get("history", {})
                .get("entitiesByKey", {})
            )
            self.entries = {
                str(key): self._entry(value)
                for key, value in entries.items()
                if isinstance(value, dict)
            }
            self.initialized = True
            return
        if change.get("type") != "patches":
            return
        for patch in change.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            path = patch.get("path")
            if (
                not isinstance(path, list)
                or path[:3] != ["turnHistory", "history", "entitiesByKey"]
                or len(path) < 4
            ):
                continue
            key = str(path[3])
            operation = patch.get("op")
            if len(path) == 4:
                if operation == "remove":
                    self.entries.pop(key, None)
                elif isinstance(patch.get("value"), dict):
                    self.entries[key] = self._entry(patch["value"])
                continue
            entry = self.entries.setdefault(key, {"items": {}})
            field = path[4]
            if operation == "remove":
                if field == "items" and len(path) >= 6:
                    entry.setdefault("items", {}).pop(int(path[5]), None)
                else:
                    entry.pop(str(field), None)
                continue
            value = patch.get("value")
            if field != "items":
                entry[str(field)] = value
                continue
            if len(path) < 6:
                if isinstance(value, list):
                    entry["items"] = {
                        index: item
                        for index, item in enumerate(value)
                        if isinstance(item, dict)
                    }
                continue
            try:
                index = int(path[5])
            except (TypeError, ValueError):
                continue
            items = entry.setdefault("items", {})
            if len(path) == 6:
                if isinstance(value, dict):
                    items[index] = dict(value)
                continue
            item = items.setdefault(index, {})
            item[str(path[6])] = value

    def result(self, turn_id: str) -> dict[str, Any] | None:
        entry = self._turn(turn_id)
        if entry is None:
            return None
        status = str(entry.get("status") or "")
        if status not in TERMINAL_TURN_STATUSES:
            return None
        messages = [
            item.get("text")
            for _, item in sorted(entry.get("items", {}).items())
            if item.get("type") == "agentMessage" and item.get("text")
        ]
        error = entry.get("error")
        if isinstance(error, dict):
            error = (
                str(error.get("message") or error.get("additionalDetails") or "").strip()
                or None
            )
        elif error is not None:
            error = str(error)
        return {
            "status": status,
            "response": str(messages[-1]) if messages else None,
            "error": error,
        }

    def contains_client_message(
        self,
        turn_id: str,
        client_message_id: str,
    ) -> bool:
        entry = self._turn(turn_id)
        if entry is None:
            return False
        return any(
            item.get("type") == "userMessage"
            and str(item.get("clientId") or "") == client_message_id
            for item in entry.get("items", {}).values()
        )

    def is_terminal(self, turn_id: str) -> bool:
        entry = self._turn(turn_id)
        return (
            entry is not None
            and str(entry.get("status") or "") in TERMINAL_TURN_STATUSES
        )

    def has_active_turn(self) -> bool:
        return any(
            str(entry.get("status") or "")
            and str(entry.get("status") or "") not in TERMINAL_TURN_STATUSES
            for entry in self.entries.values()
        )

    def has_competing_active_turn(self, turn_id: str) -> bool:
        return any(
            str(entry.get("status") or "")
            and str(entry.get("status") or "") not in TERMINAL_TURN_STATUSES
            and str(entry.get("turnId") or "") != turn_id
            for entry in self.entries.values()
        )

    def _turn(self, turn_id: str) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in self.entries.values()
                if str(entry.get("turnId") or "") == turn_id
            ),
            None,
        )

    @staticmethod
    def _entry(value: dict[str, Any]) -> dict[str, Any]:
        entry = {key: item for key, item in value.items() if key != "items"}
        entry["items"] = {
            index: item
            for index, item in enumerate(value.get("items") or [])
            if isinstance(item, dict)
        }
        return entry
