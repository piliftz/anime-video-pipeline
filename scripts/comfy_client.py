"""Minimal ComfyUI HTTP client + server launcher (stdlib only).

Talks to ComfyUI's documented HTTP API:
  GET  /system_stats            health check
  GET  /object_info/{node}      node schemas (used to validate checkpoints/samplers up front)
  POST /prompt                  queue an API-format graph -> {"prompt_id": ...}
  GET  /history/{prompt_id}     outputs + status once finished
  GET  /view?filename=...       download an output image

If no server is reachable and a ComfyUI folder is known (--comfy-dir or the
COMFYUI_DIR environment variable), `ComfyServer` starts it in the background,
waits until it answers, and stops it again afterwards.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pipeline_utils import TEMP_DIR, PipelineError, ensure_dir, get_logger

log = get_logger("comfy")

DEFAULT_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8189")


class ComfyClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    # -- low level -------------------------------------------------------- #
    def _request(self, path: str, data: dict | None = None, raw: bool = False):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.url + path, data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise PipelineError(f"ComfyUI {path} returned HTTP {exc.code}: {_explain_error(detail)}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise PipelineError(f"Cannot reach ComfyUI at {self.url} ({exc}).") from exc
        return payload if raw else json.loads(payload)

    # -- API -------------------------------------------------------------- #
    def is_up(self) -> bool:
        try:
            self._request("/system_stats")
            return True
        except PipelineError:
            return False

    def system_stats(self) -> dict:
        return self._request("/system_stats")

    def node_inputs(self, node_class: str) -> dict:
        info = self._request(f"/object_info/{urllib.parse.quote(node_class)}")
        if node_class not in info:
            raise PipelineError(f"ComfyUI has no node '{node_class}'. Update ComfyUI.")
        node = info[node_class]["input"]
        return {**node.get("required", {}), **node.get("optional", {})}

    def combo_options(self, node_class: str, input_name: str) -> list[str]:
        spec = self.node_inputs(node_class).get(input_name)
        if not spec:
            raise PipelineError(f"Node '{node_class}' has no input '{input_name}'.")
        first = spec[0]
        if isinstance(first, list):                    # classic format: [[options...], {...}]
            return [str(o) for o in first]
        if first == "COMBO" and len(spec) > 1:         # newer format: ["COMBO", {"options": [...]}]
            return [str(o) for o in spec[1].get("options", [])]
        raise PipelineError(f"Unexpected schema for {node_class}.{input_name}: {spec!r}")

    def checkpoints(self) -> list[str]:
        return self.combo_options("CheckpointLoaderSimple", "ckpt_name")

    def submit(self, graph: dict) -> str:
        resp = self._request("/prompt", {"prompt": graph, "client_id": self.client_id})
        if resp.get("node_errors"):
            raise PipelineError(f"ComfyUI rejected the workflow: {_explain_error(json.dumps(resp))}")
        return resp["prompt_id"]

    def wait(self, prompt_id: str, timeout: float = 1800.0, poll: float = 1.0) -> dict:
        """Block until the prompt finishes; return its 'outputs' dict."""
        start = time.time()
        while time.time() - start < timeout:
            hist = self._request(f"/history/{prompt_id}")
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
                    detail = msgs[0].get("exception_message", "").strip() if msgs else "unknown error"
                    raise PipelineError(f"ComfyUI failed while generating: {detail}"
                                        + (" (out of GPU memory: lower hires scale or resolution)"
                                           if "out of memory" in detail.lower() else ""))
                if status.get("completed", True):
                    return entry.get("outputs", {})
            time.sleep(poll)
        raise PipelineError(f"Timed out after {timeout:.0f}s waiting for ComfyUI prompt {prompt_id}.")

    def upload_image(self, path: Path, name: str | None = None) -> str:
        """Upload a local image to ComfyUI's input folder; return the value LoadImage expects."""
        path = Path(path)
        name = name or path.name
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in (("type", "input"), ("overwrite", "true")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                      "Content-Type: image/png\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                info = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise PipelineError(f"Uploading {path.name} to ComfyUI failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise PipelineError(f"Cannot reach ComfyUI at {self.url} ({exc}).") from exc
        sub = info.get("subfolder") or ""
        return f"{sub}/{info['name']}" if sub else info["name"]

    def download(self, image_ref: dict) -> bytes:
        query = urllib.parse.urlencode({
            "filename": image_ref["filename"],
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        })
        return self._request(f"/view?{query}", raw=True)


def _explain_error(detail: str) -> str:
    """Turn ComfyUI's JSON error blobs into one readable line."""
    try:
        data = json.loads(detail)
    except (ValueError, TypeError):
        return detail[:500]
    parts = []
    err = data.get("error")
    if isinstance(err, dict):
        parts.append(err.get("message", ""))
        if err.get("details"):
            parts.append(str(err["details"]))
    for node_id, node_err in (data.get("node_errors") or {}).items():
        for e in node_err.get("errors", []):
            parts.append(f"node {node_id} ({node_err.get('class_type')}): {e.get('message')} {e.get('details', '')}")
    return " | ".join(p for p in parts if p)[:1500] or detail[:500]


# --------------------------------------------------------------------------- #
# Server launcher
# --------------------------------------------------------------------------- #
def _find_python(comfy_dir: Path) -> Path:
    candidates = [
        comfy_dir / ".venv" / "Scripts" / "python.exe", comfy_dir / "venv" / "Scripts" / "python.exe",
        comfy_dir / ".venv" / "bin" / "python", comfy_dir / "venv" / "bin" / "python",
        comfy_dir.parent / "python_embeded" / "python.exe",  # Windows portable build
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise PipelineError(f"No Python environment found in {comfy_dir} (looked for .venv / venv / python_embeded).")


class ComfyServer:
    """Context manager: reuse a running ComfyUI or start one from *comfy_dir*."""

    def __init__(self, url: str = DEFAULT_URL, comfy_dir: str | Path | None = None,
                 startup_timeout: float = 240.0, keep_running: bool = False):
        self.client = ComfyClient(url)
        self.comfy_dir = Path(comfy_dir or os.environ.get("COMFYUI_DIR", "")) if (
            comfy_dir or os.environ.get("COMFYUI_DIR")) else None
        self.startup_timeout = startup_timeout
        self.keep_running = keep_running
        self.proc: subprocess.Popen | None = None
        self.log_path = TEMP_DIR / "comfyui_server.log"

    def __enter__(self) -> ComfyClient:
        if self.client.is_up():
            log.info("Using running ComfyUI at %s", self.client.url)
            return self.client
        if self.comfy_dir is None:
            raise PipelineError(
                f"ComfyUI is not running at {self.client.url} and no ComfyUI folder is known.\n"
                "Either start it yourself, or pass --comfy-dir / set COMFYUI_DIR so it can be started "
                "automatically, e.g. (PowerShell, once):  setx COMFYUI_DIR \"C:\\Users\\you\\comfyui-anime\"")
        main_py = self.comfy_dir / "main.py"
        if not main_py.is_file():
            raise PipelineError(f"{main_py} not found; COMFYUI_DIR must be the ComfyUI folder.")
        python = _find_python(self.comfy_dir)
        parsed = urllib.parse.urlparse(self.client.url)
        cmd = [str(python), str(main_py), "--port", str(parsed.port or 8188), "--listen", parsed.hostname or "127.0.0.1"]
        ensure_dir(self.log_path.parent)
        log.info("Starting ComfyUI: %s  (log: %s)", " ".join(cmd), self.log_path)
        self._log_fh = self.log_path.open("w", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, cwd=self.comfy_dir, stdout=self._log_fh, stderr=subprocess.STDOUT,
                                     creationflags=flags)
        start = time.time()
        while time.time() - start < self.startup_timeout:
            if self.proc.poll() is not None:
                raise PipelineError(f"ComfyUI exited during startup (code {self.proc.returncode}). "
                                    f"Last log lines:\n{self._tail()}")
            if self.client.is_up():
                log.info("ComfyUI ready after %.0fs", time.time() - start)
                return self.client
            time.sleep(2)
        self.stop()
        raise PipelineError(f"ComfyUI did not answer within {self.startup_timeout:.0f}s. Log:\n{self._tail()}")

    def _tail(self, n: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return "(no log)"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            log.info("Stopping the ComfyUI instance started by this run.")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log_fh", None):
            self._log_fh.close()

    def __exit__(self, *exc) -> None:
        if not self.keep_running:
            self.stop()
