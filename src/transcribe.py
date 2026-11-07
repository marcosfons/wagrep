"""Voice note transcription, only on explicit request (`wagrep transcribe ID...`).

Transcribing costs time and possibly money, so nothing here runs on its own:
not on sync, not when a voice note arrives. Two backends:

  api    any OpenAI-compatible /v1/audio/transcriptions endpoint: OpenAI,
         Groq, or a self-hosted faster-whisper server.
  local  faster-whisper in a private virtualenv, installed once with
         `wagrep transcribe --setup local`. This module is also the worker
         that runs inside that virtualenv (`python -m src.transcribe
         --worker`), so the main program stays standard-library only.

Results go to index.TRANSCRIPTS, outside the rebuildable index.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from . import index

log = logging.getLogger("wagrep.transcribe")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCAL_MODEL = "large-v3-turbo"
# ctranslate2 (faster-whisper's engine) publishes wheels for these.
LOCAL_PYTHONS = ("python3.13", "python3.12", "python3.11", "python3.10")
API_PARALLEL = 2      # keeps the server's single worker busy while uploading
MAX_UPLOAD = 24_000_000


class TranscribeError(Exception):
    """Configuration or backend failure; message is user-facing."""


# --- configuration -------------------------------------------------------

def load_config() -> dict:
    cfg = index.load_config("whisper")
    for key in ("backend", "url", "key", "model", "language", "prompt"):
        env = os.environ.get(f"WAGREP_WHISPER_{key.upper()}")
        if env:
            cfg[key] = env
    return cfg


def save_config(values: dict):
    index.save_config("whisper", values)


def venv_python(data_dir: str) -> str:
    return os.path.join(data_dir, "whisper-venv", "bin", "python")


# --- prompt --------------------------------------------------------------

def build_prompt(conn, msg: dict, cfg: dict) -> str:
    """Whisper continues its prompt's style and spelling, so the prompt is the
    configured lead sentence plus the names of who talks in this chat."""
    names = [r[0] for r in conn.execute(
        "SELECT sender FROM msgs WHERE chat_id = ? AND sender != 'me' "
        "AND kind != 'system' GROUP BY sender ORDER BY MAX(ts) DESC LIMIT 12",
        (msg["chat_id"],))]
    lead = (cfg.get("prompt") or "").strip()
    people = ", ".join(n for n in names if n and not n.startswith("+"))
    return " ".join(p for p in (lead, f"{people}." if people else "") if p)[:600]


# --- audio ---------------------------------------------------------------

def speech_audio(path: str, kind: str) -> tuple[str, bool]:
    """A file the backend accepts: voice notes as they are; video, or audio
    over the upload limit, re-encoded to small mono Opus. (path, temporary)"""
    if kind != "video" and kind != "video-note" and os.path.getsize(path) < MAX_UPLOAD:
        return path, False
    if not shutil.which("ffmpeg"):
        raise TranscribeError("video and large files need ffmpeg: brew install ffmpeg")
    out = os.path.join(tempfile.gettempdir(), f"wagrep-{uuid.uuid4().hex}.ogg")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", out],
                   check=True, timeout=600)
    return out, True


# --- api backend ---------------------------------------------------------

def _multipart(fields: dict, file_path: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f"\r\n\r\n{value}\r\n".encode())
    with open(file_path, "rb") as fh:
        data = fh.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{os.path.basename(file_path)}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api_transcribe(cfg: dict, path: str, prompt: str, duration) -> dict:
    url = cfg.get("url", "").rstrip("/")
    if not url:
        raise TranscribeError("no API url configured; run: wagrep transcribe --setup api --url URL")
    fields = {"model": cfg.get("model") or "whisper-1", "response_format": "verbose_json"}
    language = (cfg.get("language") or "auto").lower()
    if language != "auto":
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, content_type = _multipart(fields, path)
    timeout = max(120, int((duration or 60) * 2) + 60)
    headers = {"Content-Type": content_type, "X-Timeout-Seconds": str(timeout)}
    if cfg.get("key"):
        headers["Authorization"] = f"Bearer {cfg['key']}"
    for attempt in range(4):
        request = urllib.request.Request(f"{url}/audio/transcriptions", data=body,
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout + 30) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429 and attempt < 3:
                wait = float(exc.headers.get("Retry-After") or 5)
                log.warning("transcription API busy (429), retrying in %.0fs", wait)
                time.sleep(min(wait, 60))
                continue
            if exc.code in (401, 403):
                raise TranscribeError(f"API key rejected ({exc.code}); check `key` in {index.CONFIG}") from None
            raise TranscribeError(f"API error {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranscribeError(f"cannot reach {url}: {exc}") from None
    segments = data.get("segments") or []
    confidence = (sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
                  if segments else None)
    return {"text": (data.get("text") or "").strip(), "language": data.get("language"),
            "confidence": confidence, "model": fields["model"]}


# --- local backend -------------------------------------------------------

def local_transcribe(data_dir: str, cfg: dict, jobs: list):
    """Run jobs through the faster-whisper worker; yields (job, result|error).
    One process for the whole batch, so the model loads once."""
    python = venv_python(data_dir)
    if not os.path.exists(python):
        raise TranscribeError("local transcription is not set up; run: "
                              "wagrep transcribe --setup local")
    env = dict(os.environ, HF_HOME=os.path.join(data_dir, "models"),
               HF_HUB_DISABLE_TELEMETRY="1")
    proc = subprocess.Popen(
        [python, "-m", "src.transcribe", "--worker",
         cfg.get("model") or LOCAL_MODEL, (cfg.get("language") or "auto").lower()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=ROOT)
    try:
        for job in jobs:
            proc.stdin.write(json.dumps({"path": job["path"], "prompt": job["prompt"]}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise TranscribeError("the local whisper worker stopped; see logs/wagrep.log")
            answer = json.loads(line)
            yield job, answer
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def worker(model_name: str, language: str):
    """Runs inside the whisper virtualenv: one JSON job per stdin line."""
    import numpy as np
    from faster_whisper import WhisperModel

    threads = int(subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                                 capture_output=True, text=True).stdout.strip() or 4)
    model = WhisperModel(model_name, device="cpu", compute_type="int8",
                         cpu_threads=threads)
    for line in sys.stdin:
        job = json.loads(line)
        try:
            # ffmpeg instead of PyAV: faster-whisper's own decoder breaks on
            # recent PyAV releases, and ffmpeg reads every WhatsApp format.
            raw = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", job["path"],
                 "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                capture_output=True, check=True).stdout
            audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            segments, info = model.transcribe(
                audio, language=None if language == "auto" else language,
                beam_size=5, vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
                initial_prompt=job.get("prompt") or None)
            segments = list(segments)
            answer = {"text": " ".join(s.text.strip() for s in segments).strip(),
                      "language": info.language, "model": model_name,
                      "confidence": (sum(s.avg_logprob for s in segments) / len(segments)
                                     if segments else None)}
        except Exception as exc:  # noqa: BLE001 - reported per job, worker keeps going
            answer = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(answer), flush=True)


# --- running jobs --------------------------------------------------------

def run(data_dir: str, jobs: list, cfg: dict):
    """Transcribe jobs ({id, path, kind, duration, prompt}); yields
    (job, result dict or TranscribeError) in the order given."""
    backend = cfg.get("backend")
    if backend == "local":
        start = time.time()
        for job, answer in local_transcribe(data_dir, cfg, jobs):
            answer["took"], start = time.time() - start, time.time()
            yield job, (TranscribeError(answer["error"]) if "error" in answer else answer)
        return
    if backend != "api":
        raise TranscribeError(
            "transcription is not configured. Either:\n"
            "  wagrep transcribe --setup api --url https://api.openai.com/v1 --key sk-...\n"
            "      (or any OpenAI-compatible server: Groq, a self-hosted faster-whisper)\n"
            "  wagrep transcribe --setup local    (faster-whisper on this Mac, ~2 GB)")

    def one(job):
        start = time.time()
        path, temporary = speech_audio(job["path"], job["kind"])
        try:
            result = api_transcribe(cfg, path, job["prompt"], job["duration"])
            result["took"] = time.time() - start
            return result
        finally:
            if temporary:
                os.remove(path)

    with concurrent.futures.ThreadPoolExecutor(API_PARALLEL) as pool:
        futures = [(job, pool.submit(one, job)) for job in jobs]
        for job, future in futures:
            try:
                yield job, future.result()
            except (TranscribeError, subprocess.SubprocessError, OSError) as exc:
                yield job, exc if isinstance(exc, TranscribeError) else TranscribeError(str(exc))


# --- setup ---------------------------------------------------------------

def setup_api(url: str, key: str | None, model: str | None, language: str | None):
    url = url.rstrip("/")
    request = urllib.request.Request(f"{url}/models", headers=(
        {"Authorization": f"Bearer {key}"} if key else {}))
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise TranscribeError(f"{url} rejected the key ({exc.code})") from None
        log.warning("GET %s/models answered %s; saving anyway", url, exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise TranscribeError(f"cannot reach {url}: {exc}") from None
    save_config({"backend": "api", "url": url, "key": key,
                 "model": model or "whisper-1", "language": language or "auto"})


def setup_local(data_dir: str, model: str | None, language: str | None):
    """Create the virtualenv, install faster-whisper and download the model."""
    python = next((p for p in (shutil.which(n) for n in LOCAL_PYTHONS) if p), None)
    if not python:
        raise TranscribeError(
            "faster-whisper needs Python 3.10-3.13 (its engine has no wheels for "
            "newer ones yet): brew install python@3.13")
    if not shutil.which("ffmpeg"):
        raise TranscribeError("local transcription needs ffmpeg: brew install ffmpeg")
    venv = os.path.dirname(os.path.dirname(venv_python(data_dir)))
    model = model or LOCAL_MODEL
    print(f"wagrep: creating {venv} with {python}", file=sys.stderr)
    subprocess.run([python, "-m", "venv", venv], check=True)
    print("wagrep: installing faster-whisper (~200 MB)...", file=sys.stderr)
    subprocess.run([venv_python(data_dir), "-m", "pip", "install", "-q", "--upgrade",
                    "pip", "faster-whisper>=1.1,<2"], check=True)
    print(f"wagrep: downloading model {model} (~1.6 GB for large-v3-turbo)...",
          file=sys.stderr)
    env = dict(os.environ, HF_HOME=os.path.join(data_dir, "models"),
               HF_HUB_DISABLE_TELEMETRY="1")
    subprocess.run([venv_python(data_dir), "-c",
                    f"from faster_whisper import WhisperModel; WhisperModel({model!r}, "
                    "device='cpu', compute_type='int8')"], check=True, env=env)
    save_config({"backend": "local", "model": model, "language": language or "auto",
                 "url": None, "key": None})


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    worker(sys.argv[2], sys.argv[3])
