"""Local web page for blind error analysis (`scio annotate`).

The page shows one call at a time from a run's error_analysis folder and saves
the labels into labeling_sheet.csv, the file `scio blind` writes. Nothing here
reads the judge's grades, so neither the page nor the API can show them.

The functions below work on plain values: they build the call views, check and
apply a label, and summarize the sheet. The HTTP handler only checks the token
and routes each request to them. Schema and validation live in blind.py.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from . import blind, loaders
from .runner import load_calls

TEMPLATE = Path(__file__).with_name("annotate_template.html")
TOKEN_HEADER = "X-Scio-Token"
MAX_BODY_BYTES = 16 * 1024
FORBIDDEN = "Missing or wrong token. Open the link that `scio annotate` printed."


def new_token() -> str:
    return secrets.token_urlsafe(24)


def page_url(address: str, port: int, token: str) -> str:
    return f"http://{address}:{port}/?token={token}"


def prepare(run_dir: Path, *, track: str) -> Path:
    """The run's error_analysis folder, made with `scio blind`'s defaults if it doesn't exist yet."""
    folder = run_dir / blind.OUT_NAME
    if not folder.exists():
        blind.write(run_dir, track=track)
    return folder


def brief_text(hidden: dict[str, Any]) -> str:
    """The partner's private brief as readable text, one labeled block per field."""
    blocks = []
    for key, value in hidden.items():
        if isinstance(value, list):
            body = "\n".join(f"- {item}" for item in value)
        else:
            body = str(value).strip()
        blocks.append(f"{key.replace('_', ' ').capitalize()}\n{body}")
    return "\n\n".join(blocks)


