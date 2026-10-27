"""Loopback stand-in for chat.postMessage with per-thread fault injection.

Faults are keyed by thread_ts because the posted text is model output, not a job key.
  "429:N"   reject with HTTP 429 and Retry-After: N (not recorded as delivered)
  "timeout" record the post, then stall past the worker's 8s client timeout

Also accepts chat.update and chat.postEphemeral for the ChatOps scenarios; every call is kept
in `calls` with its method so tests can count message updates and private notices.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockSlack:
    def __init__(self, token):
        self.token = token
        self.posts, self.rejected, self.faults, self.calls = [], [], {}, []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()

    def fault(self, thread_ts, *behaviors):
        with self.lock:
            self.faults.setdefault(thread_ts, []).extend(behaviors)

    def posts_for(self, thread_ts):
        with self.lock:
            return [p for p in self.posts if p["thread_ts"] == thread_ts]

    def calls_for(self, method, **match):
        with self.lock:
            return [c for c in self.calls if c["method"] == method
                    and all(c["body"].get(k) == v for k, v in match.items())]

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _reply(self, code, body, headers=()):
                data = json.dumps(body).encode()
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                method = self.path.removeprefix("/api/")
                if method not in ("chat.postMessage", "chat.update", "chat.postEphemeral"):
                    return self._reply(404, {"ok": False, "error": "unknown_method"})
                if self.headers.get("Authorization") != "Bearer " + mock.token:
                    return self._reply(200, {"ok": False, "error": "invalid_auth"})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                with mock.lock:
                    ts = f"{time.time():.6f}"
                    mock.calls.append({"method": method, "body": body, "ts": ts, "at": time.time()})
                if method == "chat.update":
                    return self._reply(200, {"ok": True, "ts": body["ts"]})
                if method == "chat.postEphemeral":
                    return self._reply(200, {"ok": True, "message_ts": ts})
                body.setdefault("thread_ts", "")
                with mock.lock:
                    pending = mock.faults.get(body["thread_ts"], [])
                    behavior = pending.pop(0) if pending else None
                if behavior and behavior.startswith("429:"):
                    with mock.lock:
                        mock.rejected.append(body["thread_ts"])
                    return self._reply(429, {"ok": False, "error": "ratelimited"},
                                       [("Retry-After", behavior.split(":")[1])])
                with mock.lock:
                    mock.posts.append({"channel": body["channel"], "thread_ts": body["thread_ts"],
                                       "text": body["text"], "blocks": body.get("blocks"), "ts": ts,
                                       "at": time.time()})
                if behavior == "timeout":
                    time.sleep(10)
                self._reply(200, {"ok": True, "ts": ts})

        return Handler
