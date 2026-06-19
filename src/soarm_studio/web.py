from __future__ import annotations

import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from soarm_studio.config import SessionConfig
from soarm_studio.datasets.tools import inspect_dataset, validate_dataset
from soarm_studio.hardware.calibration import calibrate_session
from soarm_studio.hardware.runtime import HardwareSession, preflight_report_to_dict
from soarm_studio.recording import record_lerobot_episodes


class StudioWebServer(ThreadingHTTPServer):
    def __init__(self, address, handler, *, config: SessionConfig) -> None:
        super().__init__(address, handler)
        self.config = config


class StudioWebHandler(SimpleHTTPRequestHandler):
    server: StudioWebServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/bindings":
            with HardwareSession(self.server.config) as hardware:
                self._json(hardware.verify_bindings())
            return
        if parsed.path == "/api/status":
            with HardwareSession(self.server.config) as hardware:
                self._json(hardware.read_status())
            return
        if parsed.path == "/api/preflight":
            params = parse_qs(parsed.query)
            overwrite = params.get("overwrite", ["false"])[0].lower() == "true"
            with HardwareSession(self.server.config) as hardware:
                self._json(preflight_report_to_dict(hardware.preflight(dataset_overwrite=overwrite)))
            return
        if parsed.path == "/api/dataset/inspect":
            self._json(inspect_dataset(self.server.config.dataset.root))
            return
        if parsed.path == "/api/dataset/validate":
            errors = validate_dataset(self.server.config.dataset.root)
            self._json({"valid": not errors, "errors": errors})
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()
        if parsed.path == "/api/teleop":
            seconds = float(payload.get("seconds", 2.0))
            with HardwareSession(self.server.config) as hardware:
                report = hardware.preflight(dataset_overwrite=True)
                if not report.ok:
                    self._json(preflight_report_to_dict(report), status=HTTPStatus.BAD_REQUEST)
                    return
                self._json({"metrics": hardware.run_teleop(seconds=seconds)})
            return
        if parsed.path == "/api/record":
            result = record_lerobot_episodes(
                self.server.config,
                seconds=float(payload.get("seconds", 2.0)),
                task=str(payload.get("task", "web recording")),
                overwrite=bool(payload.get("overwrite", False)),
                warmup=float(payload.get("warmup", 0.0)),
                episodes=int(payload.get("episodes", 1)),
            )
            self._json(result)
            return
        if parsed.path == "/api/calibrate":
            role = str(payload.get("role", "both"))
            if not self.server.config.leader.mock or not self.server.config.follower.mock:
                self._json(
                    {
                        "error": (
                            "hardware calibration is interactive; use "
                            "`soarm-studio calibrate --role ...`"
                        )
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            self._json(calibrate_session(self.server.config, role=role))  # type: ignore[arg-type]
            return
        self._json({"error": f"unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _serve_static(self, path: str) -> None:
        root = Path(__file__).with_name("webapp")
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (root / rel).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or target.is_dir():
            target = root / "index.html"
        content_type = _content_type(target)
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_web(config: SessionConfig, *, host: str, port: int) -> None:
    server = StudioWebServer((host, port), StudioWebHandler, config=config)
    print(f"SOARM Studio web running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".js":
        return "text/javascript; charset=utf-8"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".png":
        return "image/png"
    return "application/octet-stream"
