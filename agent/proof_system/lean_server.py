"""Persistent Lean language-server client used by the Lean adapter."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class LeanServerError(RuntimeError):
    pass


class LeanServerTimeout(LeanServerError):
    pass


class LeanServerAmbiguousCompletion(LeanServerError):
    """Diagnostics arrived, but the server never emitted a conclusive end signal."""


@dataclass(frozen=True)
class LeanServerCheck:
    raw_output: str
    exit_code: int


class LeanServerClient:
    """Minimal JSON-RPC client for Lean's persistent language server."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        root: Path | None,
        diagnostics_fallback_seconds: float = 0.5,
    ) -> None:
        self.command = tuple(command)
        self.cwd = cwd
        self.root = root
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._send_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[Any, dict[str, Any]] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._completed_documents: set[str] = set()
        self._processing_documents: set[str] = set()
        self._diagnostic_publications: dict[str, int] = {}
        self._diagnostic_fallback_deadlines: dict[str, float] = {}
        self._document_version: dict[str, int] = {}
        self._diagnostics_fallback_seconds = diagnostics_fallback_seconds
        self._stderr: list[str] = []
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._reader_error: LeanServerError | None = None

    def start(self, *, timeout_seconds: float) -> None:
        self._process = subprocess.Popen(
            list(self.command),
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._reader = threading.Thread(target=self._read_stdout, name="lean-server-stdout", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="lean-server-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()

        root_uri = self.root.as_uri() if self.root is not None else None
        self._request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
            },
            timeout_seconds=timeout_seconds,
        )
        self._notify("initialized", {})

    def is_alive(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._reader_error is None
            and self._reader is not None
            and self._reader.is_alive()
        )

    def check_file(self, path: Path, *, timeout_seconds: float) -> LeanServerCheck:
        if not self.is_alive():
            raise LeanServerError("Lean server is not running.")

        uri = path.as_uri()
        text = path.read_text(encoding="utf-8")
        with self._condition:
            self._diagnostics.pop(uri, None)
            self._completed_documents.discard(uri)
            self._processing_documents.discard(uri)
            self._diagnostic_publications.pop(uri, None)
            self._diagnostic_fallback_deadlines.pop(uri, None)
            # Bump the version on every check. Reusing a fixed version means a
            # reopened URI can be served stale results, and some Lean versions
            # only emit progress for a version they consider new.
            self._document_version[uri] = self._document_version.get(uri, 0) + 1
            version = self._document_version[uri]
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "lean",
                    "version": version,
                    "text": text,
                }
            },
        )
        try:
            diagnostics = self._wait_for_check_completion(uri, timeout_seconds=timeout_seconds)
        finally:
            self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        raw = _format_lsp_diagnostics(path, diagnostics)
        has_error = any(diagnostic.get("severity", 1) == 1 for diagnostic in diagnostics)
        return LeanServerCheck(raw_output=raw, exit_code=1 if has_error else 0)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._request("shutdown", None, timeout_seconds=2.0)
                self._notify("exit", {})
            except LeanServerError:
                pass
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        self._process = None

    def _request(self, method: str, params: Any, *, timeout_seconds: float) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while request_id not in self._responses:
                self._raise_reader_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LeanServerTimeout(f"Lean server request {method!r} timed out after {timeout_seconds}s.")
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if "error" in response:
            raise LeanServerError(f"Lean server request {method!r} failed: {response['error']}")
        return response

    def _notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, payload: dict[str, Any]) -> None:
        self._raise_reader_error()
        if self._process is None or self._process.stdin is None:
            raise LeanServerError("Lean server stdin is closed.")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._send_lock:
            self._process.stdin.write(header + body)
            self._process.stdin.flush()

    def _wait_for_check_completion(self, uri: str, *, timeout_seconds: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while uri not in self._completed_documents:
                self._raise_reader_error()
                now = time.monotonic()
                fallback_deadline = self._diagnostic_fallback_deadlines.get(uri)
                if fallback_deadline is not None and now >= fallback_deadline:
                    raise LeanServerAmbiguousCompletion(
                        "Lean server published updated diagnostics but never emitted "
                        "an explicit completion signal."
                    )
                remaining = deadline - now
                if remaining <= 0:
                    if self._diagnostic_publications.get(uri, 0):
                        raise LeanServerAmbiguousCompletion(
                            "Lean server published diagnostics but never emitted a "
                            f"conclusive completion signal within {timeout_seconds}s."
                        )
                    raise LeanServerTimeout(f"Lean checker timed out after {timeout_seconds}s.")
                if fallback_deadline is not None:
                    remaining = min(remaining, max(0.0, fallback_deadline - now))
                self._condition.wait(remaining)
            self._completed_documents.discard(uri)
            self._processing_documents.discard(uri)
            self._diagnostic_publications.pop(uri, None)
            self._diagnostic_fallback_deadlines.pop(uri, None)
            return self._diagnostics.pop(uri, [])

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        try:
            while True:
                content_length: int | None = None
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    if line in {b"\r\n", b"\n"}:
                        break
                    name, _, value = line.decode("ascii", errors="replace").partition(":")
                    if name.lower() == "content-length":
                        content_length = int(value.strip())
                if content_length is None:
                    raise LeanServerError("Lean server message omitted Content-Length.")
                body = _read_exact(stream, content_length)
                self._handle_message(json.loads(body.decode("utf-8")))
        except Exception as exc:
            error = exc if isinstance(exc, LeanServerError) else LeanServerError(str(exc))
            logger.warning("Lean server stdout reader failed: %s", error)
            logger.debug("Lean server stdout reader failure details", exc_info=True)
            with self._condition:
                self._reader_error = error
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _handle_message(self, message: dict[str, Any]) -> None:
        logger.debug("Lean server message: %s", _message_summary(message))
        # A server-initiated *request* carries both ``id`` and ``method``.
        # Answer it and do not store it as a response to one of our requests.
        if "id" in message and "method" in message:
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": None})
            return
        with self._condition:
            # A *response* to one of our requests carries ``id`` but no ``method``.
            if "id" in message and "method" not in message:
                self._responses[message["id"]] = message
                self._condition.notify_all()
                return
            if message.get("method") == "textDocument/publishDiagnostics":
                params = message.get("params", {})
                uri = params.get("uri")
                if isinstance(uri, str):
                    published_version = params.get("version")
                    current_version = self._document_version.get(uri)
                    if (
                        isinstance(published_version, int)
                        and current_version is not None
                        and published_version != current_version
                    ):
                        return
                    diagnostics = list(params.get("diagnostics") or [])
                    self._diagnostics[uri] = diagnostics
                    publications = self._diagnostic_publications.get(uri, 0) + 1
                    self._diagnostic_publications[uri] = publications
                    # Push diagnostics are not a protocol-level completion
                    # signal. The common Lean sequence is an empty placeholder
                    # set on didOpen followed by the final set after elaboration,
                    # so ``publications >= 2`` recognizes that replacement and a
                    # non-empty set is treated the same way. We then wait briefly
                    # for the authoritative ``fileProgress=[]``; if it never
                    # comes, the adapter raises ``LeanServerAmbiguousCompletion``
                    # so ``_check_with_server`` falls back to the subprocess
                    # checker instead of accepting an unconfirmed result.
                    if publications >= 2 or diagnostics:
                        self._diagnostic_fallback_deadlines[uri] = (
                            time.monotonic() + self._diagnostics_fallback_seconds
                        )
                    self._condition.notify_all()
            elif message.get("method") == "$/lean/fileProgress":
                params = message.get("params", {})
                text_document = params.get("textDocument", {})
                uri = text_document.get("uri")
                if isinstance(uri, str):
                    if params.get("processing"):
                        self._processing_documents.add(uri)
                        # The server is actively elaborating this document, so
                        # any pending diagnostic fallback deadline is stale:
                        # we have an explicit progress signal now and must wait
                        # for ``processing=[]`` rather than let a short fallback
                        # timer declare an ambiguous completion mid-elaboration.
                        self._diagnostic_fallback_deadlines.pop(uri, None)
                    else:
                        self._completed_documents.add(uri)
                    self._condition.notify_all()

    def _raise_reader_error(self) -> None:
        if self._reader_error is not None:
            raise self._reader_error


def _read_exact(stream: Any, content_length: int) -> bytes:
    """Read one framed JSON-RPC body, tolerating short pipe reads."""
    chunks: list[bytes] = []
    remaining = content_length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            received = content_length - remaining
            raise LeanServerError(
                f"Lean server stdout ended mid-message ({received}/{content_length} bytes)."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _message_summary(message: dict[str, Any]) -> str:
    method = message.get("method")
    request_id = message.get("id")
    if method == "textDocument/publishDiagnostics":
        params = message.get("params") or {}
        return (
            f"method={method} id={request_id} "
            f"diagnostics={len(params.get('diagnostics') or [])} uri={params.get('uri')}"
        )
    if method == "$/lean/fileProgress":
        params = message.get("params") or {}
        document = params.get("textDocument") or {}
        return (
            f"method={method} processing={len(params.get('processing') or [])} "
            f"uri={document.get('uri')}"
        )
    return f"method={method} id={request_id} has_result={'result' in message}"


def _format_lsp_diagnostics(path: Path, diagnostics: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for diagnostic in diagnostics:
        severity = _lsp_severity_name(diagnostic.get("severity", 1))
        start = diagnostic.get("range", {}).get("start", {})
        line = int(start.get("line", 0)) + 1
        column = int(start.get("character", 0)) + 1
        message = str(diagnostic.get("message", "")).strip()
        lines.append(f"{path.name}:{line}:{column}: {severity}: {message}")
    return "\n".join(lines).strip()


def _lsp_severity_name(value: Any) -> str:
    if value == 2:
        return "warning"
    if value == 3:
        return "information"
    if value == 4:
        return "hint"
    return "error"
