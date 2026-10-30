"""Search by meaning, beside the FTS5 keyword index.

Everything here is derived and disposable. `vectors.db` lives in the state directory next
to `index.db`, is rebuilt from the notes, is never backed up, and deleting it is always
safe: recall then answers keyword-only. The markdown stays the only truth.

Four small parts, none of which knows the others' internals:

  * config      `settings()` reads the engine's config (environment, set once by whoever
                started the process). `BRAIN_VECTORS=off|shadow|on` is the switch.
  * embedder    `embed()` calls an OpenAI-shaped `POST {BRAIN_EMBEDDER_URL}/v1/embeddings`
                with urllib. The engine ships no model and no server; to swap the
                embedder or its model, change `BRAIN_EMBEDDER_URL` / `BRAIN_EMBEDDER_MODEL`.
                A new `model_key` makes every stored vector stale, so all are re-embedded.
  * store       `build()` fills `vectors.db` incrementally by content hash;
                `load()` reads it back for a query.
  * ranking     `vector_list()` is brute-force cosine in pure Python; `fuse()` merges the
                keyword list and the vector list by reciprocal-rank fusion.

The trust rules are NOT in this module. The caller hands it the set of notes a query may
see (the same scope filter the keyword search applies), and the vector arm only ever ranks
inside that set, so a vector can never surface a note keyword search would have hidden.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:          # non-POSIX: the atomic rename below still holds
    fcntl = None

MODES = ("off", "shadow", "on")
DEFAULT_MODEL = "nomic-embed-text-v1.5"
# Placeholders. The real floor and threshold are calibrated later, on data that does not
# live in this repository, and then set in the deployment's config.
DEFAULT_FLOOR = 0.35
DEFAULT_HIGH = 0.65

QUERY_PREFIX = "search_query: "
DOC_PREFIX = "search_document: "
QUERY_TIMEOUT = 2.0         # seconds, the whole query embed
INDEX_TIMEOUT = 60.0        # seconds, one batch of documents
BATCH = 16
MAX_RESPONSE = 32 * 1024 * 1024
CHUNK_CHARS = 1500
TOP_CHUNKS = 30
RRF_K = 60
DB_NAME = "vectors.db"

SCHEMA = ("CREATE TABLE IF NOT EXISTS vectors ("
          "note_id TEXT NOT NULL, chunk_no INTEGER NOT NULL, content_hash TEXT NOT NULL, "
          "model_key TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (note_id, chunk_no))")


class EmbedError(Exception):
    """The embedder could not answer. Recall catches it and degrades; indexing reports it."""


# -- config ----------------------------------------------------------------

def _number(raw, default):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def settings(env=None) -> dict:
    """The engine's vector config. An unknown `BRAIN_VECTORS` value means `off`: a typo
    must not switch a new retrieval path on."""
    env = os.environ if env is None else env
    mode = (env.get("BRAIN_VECTORS") or "off").strip().lower()
    model = (env.get("BRAIN_EMBEDDER_MODEL") or "").strip() or DEFAULT_MODEL
    return {"mode": mode if mode in MODES else "off",
            "url": (env.get("BRAIN_EMBEDDER_URL") or "").strip(),
            "model": model,
            "floor": _number(env.get("BRAIN_VECTOR_FLOOR"), DEFAULT_FLOOR),
            "high": _number(env.get("BRAIN_VECTOR_HIGH"), DEFAULT_HIGH)}


# -- embedder client -------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # note text goes to the configured embedder and nowhere else


# No proxy: note text must not take a detour through whatever http_proxy says.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _unit(values) -> bytes:
    vec = array("f", (float(v) for v in values))
    norm = math.sqrt(sum(x * x for x in vec))
    if not vec or not math.isfinite(norm) or norm == 0.0:
        raise EmbedError("the embedder returned an empty or zero vector")
    return array("f", (x / norm for x in vec)).tobytes()


def embed(cfg: dict, texts: list, prefix: str, timeout: float) -> list:
    """One unit-length float32 vector (as bytes) per text, or EmbedError. `timeout` bounds
    the whole call, not each socket read."""
    url = cfg.get("url") or ""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise EmbedError("BRAIN_EMBEDDER_URL is not an http(s) URL")
    body = json.dumps({"model": cfg["model"], "input": [prefix + t for t in texts]}).encode()
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/embeddings", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    deadline = time.monotonic() + timeout
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise EmbedError("the embedder timed out")
                piece = response.read(65536)
                if not piece:
                    break
                raw += piece
                if len(raw) > MAX_RESPONSE:
                    raise EmbedError("the embedder's answer is too large")
    except EmbedError:
        raise
    except Exception as exc:     # URLError, HTTPError, timeouts, resets, bad HTTP
        raise EmbedError("the embedder is unavailable (%s)" % type(exc).__name__) from None
    try:
        data = sorted(json.loads(bytes(raw))["data"], key=lambda item: item["index"])
        if [item["index"] for item in data] != list(range(len(texts))):
            raise ValueError("indexes")
        return [_unit(item["embedding"]) for item in data]
    except EmbedError:
        raise
    except Exception:
        raise EmbedError("the embedder's answer is not an OpenAI embeddings response") from None


# -- chunking and hashing --------------------------------------------------

def record_hash(rec: dict) -> str:
    """What a vector was made from: the title, aliases, topics and body of a note."""
    blob = "\x00".join(str(rec.get(k) or "") for k in ("title", "aliases", "topics", "body"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _windows(paragraph: str, size: int) -> list:
    out = []
    while len(paragraph) > size:
        cut = paragraph.rfind(" ", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()
    return out + [paragraph]


def chunk_texts(rec: dict, size: int = CHUNK_CHARS) -> list:
    """Chunk 0 is the title, aliases and topics; the rest are body chunks of about
    `size` characters, cut at paragraph breaks (a paragraph longer than that is cut at
    a space)."""
    head = [str(rec.get("title") or "")]
    if rec.get("aliases"):
        head.append("Aliases: %s" % rec["aliases"])
    if rec.get("topics"):
        head.append("Topics: %s" % rec["topics"])
    chunks = ["\n".join(head).strip()]
    current = ""
    for paragraph in re.split(r"\n\s*\n", str(rec.get("body") or "")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for piece in _windows(paragraph, size):
            if current and len(current) + 2 + len(piece) > size:
                chunks.append(current)
                current = piece
            else:
                current = current + "\n\n" + piece if current else piece
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


# -- the store -------------------------------------------------------------

def db_path(state_dir):
    return os.path.join(str(state_dir), DB_NAME)


def build(records, state_dir, cfg: dict, full: bool = False, embedder=None, quiet=True) -> dict:
    """Bring `vectors.db` in line with `records` (the FTS-indexed set, nothing else).

    Incremental: a note whose content hash and model are unchanged is skipped; rows of
    notes that are no longer in the set are deleted first, so an archived or purged note
    loses its vectors even when the embedder is down. `full` rebuilds into a temporary
    file and swaps it in, so a failed rebuild leaves the old file serving.

    Raises EmbedError after committing the notes finished so far."""
    embedder = embedder or embed
    state_dir = str(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    path = db_path(state_dir)
    lock = open(os.path.join(state_dir, "vectors.lock"), "w")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        target = tmp if full else path
        if full and os.path.exists(tmp):
            os.unlink(tmp)
        db = sqlite3.connect(target, timeout=30)
        try:
            db.execute(SCHEMA)
            stored = {}
            for note_id, content_hash, model in db.execute(
                    "SELECT note_id, content_hash, model_key FROM vectors "
                    "GROUP BY note_id, content_hash, model_key"):
                stored.setdefault(note_id, set()).add((content_hash, model))
            wanted, seen = [], set()
            for rec in records:
                if rec["id"] not in seen:         # an id used twice would collide on the key
                    seen.add(rec["id"])
                    wanted.append(rec)
            removed = [n for n in stored if n not in seen]
            with db:
                db.executemany("DELETE FROM vectors WHERE note_id = ?", [(n,) for n in removed])
            stats = {"notes": len(wanted), "embedded": 0, "skipped": 0,
                     "removed": len(removed), "chunks": 0}
            for rec in wanted:
                content_hash = record_hash(rec)
                if stored.get(rec["id"]) == {(content_hash, cfg["model"])}:
                    stats["skipped"] += 1
                    continue
                texts = chunk_texts(rec)
                vectors = []
                for start in range(0, len(texts), BATCH):
                    vectors += embedder(cfg, texts[start:start + BATCH], DOC_PREFIX, INDEX_TIMEOUT)
                with db:
                    db.execute("DELETE FROM vectors WHERE note_id = ?", (rec["id"],))
                    db.executemany(
                        "INSERT INTO vectors (note_id, chunk_no, content_hash, model_key, vector) "
                        "VALUES (?,?,?,?,?)",
                        [(rec["id"], no, content_hash, cfg["model"], vec)
                         for no, vec in enumerate(vectors)])
                stats["embedded"] += 1
                stats["chunks"] += len(vectors)
        finally:
            db.close()
        if full:
            os.replace(tmp, path)
        return stats
    finally:
        lock.close()
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(state_dir, model_key: str) -> list:
    """Every stored chunk made with `model_key`, as (note_id, chunk_no, hash, vector).
    Empty when the file is missing, unreadable or built for another model."""
    path = db_path(state_dir)
    if not os.path.exists(path):
        return []
    try:
        db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT note_id, chunk_no, content_hash, vector FROM vectors "
                              "WHERE model_key = ?", (model_key,)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    out = []
    for note_id, chunk_no, content_hash, blob in rows:
        vec = array("f")
        vec.frombytes(blob)
        out.append((note_id, chunk_no, content_hash, vec))
    return out


# -- ranking ---------------------------------------------------------------

def cosine(a, b) -> float:
    """Dot product; both sides are unit length."""
    return sum(x * y for x, y in zip(a, b)) if len(a) == len(b) else 0.0


def vector_list(query_vec, rows, allowed: dict, weights: dict, floor: float, fresh,
                limit: int = TOP_CHUNKS) -> list:
    """The meaning arm's ranked list, one entry per note, best chunk first.

    `allowed` maps note id -> folder for the notes this query may see; a stored vector
    for any other note (archived, out of scope, hidden from this caller) is ignored.
    `fresh(note_id, stored_hash)` says whether the vector still describes the note as it
    is on disk; a stale one is skipped, so an old meaning is never served.
    Only chunks whose cosine is above `floor` count; they rank by cosine times the folder
    weight; the top `limit` chunks are kept; a note scores by its best one."""
    qvec = array("f")
    qvec.frombytes(query_vec)
    scored = []
    for note_id, chunk_no, content_hash, vec in rows:
        folder = allowed.get(note_id)
        if folder is None:
            continue
        sim = cosine(qvec, vec)
        if sim > floor:
            scored.append((-sim * weights.get(folder, 1.0), note_id, chunk_no, sim, content_hash))
    scored.sort()
    out, seen, verdict, taken = [], set(), {}, 0
    for _weighted, note_id, chunk_no, sim, content_hash in scored:
        if taken >= limit:
            break
        if note_id not in verdict:
            verdict[note_id] = bool(fresh(note_id, content_hash))
        if not verdict[note_id]:
            continue
        taken += 1
        if note_id not in seen:
            seen.add(note_id)
            out.append({"id": note_id, "chunk_no": chunk_no, "cosine": sim})
    return out


def fuse(keyword_ids: list, vector_hits: list, high: float) -> list:
    """Reciprocal-rank fusion: score = sum of 1 / (RRF_K + rank) over the lists a note is
    in. Ties go to the keyword order (bm25 times folder weight), then the vector order.
    Each entry names its evidence: a note in both lists is `keyword_exact`; a vector-only
    note is `high_vector_match` at or above `high`, else `weak_semantic`."""
    entries = {}
    for rank, note_id in enumerate(keyword_ids, 1):
        entries.setdefault(note_id, {"id": note_id, "kw": rank, "vec": None, "score": 0.0})
        entries[note_id]["score"] += 1.0 / (RRF_K + rank)
    for rank, hit in enumerate(vector_hits, 1):
        entry = entries.setdefault(hit["id"], {"id": hit["id"], "kw": None, "vec": None,
                                               "score": 0.0})
        entry["vec"] = dict(hit, rank=rank)
        entry["score"] += 1.0 / (RRF_K + rank)
    never = len(keyword_ids) + len(vector_hits) + 1
    out = sorted(entries.values(),
                 key=lambda e: (-round(e["score"], 12), e["kw"] or never, e["vec"]["rank"] if e["vec"] else never))
    for entry in out:
        if entry["kw"] is not None:
            entry["evidence"] = "keyword_exact"
        else:
            entry["evidence"] = ("high_vector_match" if entry["vec"]["cosine"] >= high
                                 else "weak_semantic")
    return out