def turn_views(turns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Transcript turns for the page, numbering the seller's turns. Returns the views and the seller turn count."""
    views: list[dict[str, Any]] = []
    seller_turns = 0
    for turn in turns:
        if turn["speaker"] == "seller":
            seller_turns += 1
            views.append({"speaker": "seller", "text": turn["text"], "seller_turn": seller_turns})
        else:
            views.append({"speaker": "buyer", "text": turn["text"], "seller_turn": None})
    return views, seller_turns


def apply_label(rows: list[dict[str, str]], call_id: str, fields: dict[str, Any], seller_turns: int) -> dict[str, str]:
    """Check a label and write it into the row for call_id. Other rows are left as they are."""
    matches = [row for row in rows if row["call_id"] == call_id]
    if not matches:
        raise blind.LabelError(f"Unknown call id: {call_id!r}.")
    row = matches[0]
    row.update(blind.validate_label(fields, seller_turns))
    return row


def progress(rows: list[dict[str, str]]) -> dict[str, int]:
    return {"labeled": sum(bool(row["your_pass_fail"]) for row in rows), "total": len(rows)}


def summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Counts for the summary view: verdicts, failure categories with their call numbers, and failures by turn."""
    by_category: dict[str, list[int]] = defaultdict(list)
    by_turn: dict[int, int] = defaultdict(int)
    for row in rows:
        if row["failure_category"]:
            by_category[row["failure_category"]].append(int(row["n"]))
        if row["first_failure_turn"]:
            by_turn[int(row["first_failure_turn"])] += 1
    ranked = sorted(
        ((len(numbers), name, sorted(numbers)) for name, numbers in by_category.items()),
        key=lambda item: (-item[0], item[1]),
    )
    return {
        "progress": progress(rows),
        "pass": sum(row["your_pass_fail"] == "pass" for row in rows),
        "fail": sum(row["your_pass_fail"] == "fail" for row in rows),
        "categories": [{"category": name, "count": count, "calls": calls} for count, name, calls in ranked],
        "failures_by_turn": [{"turn": turn, "count": by_turn[turn]} for turn in sorted(by_turn)],
    }


def label_of(row: dict[str, str]) -> dict[str, str]:
    return {name: row[name] for name in blind.LABEL_FIELDS}


class Store:
    """One run's labeling sheet. Every read and write goes through a lock and the sheet on disk."""

    def __init__(self, run_dir: Path, *, track: str) -> None:
        self.folder = prepare(run_dir, track=track)
        self.sheet = self.folder / blind.SHEET_NAME
        blind.upgrade_sheet(self.sheet)
        _, rows = blind.read_sheet(self.sheet)
        personas = loaders.load_personas("all", track=track)
        calls = {call["id"]: call for call in load_calls(run_dir)}
        self._static: dict[str, dict[str, Any]] = {}
        for row in rows:
            call = calls.get(row["call_id"])
            if call is None:
                raise SystemExit(
                    f"{self.sheet} lists {row['call_id']!r}, but this run has no such call. "
                    "Delete error_analysis and run `scio annotate` again to rebuild it."
                )
            persona = personas.get(call["persona"])
            if persona is None:
                raise SystemExit(f"Track {track!r} has no persona {call['persona']!r}, needed by {row['call_id']!r}.")
            views, seller_turns = turn_views(call["turns"])
            self._static[row["call_id"]] = {
                "persona_id": call["persona"],
                "partner": dict(persona["public"]),
                "partner_private_brief": brief_text(persona["hidden"]),
                "seller_model": call["seller_model_name"],
                "seller_prompt": loaders.prompt_name(call["seller_prompt"]),
                "turns": views,
                "seller_turns": seller_turns,
            }
        self._lock = threading.Lock()

    def _static_for(self, call_id: str) -> dict[str, Any]:
        if call_id not in self._static:
            raise RuntimeError(f"The sheet lists {call_id!r}, which this server did not load. Restart `scio annotate`.")
        return self._static[call_id]

    def calls_payload(self) -> dict[str, Any]:
        with self._lock:
            _, rows = blind.read_sheet(self.sheet)
        calls = [
            {"n": int(row["n"]), "call_id": row["call_id"], **self._static_for(row["call_id"]), "label": label_of(row)}
            for row in rows
        ]
        return {"calls": calls, "progress": progress(rows)}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            _, rows = blind.read_sheet(self.sheet)
        return summarize(rows)

    def label(self, call_id: str, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        with self._lock:
            if call_id not in self._static:
                raise blind.LabelError(f"Unknown call id: {call_id!r}.")
            columns, rows = blind.read_sheet(self.sheet)
            row = apply_label(rows, call_id, fields, self._static[call_id]["seller_turns"])
            blind.write_sheet(self.sheet, columns, rows)
            return {"n": int(row["n"]), "call_id": call_id, **label_of(row)}, progress(rows)


class AnnotateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: Store, token: str) -> None:
        super().__init__(address, _Handler)
        self.store = store
        self.token = token


class _Handler(BaseHTTPRequestHandler):
    """Routes requests to the store. Every request needs the token; nothing else is decided here."""

    def log_message(self, format: str, *args: Any) -> None:
        # The default log prints request lines, and the page URL carries the token.
        return

    def _server(self) -> AnnotateServer:
        return cast(AnnotateServer, self.server)

    def _allowed(self, provided: str) -> bool:
        return hmac.compare_digest(provided.encode("utf-8"), self._server().token.encode("utf-8"))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/":
            if not self._allowed(parse_qs(url.query).get("token", [""])[0]):
                self._error(403, FORBIDDEN)
                return
            self._send(200, TEMPLATE.read_bytes(), "text/html; charset=utf-8")
            return
        if not self._allowed(self.headers.get(TOKEN_HEADER, "")):
            self._error(403, FORBIDDEN)
            return
        try:
            if url.path == "/api/calls":
                self._json(200, self._server().store.calls_payload())
            elif url.path == "/api/summary":
                self._json(200, self._server().store.summary())
            else:
                self._error(404, "Not found.")
        except RuntimeError as error:
            self._error(500, str(error))

    def do_POST(self) -> None:
        if not self._allowed(self.headers.get(TOKEN_HEADER, "")):
            self._error(403, FORBIDDEN)
            return
        if urlsplit(self.path).path != "/api/label":
            self._error(404, "Not found.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length must be a number.")
            return
        if length < 0:
            self._error(400, "Content-Length must not be negative.")
            return
        if length > MAX_BODY_BYTES:
            self._error(413, "The label request is too large.")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            self._error(400, "The request body is not valid JSON.")
            return
        if not isinstance(body, dict):
            self._error(400, "The request body must be a JSON object.")
            return
        call_id = body.pop("call_id", None)
        if not isinstance(call_id, str):
            self._error(400, "call_id is required.")
            return
        try:
            row, counts = self._server().store.label(call_id, body)
        except blind.LabelError as error:
            self._error(400, str(error))
            return
        self._json(200, {"row": row, "progress": counts})


def make_server(store: Store, *, host: str, port: int, token: str) -> AnnotateServer:
    return AnnotateServer((host, port), store, token)


def serve(server: AnnotateServer) -> None:
    """Serve until Ctrl+C, then close the socket."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def tunnel_hint(port: int, user: str, host: str) -> str:
    """The SSH command that forwards this page to another machine, and where to open it there."""
    return f"From another machine: ssh -L {port}:127.0.0.1:{port} {user}@{host}, then open the same URL there."
