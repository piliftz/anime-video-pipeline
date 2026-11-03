"""Minimal VOICEVOX engine client + launcher (stdlib only).

VOICEVOX is a free Japanese TTS engine that runs locally. Its HTTP API:
  GET  /version                       health check
  GET  /speakers                      characters and their styles (style ids are what synthesis uses)
  GET  /speaker_info?speaker_uuid=…   terms of use ("policy") per character
  POST /audio_query?text=…&speaker=ID returns an editable query (speed, pitch, intonation…)
  POST /synthesis?speaker=ID          body = the query JSON, returns a WAV

Voices are cast by NAME (e.g. "青山龍星" / "ノーマル") and resolved to ids at runtime, because ids can
differ between engine versions. If the engine isn't running and VOICEVOX_RUN (or a standard install
path) points to the engine's run.exe, it is started automatically.

Every VOICEVOX character has its own terms; most require a credit such as "VOICEVOX:青山龍星".
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
from pathlib import Path

from pipeline_utils import TEMP_DIR, PipelineError, ensure_dir, get_logger

log = get_logger("voicevox")

DEFAULT_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
# Query fields we let storyboards adjust (names as in VOICEVOX's AudioQuery).
QUERY_PARAMS = {"speed": "speedScale", "pitch": "pitchScale", "intonation": "intonationScale",
                "volume": "volumeScale", "pre": "prePhonemeLength", "post": "postPhonemeLength"}


class VoicevoxClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._speakers: list[dict] | None = None

    def _request(self, path: str, body: bytes | None = None, method: str | None = None, raw: bool = False):
        req = urllib.request.Request(self.url + path, data=body, method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            raise PipelineError(f"VOICEVOX {path.split('?')[0]} returned HTTP {exc.code}: "
                                f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise PipelineError(f"Cannot reach the VOICEVOX engine at {self.url} ({exc}).") from exc
        return data if raw else json.loads(data)

    def is_up(self) -> bool:
        try:
            self._request("/version")
            return True
        except PipelineError:
            return False

    def speakers(self) -> list[dict]:
        if self._speakers is None:
            self._speakers = self._request("/speakers")
        return self._speakers

    def resolve(self, speaker: str, style: str | None) -> tuple[int, str, str]:
        """(style_id, speaker_uuid, style_name_used) for a character name + style name."""
        found = next((s for s in self.speakers() if s["name"] == speaker), None)
        if found is None:
            names = ", ".join(s["name"] for s in self.speakers())
            raise PipelineError(f"VOICEVOX has no character '{speaker}'. Installed: {names}")
        styles = [st for st in found["styles"] if st.get("type", "talk") == "talk"]
        chosen = next((st for st in styles if st["name"] == style), None) if style else None
        if chosen is None:
            chosen = styles[0]
            if style:
                log.warning("'%s' has no style '%s'; using '%s'. Styles: %s", speaker, style, chosen["name"],
                            ", ".join(st["name"] for st in styles))
        return int(chosen["id"]), found["speaker_uuid"], chosen["name"]

    def synthesize(self, text: str, style_id: int, params: dict | None = None) -> bytes:
        q = urllib.parse.urlencode({"text": text, "speaker": style_id})
        query = self._request(f"/audio_query?{q}", body=b"", method="POST")
        for key, field in QUERY_PARAMS.items():
            if params and params.get(key) is not None:
                query[field] = float(params[key])
        return self._request(f"/synthesis?speaker={style_id}", body=json.dumps(query).encode("utf-8"), raw=True)

    def policy(self, speaker_uuid: str) -> str:
        info = self._request(f"/speaker_info?{urllib.parse.urlencode({'speaker_uuid': speaker_uuid})}")
        return info.get("policy", "")


def _find_engine() -> Path | None:
    env = os.environ.get("VOICEVOX_RUN")
    candidates = [Path(env)] if env else []
    local = os.environ.get("LOCALAPPDATA")
    if local:  # default per-user install of the VOICEVOX app on Windows
        candidates += [Path(local) / "Programs" / "VOICEVOX" / "vv-engine" / "run.exe",
                       Path(local) / "Programs" / "VOICEVOX" / "run.exe"]
    return next((c for c in candidates if c.is_file()), None)


class VoicevoxServer:
    """Context manager: reuse a running engine, or start run.exe and stop it afterwards."""

    def __init__(self, url: str = DEFAULT_URL, startup_timeout: float = 120.0):
        self.client = VoicevoxClient(url)
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> VoicevoxClient:
        if self.client.is_up():
            return self.client
        engine = _find_engine()
        if engine is None:
            raise PipelineError(
                f"The VOICEVOX engine is not running at {self.client.url}.\n"
                "Start the VOICEVOX app (it runs the engine while open), or set VOICEVOX_RUN to the engine's "
                "run.exe so it can be started automatically.")
        parsed = urllib.parse.urlparse(self.client.url)
        cmd = [str(engine), "--host", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 50021)]
        log_path = ensure_dir(TEMP_DIR) / "voicevox_engine.log"
        log.info("Starting VOICEVOX engine: %s (log: %s)", " ".join(cmd), log_path)
        self._log = log_path.open("w", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, cwd=engine.parent, stdout=self._log, stderr=subprocess.STDOUT,
                                     creationflags=flags)
        start = time.time()
        while time.time() - start < self.startup_timeout:
            if self.proc.poll() is not None:
                raise PipelineError(f"VOICEVOX engine exited during startup (code {self.proc.returncode}); see {log_path}")
            if self.client.is_up():
                log.info("VOICEVOX ready after %.0fs", time.time() - start)
                return self.client
            time.sleep(1.5)
        self.__exit__()
        raise PipelineError(f"VOICEVOX engine did not answer within {self.startup_timeout:.0f}s; see {log_path}")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log", None):
            self._log.close()


def main(argv: list[str] | None = None) -> int:
    """List installed VOICEVOX characters/styles, or audition a line to a WAV file."""
    import argparse

    from pipeline_utils import setup_logging

    p = argparse.ArgumentParser(description="List VOICEVOX voices or audition a line.")
    p.add_argument("--say", help="Japanese text to speak (audition).")
    p.add_argument("--speaker", help="Character name, e.g. 青山龍星")
    p.add_argument("--style", help="Style name, e.g. ノーマル (default: the character's first style)")
    p.add_argument("--speed", type=float)
    p.add_argument("-o", "--out", type=Path, default=Path("output/voicevox_audition.wav"))
    args = p.parse_args(argv)
    setup_logging()
    try:
        with VoicevoxServer() as vv:
            if not args.say:
                for s in vv.speakers():
                    styles = ", ".join(st["name"] for st in s["styles"] if st.get("type", "talk") == "talk")
                    print(f"{s['name']}: {styles}")
                return 0
            if not args.speaker:
                raise PipelineError("--say needs --speaker")
            style_id, _, used = vv.resolve(args.speaker, args.style)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(vv.synthesize(args.say, style_id, {"speed": args.speed}))
            print(f"{args.out}  ({args.speaker} / {used})")
            return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
