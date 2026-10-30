"""A deterministic stand-in for the embedder service, for the hybrid-search tests.

It speaks the one call the engine makes, `POST /v1/embeddings` (OpenAI shape), on a
loopback port, and turns text into a hashed bag of words: the same words always give the
same vector, and two texts that share words are close. A small SYNONYMS table puts two
different words in one bucket, which is how a test gets a note that keyword search cannot
find but the meaning arm can. It strips the `search_query: ` / `search_document: ` prefix
before hashing (and records it), as a model that understands the task prefix would.

Nothing here is a model, and nothing leaves the machine."""
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 128
SYNONYMS = {"gizmo": "sprocket", "gizmos": "sprocket", "sprockets": "sprocket",
            "toothed": "sprocket", "automobile": "car", "vehicle": "car"}


def vectorize(text: str) -> list:
    vec = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        word = SYNONYMS.get(word, word)
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    return vec


class FakeEmbedder(object):
    """`with FakeEmbedder() as fake:` then point BRAIN_EMBEDDER_URL at `fake.url`.
    `fake.calls` records (prefix, text) for every input; `fake.fail` / `fake.delay`
    make it refuse or stall, `fake.drip` makes it send the body a byte at a time."""

    def __init__(self):
        self.calls, self.fail, self.delay, self.drip = [], None, 0.0, 0.0
        self.arrivals = 0                  # embedding requests received, counted before any delay
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/v1/embeddings":       # a wrong path in the client must fail a test
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer.arrivals += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.fail:
                    self.send_response(outer.fail)
                    self.end_headers()
                    return
                data = []
                for index, text in enumerate(body["input"]):
                    prefix = text.split(": ", 1)[0] + ": "
                    outer.calls.append((prefix, text[len(prefix):]))
                    data.append({"object": "embedding", "index": index,
                                 "embedding": vectorize(text[len(prefix):])})
                payload = json.dumps({"object": "list", "model": body.get("model"),
                                      "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if outer.drip:               # a body one byte per `drip` seconds, until the client gives up
                    try:
                        for at in range(len(payload)):
                            self.wfile.write(payload[at:at + 1])
                            self.wfile.flush()
                            time.sleep(outer.drip)
                    except OSError:
                        pass
                    return
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def embedded_texts(self, prefix):
        return [t for p, t in self.calls if p == prefix]
