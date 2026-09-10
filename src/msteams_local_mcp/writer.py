"""Local Microsoft Teams desktop write support for Windows.

This module intentionally does NOT use Microsoft Graph, OAuth, Azure app
registration, or Teams session-token scraping. It drives the already signed-in
Teams desktop client through Microsoft's Windows App Development CLI (``winapp``)
and Windows UI Automation.

Safety properties:
- exact existing chat is opened by Teams chat id via an official Teams deep link;
- the compose box is located through UI Automation and the requested text is
  verified before any send action is attempted;
- sending prefers invoking the visible Send button; Enter injection is only a
  fallback and is targeted at the verified compose control;
- failures are fail-closed: if the target window or compose control cannot be
  resolved reliably, nothing is sent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


class LocalTeamsWriteError(RuntimeError):
    """Raised when local Teams UI automation cannot complete safely."""


_WINAPP_ENV = "MSTEAMS_WINAPP"
_UI_APP_ENV = "MSTEAMS_UI_APP"
_NAV_DELAY = float(os.environ.get("MSTEAMS_UI_NAV_DELAY", "2.0"))
_UI_TIMEOUT = float(os.environ.get("MSTEAMS_UI_TIMEOUT", "12"))

# Accessible labels observed across English/Dutch Teams builds. We still have a
# control-type fallback below, so localization changes are not fatal by default.
_COMPOSE_LABELS = (
    "Type a message",
    "Type a new message",
    "Type your message",
    "Typ een bericht",
    "Typ een nieuw bericht",
    "Een bericht typen",
    "Bericht typen",
)
_SEND_LABELS = (
    "Send",
    "Send message",
    "Verzenden",
    "Bericht verzenden",
)
_APP_CANDIDATES = ("ms-teams", "msteams", "teams")


def _winapp_path() -> str:
    configured = os.environ.get(_WINAPP_ENV)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise LocalTeamsWriteError(f"{_WINAPP_ENV} points to a missing file: {path}")
    found = shutil.which("winapp") or shutil.which("winapp.exe")
    if not found:
        raise LocalTeamsWriteError(
            "Microsoft winapp CLI is not installed or not on PATH. Install it with: "
            "winget install Microsoft.winappcli --source winget"
        )
    return found


def _json_error(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)
        if err:
            return str(err)
    return ""


def _run_winapp(
    args: list[str], *, timeout: float = _UI_TIMEOUT, allow_failure: bool = False
) -> tuple[int, dict[str, Any]]:
    cmd = [_winapp_path(), *args]
    if "--json" not in args:
        cmd.append("--json")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalTeamsWriteError(f"winapp timed out: {' '.join(args)}") from exc

    data: dict[str, Any] = {}
    raw = (proc.stdout or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {"raw": raw}

    if proc.returncode != 0 and not allow_failure:
        detail = _json_error(data) or (proc.stderr or "").strip() or raw
        raise LocalTeamsWriteError(
            f"winapp failed ({proc.returncode}) for {' '.join(args)}"
            + (f": {detail}" if detail else "")
        )
    return proc.returncode, data


def _teams_status() -> dict[str, Any]:
    preferred = os.environ.get(_UI_APP_ENV)
    candidates = (preferred,) if preferred else _APP_CANDIDATES
    last_error = ""
    for app in candidates:
        if not app:
            continue
        code, data = _run_winapp(
            ["ui", "status", "-a", app], timeout=5, allow_failure=True
        )
        if code == 0 and data.get("hwnd"):
            data["app_selector"] = app
            return data
        last_error = _json_error(data) or str(data.get("raw") or "")
    raise LocalTeamsWriteError(
        "No running Microsoft Teams desktop window could be resolved through UI Automation. "
        "Open the new Teams desktop client and sign in first."
        + (f" Last winapp error: {last_error}" if last_error else "")
    )


def local_write_status() -> dict[str, Any]:
    """Return non-sensitive readiness information for local Teams writes."""
    result: dict[str, Any] = {
        "platform": sys.platform,
        "supported": sys.platform.startswith("win"),
        "winapp": None,
        "teams_running": False,
    }
    if not result["supported"]:
        result["reason"] = "Local write automation is currently implemented for Windows only."
        return result
    try:
        result["winapp"] = _winapp_path()
    except LocalTeamsWriteError as exc:
        result["reason"] = str(exc)
        return result
    try:
        status = _teams_status()
        result.update(
            teams_running=True,
            process_name=status.get("processName", ""),
            window_title=status.get("windowTitle", ""),
            hwnd=status.get("hwnd"),
            app_selector=status.get("app_selector", ""),
        )
    except LocalTeamsWriteError as exc:
        result["reason"] = str(exc)
    return result


def _chat_deep_link(conversation_id: str) -> str:
    cid = conversation_id.strip()
    if not cid.startswith("19:"):
        raise LocalTeamsWriteError(
            "conversation_id is not a Teams chat id (expected a value starting with '19:')."
        )
    if "@thread.tacv2" in cid:
        raise LocalTeamsWriteError(
            "This conversation is a Teams channel. The first local-write version only sends "
            "to existing 1:1/group chats; channel sending needs the local team/group id as well."
        )
    # Keep Teams' normal chat-id punctuation readable while encoding anything else.
    encoded = quote(cid, safe=":@._-")
    return f"msteams://teams.microsoft.com/l/chat/{encoded}/conversations"


def _open_chat(conversation_id: str) -> None:
    if not sys.platform.startswith("win"):
        raise LocalTeamsWriteError("Local Teams sending is currently supported on Windows only.")
    url = _chat_deep_link(conversation_id)
    try:
        os.startfile(url)  # type: ignore[attr-defined]  # Windows-only API
    except OSError as exc:
        raise LocalTeamsWriteError(
            "Windows could not open the msteams: deep link. Ensure the new Teams desktop client "
            "is installed and registered as the msteams protocol handler."
        ) from exc


def _search(hwnd: int, query: str, max_results: int = 30) -> list[dict[str, Any]]:
    code, data = _run_winapp(
        ["ui", "search", query, "--max", str(max_results), "-w", str(hwnd)],
        timeout=6,
        allow_failure=True,
    )
    if code not in (0, 1):
        return []
    matches = data.get("matches") or []
    return [m for m in matches if isinstance(m, dict)]


def _selector(m: dict[str, Any]) -> Optional[str]:
    if m.get("selector"):
        return str(m["selector"])
    anc = m.get("invokableAncestor")
    if isinstance(anc, dict) and anc.get("selector"):
        return str(anc["selector"])
    return None


def _visible_edit_candidate(m: dict[str, Any]) -> bool:
    typ = str(m.get("type") or "").lower()
    return (
        typ in {"edit", "document"}
        and m.get("isEnabled", True) is not False
        and m.get("isOffscreen", False) is not True
        and float(m.get("width") or 0) >= 120
        and float(m.get("height") or 0) >= 18
        and bool(_selector(m))
    )


def _find_compose(hwnd: int) -> dict[str, Any]:
    deadline = time.monotonic() + _UI_TIMEOUT
    while time.monotonic() < deadline:
        candidates: list[dict[str, Any]] = []
        for label in _COMPOSE_LABELS:
            candidates.extend(m for m in _search(hwnd, label) if _visible_edit_candidate(m))
        if not candidates:
            for control_type in ("Document", "Edit"):
                candidates.extend(
                    m for m in _search(hwnd, control_type) if _visible_edit_candidate(m)
                )
        if candidates:
            # Compose is normally the lowest large editable surface in the chat window.
            candidates.sort(
                key=lambda m: (float(m.get("y") or 0), float(m.get("width") or 0)),
                reverse=True,
            )
            return candidates[0]
        time.sleep(0.5)
    raise LocalTeamsWriteError(
        "Teams opened, but no reliable message compose control was found. Nothing was sent."
    )


def _get_value(hwnd: int, selector: str) -> str:
    _, data = _run_winapp(["ui", "get-value", selector, "-w", str(hwnd)], timeout=5)
    return str(data.get("text") or "")


def _set_and_verify(hwnd: int, compose: dict[str, Any], message: str) -> str:
    selector = _selector(compose)
    if not selector:
        raise LocalTeamsWriteError("Compose control has no stable selector. Nothing was sent.")
    _run_winapp(["ui", "set-value", selector, message, "-w", str(hwnd)], timeout=8)
    actual = _get_value(hwnd, selector)
    if actual != message:
        raise LocalTeamsWriteError(
            "Teams compose text could not be verified exactly after setting it. Nothing was sent; "
            "the draft may still be visible in Teams for manual review."
        )
    return selector


def _find_send_button(hwnd: int) -> Optional[str]:
    candidates: list[dict[str, Any]] = []
    for label in _SEND_LABELS:
        candidates.extend(_search(hwnd, label, max_results=20))
    usable: list[dict[str, Any]] = []
    for m in candidates:
        sel = _selector(m)
        if not sel:
            continue
        typ = str(m.get("type") or "").lower()
        if m.get("isEnabled", True) is False or m.get("isOffscreen", False) is True:
            continue
        if typ == "button" or m.get("isInvokable") or isinstance(m.get("invokableAncestor"), dict):
            usable.append(m)
    if not usable:
        return None
    usable.sort(key=lambda m: float(m.get("y") or 0), reverse=True)
    return _selector(usable[0])


def _verify_cleared(hwnd: int, compose_selector: str) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if _get_value(hwnd, compose_selector) == "":
                return True
        except LocalTeamsWriteError:
            # Teams may replace the compose DOM node after send. Re-resolve it once.
            try:
                compose = _find_compose(hwnd)
                new_selector = _selector(compose)
                if new_selector and _get_value(hwnd, new_selector) == "":
                    return True
            except LocalTeamsWriteError:
                pass
        time.sleep(0.4)
    return False


def send_chat_message(
    conversation_id: str,
    message: str,
    *,
    allow_enter_fallback: bool = True,
) -> dict[str, Any]:
    """Send plain text to an existing Teams 1:1/group chat through the local desktop UI.

    ``conversation_id`` must come from the local cache (``list_conversations`` or
    ``read_conversation``). No Graph/API token is used. The function opens that exact
    chat with a Teams deep link, verifies the compose text, then invokes Send.
    """
    if not message or not message.strip():
        raise LocalTeamsWriteError("Refusing to send an empty Teams message.")
    if len(message) > 25_000:
        raise LocalTeamsWriteError("Message is too large for the local Teams sender (max 25,000 chars).")

    # Validate dependencies before changing Teams navigation state.
    _winapp_path()
    _chat_deep_link(conversation_id)
    _open_chat(conversation_id)
    time.sleep(max(0.2, _NAV_DELAY))

    status = _teams_status()
    hwnd = int(status["hwnd"])
    compose = _find_compose(hwnd)
    compose_selector = _set_and_verify(hwnd, compose, message)

    send_selector = _find_send_button(hwnd)
    method = "send_button"
    if send_selector:
        try:
            _run_winapp(["ui", "invoke", send_selector, "-w", str(hwnd)], timeout=6)
        except LocalTeamsWriteError:
            send_selector = None

    if not send_selector:
        if not allow_enter_fallback:
            raise LocalTeamsWriteError(
                "The Send button could not be invoked. The verified draft was left in Teams and "
                "Enter fallback is disabled."
            )
        method = "enter_fallback"
        # Focus + SendInput is intentionally the last resort for Chromium/WebView2 controls.
        _run_winapp(["ui", "focus", compose_selector, "-w", str(hwnd)], timeout=5)
        _run_winapp(
            [
                "ui",
                "send-keys",
                "enter",
                "--target",
                compose_selector,
                "-w",
                str(hwnd),
                "--via",
                "send-input",
            ],
            timeout=6,
        )

    if not _verify_cleared(hwnd, compose_selector):
        raise LocalTeamsWriteError(
            "A send action was attempted, but the compose box did not clear, so delivery could not "
            "be verified. Check Teams before retrying to avoid a duplicate."
        )

    return {
        "sent": True,
        "conversation_id": conversation_id,
        "transport": "local_teams_desktop_ui",
        "send_method": method,
        "graph_used": False,
    }
