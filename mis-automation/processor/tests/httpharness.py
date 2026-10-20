"""Starts the real ASGI app under uvicorn on a free port and talks to it over HTTP (urllib)."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

import uvicorn

from processor.app import create_app

KEY = "test-key-" + uuid.uuid4().hex


class Server:
    def __init__(self, settings, db, llm=None):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.app = create_app(settings, db, llm)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def call(self, method, path, body=None, *, key=KEY, headers=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": ctype, **(headers or {})}
        if key is not None:
            h["X-API-Key"] = key
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            try:
                return e.code, json.loads(raw_body)
            except Exception:
                return e.code, {"raw": raw_body[:200].decode(errors="replace")}

    def upload(self, path, token, filename, data, **kw):
        b = "----t" + uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="claim_token"\r\n\r\n{token}\r\n'.encode(),
                 f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
                 data, f"\r\n--{b}--\r\n".encode()]
        return self.call("POST", path, raw=b"".join(parts), ctype=f"multipart/form-data; boundary={b}", **kw)
