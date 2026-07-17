"""Playwright-backed browser worker used by the browser tool family."""
from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class _BrowserSession:
    session_id: str
    browser: Any
    context: Any
    page: Any
    headless: bool
    created_at: float = field(default_factory=time.time)
    console_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    network_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))


@dataclass(slots=True)
class _BrowserRequest:
    operation: str
    arguments: dict[str, Any]
    future: Future


class BrowserService:
    """Runs all Playwright objects on one dedicated thread.

    Playwright's synchronous API is greenlet/thread-affine. Routing every
    browser operation through this worker avoids intermittent failures when
    tool calls originate from different runtime threads.
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self._requests: queue.Queue[_BrowserRequest | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def execute(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        self._ensure_thread()
        future: Future = Future()
        self._requests.put(_BrowserRequest(operation, dict(arguments or {}), future))
        return future.result(timeout=max(1.0, timeout_seconds))

    def shutdown(self) -> None:
        with self._start_lock:
            thread = self._thread
            if thread is None:
                return
            if thread.is_alive():
                self._requests.put(None)
                thread.join(timeout=5)
            if not thread.is_alive() and self._thread is thread:
                self._thread = None

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="quimera-browser-worker",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        sessions: dict[str, _BrowserSession] = {}
        playwright = None
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    break
                try:
                    if playwright is None:
                        from playwright.sync_api import sync_playwright

                        playwright = sync_playwright().start()
                    result = self._dispatch(playwright, sessions, request.operation, request.arguments)
                except Exception as exc:  # noqa: BLE001 - propagated to the tool result
                    request.future.set_exception(exc)
                else:
                    request.future.set_result(result)
        except Exception as exc:  # noqa: BLE001
            while True:
                try:
                    request = self._requests.get_nowait()
                except queue.Empty:
                    break
                if request is not None:
                    request.future.set_exception(exc)
        finally:
            for session in list(sessions.values()):
                self._close_session(session)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass

    def _dispatch(
        self,
        playwright: Any,
        sessions: dict[str, _BrowserSession],
        operation: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if operation == "start":
            return self._start_session(playwright, sessions, args)
        if operation == "status":
            return self._status(sessions)

        session = self._require_session(sessions, str(args.get("session_id", "")))
        if operation == "close":
            self._close_session(session)
            sessions.pop(session.session_id, None)
            return {"session_id": session.session_id, "closed": True}
        if operation == "navigate":
            response = session.page.goto(
                args["url"],
                wait_until=args.get("wait_until", "load"),
                timeout=int(args.get("timeout_ms", 30_000)),
            )
            return self._page_state(session, response=response)
        if operation == "snapshot":
            return self._snapshot(session, args)
        if operation == "click":
            return self._click(session, args)
        if operation == "type":
            return self._type(session, args)
        if operation == "press":
            return self._press(session, args)
        if operation == "mouse":
            return self._mouse(session, args)
        if operation == "wait":
            return self._wait(session, args)
        if operation == "evaluate":
            value = session.page.evaluate(args["expression"], args.get("arg"))
            return {**self._page_state(session), "value": self._json_safe(value)}
        if operation == "screenshot":
            return self._screenshot(session, args)
        if operation == "console":
            return self._consume_events(session.console_events, args)
        if operation == "network":
            return self._consume_events(session.network_events, args)
        raise ValueError(f"Operação de navegador desconhecida: {operation}")

    def _start_session(
        self,
        playwright: Any,
        sessions: dict[str, _BrowserSession],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        executable_path = args.get("executable_path") or self._find_browser_executable()
        launch_args = ["--disable-dev-shm-usage"]
        if bool(args.get("disable_gpu", False)):
            launch_args.append("--disable-gpu")
        browser = playwright.chromium.launch(
            headless=bool(args.get("headless", True)),
            executable_path=executable_path,
            args=launch_args,
        )
        viewport = {
            "width": int(args.get("width", 1280)),
            "height": int(args.get("height", 720)),
        }
        context = browser.new_context(
            viewport=viewport,
            device_scale_factor=float(args.get("device_scale_factor", 1.0)),
            ignore_https_errors=bool(args.get("ignore_https_errors", True)),
        )
        page = context.new_page()
        page.set_default_timeout(int(args.get("timeout_ms", 15_000)))
        session_id = uuid.uuid4().hex[:12]
        session = _BrowserSession(
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
            headless=bool(args.get("headless", True)),
        )
        sessions[session_id] = session
        self._bind_events(session)

        url = args.get("url")
        response = None
        if url:
            response = page.goto(
                url,
                wait_until=args.get("wait_until", "load"),
                timeout=int(args.get("navigation_timeout_ms", 30_000)),
            )
        return {
            **self._page_state(session, response=response),
            "viewport": viewport,
            "headless": session.headless,
            "executable_path": executable_path,
        }

    @staticmethod
    def _find_browser_executable() -> str | None:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            executable = shutil.which(name)
            if executable:
                return executable
        return None

    def _bind_events(self, session: _BrowserSession) -> None:
        def on_console(message: Any) -> None:
            location = message.location or {}
            session.console_events.append({
                "kind": "console",
                "type": message.type,
                "text": message.text,
                "url": location.get("url", ""),
                "line": location.get("lineNumber"),
                "column": location.get("columnNumber"),
                "timestamp": time.time(),
            })

        def on_page_error(error: Any) -> None:
            session.console_events.append({
                "kind": "pageerror",
                "type": "error",
                "text": str(error),
                "timestamp": time.time(),
            })

        def on_request(request: Any) -> None:
            session.network_events.append({
                "kind": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "timestamp": time.time(),
            })

        def on_response(response: Any) -> None:
            session.network_events.append({
                "kind": "response",
                "status": response.status,
                "ok": response.ok,
                "url": response.url,
                "resource_type": response.request.resource_type,
                "timestamp": time.time(),
            })

        session.page.on("console", on_console)
        session.page.on("pageerror", on_page_error)
        session.page.on("request", on_request)
        session.page.on("response", on_response)

    def _snapshot(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        selector = args.get("selector") or "body"
        max_text_chars = max(100, min(int(args.get("max_text_chars", 12_000)), 100_000))
        max_elements = max(1, min(int(args.get("max_elements", 150)), 1000))
        locator = session.page.locator(selector).first
        locator.wait_for(state="attached", timeout=int(args.get("timeout_ms", 15_000)))
        text = locator.inner_text(timeout=int(args.get("timeout_ms", 15_000)))
        if len(text) > max_text_chars:
            text = text[:max_text_chars] + "\n...[texto truncado]"

        elements = session.page.evaluate(
            """({selector, limit}) => {
                const root = document.querySelector(selector);
                if (!root) return [];
                const cssPath = (el) => {
                    if (el.id) return `#${CSS.escape(el.id)}`;
                    const testId = el.getAttribute('data-testid');
                    if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
                    const name = el.getAttribute('name');
                    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
                    const aria = el.getAttribute('aria-label');
                    if (aria) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
                        let part = node.tagName.toLowerCase();
                        const siblings = [...node.parentElement.children].filter(x => x.tagName === node.tagName);
                        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
                        parts.unshift(part);
                        node = node.parentElement;
                    }
                    return `body > ${parts.join(' > ')}`;
                };
                const candidates = root.querySelectorAll(
                    'a,button,input,textarea,select,summary,[role],canvas,[contenteditable="true"],video,audio'
                );
                return [...candidates].slice(0, limit).map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        index,
                        selector: cssPath(el),
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        type: el.getAttribute('type') || '',
                        name: el.getAttribute('name') || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        text: (el.innerText || el.value || el.getAttribute('alt') || '').trim().slice(0, 300),
                        disabled: Boolean(el.disabled),
                        visible: rect.width > 0 && rect.height > 0,
                        bounds: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    };
                });
            }""",
            {"selector": selector, "limit": max_elements},
        )
        return {
            **self._page_state(session),
            "selector": selector,
            "text": text,
            "elements": elements,
        }

    def _click(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("selector"):
            session.page.locator(args["selector"]).first.click(
                button=args.get("button", "left"),
                click_count=int(args.get("click_count", 1)),
                force=bool(args.get("force", False)),
                timeout=int(args.get("timeout_ms", 15_000)),
            )
        else:
            session.page.mouse.click(
                float(args["x"]),
                float(args["y"]),
                button=args.get("button", "left"),
                click_count=int(args.get("click_count", 1)),
                delay=int(args.get("delay_ms", 0)),
            )
        return self._page_state(session)

    def _type(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        selector = args["selector"]
        text = str(args.get("text", ""))
        clear = bool(args.get("clear", True))
        delay_ms = max(0, int(args.get("delay_ms", 0)))
        locator = session.page.locator(selector).first
        locator.wait_for(state="attached", timeout=int(args.get("timeout_ms", 15_000)))

        initial = "" if clear else self._editable_value(locator)
        target = initial + text
        if delay_ms <= 0:
            locator.fill(target)
        else:
            accumulated = initial
            if clear:
                locator.fill("")
            for character in text:
                accumulated += character
                # Frameworks podem recriar o input a cada evento. Reconsultar o
                # seletor em cada iteração evita operar em um locator destacado.
                session.page.locator(selector).first.fill(accumulated)
                session.page.wait_for_timeout(delay_ms)

        current = self._editable_value(session.page.locator(selector).first)
        return {**self._page_state(session), "value": current}

    @staticmethod
    def _editable_value(locator: Any) -> str:
        value = locator.evaluate(
            """element => {
                if ('value' in element) return String(element.value ?? '');
                return String(element.textContent ?? '');
            }"""
        )
        return str(value)

    def _press(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        key = args["key"]
        selector = args.get("selector")
        duration_ms = max(0, int(args.get("duration_ms", 0)))
        if selector:
            session.page.locator(selector).first.press(key)
        elif duration_ms:
            session.page.keyboard.down(key)
            session.page.wait_for_timeout(duration_ms)
            session.page.keyboard.up(key)
        else:
            session.page.keyboard.press(key)
        return self._page_state(session)

    def _mouse(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        action = args["action"]
        if action == "move":
            session.page.mouse.move(float(args["x"]), float(args["y"]), steps=int(args.get("steps", 1)))
        elif action == "down":
            session.page.mouse.down(button=args.get("button", "left"))
        elif action == "up":
            session.page.mouse.up(button=args.get("button", "left"))
        elif action == "click":
            session.page.mouse.click(
                float(args["x"]),
                float(args["y"]),
                button=args.get("button", "left"),
                click_count=int(args.get("click_count", 1)),
                delay=int(args.get("delay_ms", 0)),
            )
        elif action == "wheel":
            session.page.mouse.wheel(float(args.get("delta_x", 0)), float(args.get("delta_y", 0)))
        else:
            raise ValueError(f"Ação de mouse inválida: {action}")
        return self._page_state(session)

    def _wait(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("selector"):
            session.page.locator(args["selector"]).first.wait_for(
                state=args.get("state", "visible"),
                timeout=int(args.get("timeout_ms", 15_000)),
            )
        elif args.get("expression"):
            session.page.wait_for_function(
                args["expression"],
                timeout=int(args.get("timeout_ms", 15_000)),
            )
        else:
            session.page.wait_for_timeout(int(args.get("timeout_ms", 1000)))
        return self._page_state(session)

    def _screenshot(self, session: _BrowserSession, args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_args = {
            "path": str(path),
            "type": "jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "png",
        }
        if screenshot_args["type"] == "jpeg":
            screenshot_args["quality"] = int(args.get("quality", 85))
        if args.get("selector"):
            session.page.locator(args["selector"]).first.screenshot(**screenshot_args)
        else:
            session.page.screenshot(full_page=bool(args.get("full_page", False)), **screenshot_args)
        return {**self._page_state(session), "path": str(path), "bytes": path.stat().st_size}

    @staticmethod
    def _consume_events(events: deque[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit", 100)), 1000))
        values = list(events)[-limit:]
        if bool(args.get("clear", False)):
            events.clear()
        return {"events": values, "count": len(values), "cleared": bool(args.get("clear", False))}

    @staticmethod
    def _require_session(sessions: dict[str, _BrowserSession], session_id: str) -> _BrowserSession:
        session = sessions.get(session_id)
        if session is None:
            raise ValueError(f"Sessão de navegador inexistente: {session_id}")
        return session

    @staticmethod
    def _status(sessions: dict[str, _BrowserSession]) -> dict[str, Any]:
        values = [
            {
                "session_id": session.session_id,
                "url": session.page.url,
                "title": session.page.title(),
                "headless": session.headless,
                "created_at": session.created_at,
            }
            for session in sessions.values()
        ]
        return {"sessions": values, "count": len(values)}

    @staticmethod
    def _page_state(session: _BrowserSession, *, response: Any = None) -> dict[str, Any]:
        result = {
            "session_id": session.session_id,
            "url": session.page.url,
            "title": session.page.title(),
        }
        if response is not None:
            result["status"] = response.status
            result["ok"] = response.ok
        return result

    @staticmethod
    def _close_session(session: _BrowserSession) -> None:
        for resource in (session.context, session.browser):
            try:
                resource.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _json_safe(value: Any) -> Any:
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            return repr(value)
        return value
